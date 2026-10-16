"""
ClickHouse schema management and query helpers for the provider-aware tick
pipeline (pepperstone / mock).

Tables are created idempotently (``CREATE TABLE IF NOT EXISTS``) and every DDL
failure — including the verified ``ACCESS_DENIED`` (error 497) on the ``admin``
user — is logged and swallowed so the API never crashes over schema issues.

Provider awareness:
    Both tables carry a ``provider String`` column (default ``'pepperstone'``)
    so market data from multiple brokers can coexist in one table and be
    filtered independently. Fresh tables are created with the column in the
    DDL; pre-existing tables are upgraded idempotently in ``ensure_schema``
    via ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS provider``. Every query
    helper accepts an optional ``provider=`` filter (an additive AND clause);
    when omitted, behaviour is identical to the pre-provider schema.

Environment variables:
    CLICKHOUSE_TICKS_TABLE          Default: "default.forex_ticks"
    CLICKHOUSE_TRADE_HISTORY_TABLE  Default: "default.trade_history"

Target database note:
    The ``admin`` user currently LACKS CREATE DATABASE / CREATE TABLE grants on
    the production server (verified error 497 ACCESS_DENIED), so tables default
    to the ``default`` database. The intended production location is
    ``njra_markets.forex_ticks`` — once grants are fixed, set
    ``CLICKHOUSE_TICKS_TABLE=njra_markets.forex_ticks`` (and the trade-history
    equivalent) in the environment; no code change is required.

Query helpers use clickhouse-connect's ``parameters=`` binding (never
``settings=``) for safe value substitution. Table names are trusted
environment configuration and are interpolated directly into the SQL — they
are never user input.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from forex.clickhouse.connection import CLICKHOUSE_DATABASE

logger = logging.getLogger("forex.clickhouse.schema")

# ---------------------------------------------------------------------------
# Environment-driven table names (fully qualified: <database>.<table>)
# ---------------------------------------------------------------------------
TICKS_TABLE = os.environ.get("CLICKHOUSE_TICKS_TABLE", "default.forex_ticks")
TRADE_HISTORY_TABLE = os.environ.get(
    "CLICKHOUSE_TRADE_HISTORY_TABLE", "default.trade_history"
)
CANDLES_TABLE = os.environ.get("CLICKHOUSE_CANDLES_TABLE", "default.candles")
SIGNALS_V2_TABLE = os.environ.get("CLICKHOUSE_SIGNALS_V2_TABLE", "default.trading_signals_v2")
FVG_TABLE = os.environ.get("CLICKHOUSE_FVG_TABLE", "default.fair_value_gaps")

# Default provider applied to legacy rows and fresh DDL columns. Matches the
# canonical ProviderName values in ``forex.broker.models``.
DEFAULT_PROVIDER = "pepperstone"

# ---------------------------------------------------------------------------
# DDL (idempotent; ReplacingMergeTree deduplicates on full-row replacement)
# ---------------------------------------------------------------------------
# TTL clause: keep only the last 60 days of ticks (~3.4M rows expected).
# For existing tables without TTL, ensure_schema runs an idempotent ALTER
# (see _ensure_table) to add the TTL post-create.
_TICKS_DDL = f"""
CREATE TABLE IF NOT EXISTS {TICKS_TABLE} (
    provider String DEFAULT '{DEFAULT_PROVIDER}',
    symbol String,
    bid Float64,
    ask Float64,
    spread Float32,
    digits UInt8,
    timestamp DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree()
ORDER BY (symbol, timestamp)
TTL timestamp + INTERVAL 60 DAY DELETE
"""

_TRADE_HISTORY_DDL = f"""
CREATE TABLE IF NOT EXISTS {TRADE_HISTORY_TABLE} (
    provider String DEFAULT '{DEFAULT_PROVIDER}',
    account_id UInt64,
    symbol String,
    side String,
    volume UInt64,
    entry_price Float64,
    realized_pnl Float64,
    timestamp DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree()
ORDER BY (account_id, timestamp)
"""

# ---------------------------------------------------------------------------
# Candle table — aggregated OHLC from the candle engine (Phase 1-2).
# ponytail: single table with `timeframe` column instead of per-TF tables.
# Partitioned by day + timeframe for efficient range scans and TTL.
# TTL: 1D/4H candles 365d, 1H/15M 90d, 5M/1M 30d — enforced via
# conditional TTL expressions in DDL; ensure_candle_schema backfills on
# existing deployments via ALTER TABLE ... MODIFY TTL.
# Engine: ReplacingMergeTree(updated_at) for idempotent re-inserts
# (dedup key = ORDER BY (symbol, timeframe, open_time)).
# ---------------------------------------------------------------------------
_CANDLES_DDL = f"""
CREATE TABLE IF NOT EXISTS {CANDLES_TABLE} (
    symbol String,
    timeframe UInt32,
    open_time DateTime,
    close_time Nullable(DateTime),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume_ticks UInt32,
    is_closed UInt8 DEFAULT 1,
    gap_filled UInt8 DEFAULT 0,
    updated_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY (timeframe, toYYYYMMDD(open_time))
ORDER BY (symbol, timeframe, open_time)
TTL open_time + INTERVAL 30 DAY DELETE WHERE timeframe IN (60, 300),
     open_time + INTERVAL 90 DAY DELETE WHERE timeframe IN (900, 3600),
     open_time + INTERVAL 365 DAY DELETE WHERE timeframe IN (14400, 86400)
"""

# ---------------------------------------------------------------------------
# Trading signals v2 — enhanced schema from the signal engine (Phase 4).
# ponytail: separate table from legacy `trading_signals` to avoid schema
# migration pain.  Legacy table stays untouched.
# ---------------------------------------------------------------------------
_SIGNALS_V2_DDL = f"""
CREATE TABLE IF NOT EXISTS {SIGNALS_V2_TABLE} (
    signal_id String,
    symbol String,
    direction String,
    state String,
    created_at DateTime64(3, 'UTC'),
    entry Float64,
    stop_loss Float64,
    take_profit Float64,
    entry_zone_low Float64,
    entry_zone_high Float64,
    confidence_score Float64,
    confidence_evidence String,
    explanation String,
    strategy String,
    timeframe String,
    market_regime String,
    risk_reward_ratio Float64
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (signal_id)
"""

# ---------------------------------------------------------------------------
# Fair Value Gaps (Phase 3D — m.md §24).
# ponytail: single table keyed by the deterministic fvg_id (ReplacingMergeTree
# dedupes on full-row replacement, so re-broadcasts just refresh status).
# ---------------------------------------------------------------------------
_FVG_DDL = f"""
CREATE TABLE IF NOT EXISTS {FVG_TABLE} (
    fvg_id String,
    symbol String,
    timeframe UInt32,
    formation_time DateTime,
    direction String,
    upper_boundary Float64,
    lower_boundary Float64,
    size Float64,
    origin String,
    status String,
    filled_percentage Float64,
    fully_filled UInt8 DEFAULT 0,
    invalidated UInt8 DEFAULT 0,
    updated_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (fvg_id)
"""
# Idempotent backfill for tables created before the provider-aware DDL.
_PROVIDER_COLUMN_DDL = (
    f"ALTER TABLE {{table}} ADD COLUMN IF NOT EXISTS "
    f"provider String DEFAULT '{DEFAULT_PROVIDER}'"
)

# Idempotent TTL migration for tables created before the 60-day retention policy.
# Checks system.tables.engine_full for existing TTL to avoid redundant ALTERs.
_TTL_MIGRATION_DDL = (
    "ALTER TABLE {table} MODIFY TTL timestamp + INTERVAL 60 DAY DELETE"
)


def _table_has_ttl(client, table: str) -> bool:
    """
    Return True if the table already has a TTL clause in its engine definition.

    Queries system.tables.engine_full which contains the full engine string
    including TTL. Returns False on any error (defensive — assume no TTL so
    the ALTER will be attempted and any 'already has TTL' error will be
    swallowed).
    """
    try:
        # table may be "db.table" or just "table"
        if "." in table:
            db, tbl = table.split(".", 1)
        else:
            db, tbl = CLICKHOUSE_DATABASE, table
        result = client.query(
            "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
            parameters={"db": db, "tbl": tbl},
        )
        if result.result_set:
            engine_full = result.result_set[0][0] or ""
            return "TTL" in engine_full.upper()
    except Exception:  # noqa: BLE001 — defensive, never crash boot
        pass
    return False


def _is_access_denied(exc: Exception) -> bool:
    """True when the exception is a ClickHouse ACCESS_DENIED (error 497)."""
    message = str(exc)
    return "ACCESS_DENIED" in message or "Code: 497" in message


def _ensure_table(client, table: str, ddl: str) -> None:
    """Idempotently create ``table`` and backfill its ``provider`` column.

    Each step is attempted independently and failures are logged — never
    raised — so a missing grant (e.g. ACCESS_DENIED on ``admin``) degrades
    only the affected table and the API keeps booting.
    """
    try:
        client.command(ddl)
        logger.info("ClickHouse table `%s` ensured", table)
    except Exception as e:  # noqa: BLE001 — DDL must never crash the pipeline
        if _is_access_denied(e):
            logger.error(
                "ClickHouse ACCESS_DENIED creating `%s` — user '%s' lacks "
                "CREATE TABLE grants; schema will be created once grants are "
                "fixed. Point CLICKHOUSE_TICKS_TABLE / "
                "CLICKHOUSE_TRADE_HISTORY_TABLE at the target database "
                "(e.g. njra_markets.forex_ticks) when available. Error: %s",
                table,
                os.environ.get("CLICKHOUSE_USER", "admin"),
                e,
            )
        else:
            logger.warning(
                "Could not ensure ClickHouse table `%s`: %s", table, e
            )

    # No-op on fresh tables (column already in DDL); upgrades pre-existing
    # tables that lack the provider column.
    try:
        client.command(_PROVIDER_COLUMN_DDL.format(table=table))
        logger.info("ClickHouse column `provider` ensured on `%s`", table)
    except Exception as e:  # noqa: BLE001 — DDL must never crash the pipeline
        if _is_access_denied(e):
            logger.error(
                "ClickHouse ACCESS_DENIED adding `provider` to `%s`: %s",
                table,
                e,
            )
        else:
            logger.warning(
                "Could not add `provider` column to `%s`: %s", table, e
            )

    # TTL migration: add 60-day retention to the ticks table if not already present.
    # Only applies to the ticks table (trade_history has no TTL requirement).
    if table == TICKS_TABLE and not _table_has_ttl(client, table):
        try:
            client.command(_TTL_MIGRATION_DDL.format(table=table))
            logger.info("ClickHouse TTL (60-day retention) ensured on `%s`", table)
        except Exception as e:  # noqa: BLE001 — DDL must never crash the pipeline
            if _is_access_denied(e):
                logger.error(
                    "ClickHouse ACCESS_DENIED adding TTL to `%s`: %s",
                    table,
                    e,
                )
            else:
                # Swallow 'already has TTL' or any other error — the table
                # either already has it or will be retried on next boot.
                logger.warning(
                    "Could not add TTL to `%s` (may already exist): %s", table, e
                )


def _table_engine(client, table: str) -> str | None:
    """Return the engine string for a table, or None on error."""
    try:
        if "." in table:
            db, tbl = table.split(".", 1)
        else:
            db, tbl = CLICKHOUSE_DATABASE, table
        result = client.query(
            "SELECT engine FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
            parameters={"db": db, "tbl": tbl},
        )
        if result.result_set:
            return result.result_set[0][0] or ""
    except Exception:  # noqa: BLE001 — defensive
        pass
    return None


def _table_has_rows(client, table: str) -> bool:
    """Return True if the table has at least one row."""
    try:
        if "." in table:
            db, tbl = table.split(".", 1)
        else:
            db, tbl = CLICKHOUSE_DATABASE, table
        result = client.query(
            f"SELECT 1 FROM {table} LIMIT 1"
        )
        return bool(result.result_set)
    except Exception:  # noqa: BLE001 — defensive
        return False


def _migrate_candles_to_replacing(client) -> None:
    """
    Migrate the candles table from MergeTree to ReplacingMergeTree.

    If the table already uses ReplacingMergeTree, this is a no-op.
    If the table has data, creates a new table with the target engine,
    copies data, drops the old table, and renames the new one.
    If the table is empty, simply recreates it with the new DDL.
    """
    try:
        engine = _table_engine(client, CANDLES_TABLE)
        if engine and "ReplacingMergeTree" in engine:
            logger.info("Candles table already uses ReplacingMergeTree; migration skipped")
            return

        has_data = _table_has_rows(client, CANDLES_TABLE)

        if has_data:
            # Safe migration: create new table, copy data, swap
            new_table = f"{CANDLES_TABLE}_new"
            # Create new table with target DDL (ReplacingMergeTree + TTL)
            new_ddl = _CANDLES_DDL.replace(CANDLES_TABLE, new_table).replace("IF NOT EXISTS", "")
            client.command(new_ddl)
            logger.info("Created new candles table `%s` with ReplacingMergeTree", new_table)

            # Copy data (updated_at will be populated with DEFAULT now64(3))
            client.command(f"INSERT INTO {new_table} SELECT *, now64(3) FROM {CANDLES_TABLE}")
            logger.info("Copied data from `%s` to `%s`", CANDLES_TABLE, new_table)

            # Atomic swap
            client.command(f"DROP TABLE {CANDLES_TABLE}")
            client.command(f"RENAME TABLE {new_table} TO {CANDLES_TABLE}")
            logger.info("Migrated candles table to ReplacingMergeTree (data preserved)")
        else:
            # Empty table: just recreate with new DDL
            client.command(f"DROP TABLE IF EXISTS {CANDLES_TABLE}")
            client.command(_CANDLES_DDL)
            logger.info("Recreated empty candles table with ReplacingMergeTree")

    except Exception as e:  # noqa: BLE001 — migration must never crash boot
        if _is_access_denied(e):
            logger.error(
                "ClickHouse ACCESS_DENIED migrating candles table — user lacks "
                "required grants. Migration will retry on next boot. Error: %s", e
            )
        else:
            logger.warning("Could not migrate candles table (will retry on next boot): %s", e)


def _ensure_candle_ttl(client) -> None:
    """
    Ensure the candles table has the correct conditional TTL expressions.

    Runs ALTER TABLE ... MODIFY TTL on existing deployments. Idempotent —
    ClickHouse will error if TTL is identical, which we swallow.
    """
    try:
        # Check if TTL already matches (best-effort via engine_full)
        if "." in CANDLES_TABLE:
            db, tbl = CANDLES_TABLE.split(".", 1)
        else:
            db, tbl = CLICKHOUSE_DATABASE, CANDLES_TABLE
        result = client.query(
            "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
            parameters={"db": db, "tbl": tbl},
        )
        if result.result_set:
            engine_full = result.result_set[0][0] or ""
            # Check if all three TTL intervals are present
            if ("30 DAY" in engine_full and "90 DAY" in engine_full and "365 DAY" in engine_full):
                logger.info("Candles table TTL already up to date")
                return

        # Apply conditional TTL
        ttl_ddl = (
            f"ALTER TABLE {CANDLES_TABLE} MODIFY TTL "
            "open_time + INTERVAL 30 DAY DELETE WHERE timeframe IN (60, 300), "
            "open_time + INTERVAL 90 DAY DELETE WHERE timeframe IN (900, 3600), "
            "open_time + INTERVAL 365 DAY DELETE WHERE timeframe IN (14400, 86400)"
        )
        client.command(ttl_ddl)
        logger.info("Candles table conditional TTL ensured")
    except Exception as e:  # noqa: BLE001 — TTL migration must never crash boot
        if _is_access_denied(e):
            logger.error(
                "ClickHouse ACCESS_DENIED adding TTL to candles table: %s", e
            )
        else:
            # Swallow 'already has TTL' or syntax errors — will retry on next boot
            logger.warning("Could not ensure candles TTL (may already exist): %s", e)


def ensure_candle_schema(client) -> None:
    """
    Idempotently ensure the candles table has the correct engine (ReplacingMergeTree)
    and conditional TTL expressions. Safe to call on every boot.
    """
    _migrate_candles_to_replacing(client)
    _ensure_candle_ttl(client)
    # Also ensure provider column (for consistency with other tables)
    try:
        client.command(_PROVIDER_COLUMN_DDL.format(table=CANDLES_TABLE))
        logger.info("ClickHouse column `provider` ensured on `%s`", CANDLES_TABLE)
    except Exception as e:  # noqa: BLE001
        if _is_access_denied(e):
            logger.error("ClickHouse ACCESS_DENIED adding `provider` to `%s`: %s", CANDLES_TABLE, e)
        else:
            logger.warning("Could not add `provider` column to `%s`: %s", CANDLES_TABLE, e)


def ensure_schema(client) -> None:
    """
    Idempotently create the tick, trade-history, and candle tables and ensure
    each has the ``provider`` column (fresh DDL covers new tables; the ALTER
    backfills pre-existing ones).
    """
    for table, ddl in (
        (TICKS_TABLE, _TICKS_DDL),
        (TRADE_HISTORY_TABLE, _TRADE_HISTORY_DDL),
        (SIGNALS_V2_TABLE, _SIGNALS_V2_DDL),
        (FVG_TABLE, _FVG_DDL),
    ):
        _ensure_table(client, table, ddl)

    # Candles table requires special handling for ReplacingMergeTree migration
    # and conditional TTL — use dedicated function.
    ensure_candle_schema(client)


# ---------------------------------------------------------------------------
# Candle persistence helpers (Phase 2)
# ---------------------------------------------------------------------------

def insert_candle_batch(client, candles: list[dict]) -> None:
    """
    Insert a batch of closed candles into the candle table.

    Each candle dict must have: symbol, timeframe, open_time, close_time,
    open, high, low, close, volume_ticks, is_closed, gap_filled.
    Failures are logged and re-raised so the caller can retry.
    """
    if not candles:
        return
    normalized = []
    for c in candles:
        normalized.append((
            c["symbol"],
            int(c["timeframe"]),
            c["open_time"],
            c.get("close_time"),
            float(c["open"]),
            float(c["high"]),
            float(c["low"]),
            float(c["close"]),
            int(c["volume_ticks"]),
            1 if c.get("is_closed", True) else 0,
            1 if c.get("gap_filled", False) else 0,
        ))
    try:
        client.insert(
            CANDLES_TABLE,
            normalized,
            column_names=[
                "symbol", "timeframe", "open_time", "close_time",
                "open", "high", "low", "close",
                "volume_ticks", "is_closed", "gap_filled",
            ],
        )
    except Exception as e:
        if _is_access_denied(e):
            logger.warning(
                "ClickHouse ACCESS_DENIED inserting candles into `%s` — "
                "user lacks INSERT grants. Candles are cached in memory; "
                "persist once grants are fixed. Error: %s",
                CANDLES_TABLE, e,
            )
        else:
            logger.error(
                "Failed to insert %d candles into `%s`: %s",
                len(normalized), CANDLES_TABLE, e,
            )
            raise


def get_candles_from_ch(
    client, symbol: str, timeframe: int, limit: int = 200
) -> list[dict]:
    """
    Return the most recent ``limit`` closed candles for ``symbol`` at
    ``timeframe`` (seconds), in chronological order.

    Only 1-minute candles are stored.  Higher timeframes are aggregated
    on-the-fly by bucketing 1m candles via ClickHouse SQL.
    Returns ``[]`` on any error (best-effort, never crashes caller).
    """
    if timeframe == 60:
        sql = (
            f"SELECT symbol, timeframe, open_time, close_time, "
            f"open, high, low, close, volume_ticks, gap_filled "
            f"FROM {CANDLES_TABLE} "
            f"WHERE symbol = {{sym:String}} AND timeframe = 60 "
            f"AND is_closed = 1 "
            f"ORDER BY open_time DESC "
            f"LIMIT {{lim:UInt32}}"
        )
        try:
            result = client.query(sql, parameters={"sym": symbol, "lim": limit})
        except Exception as e:
            logger.warning("Could not read 1m candles for %s: %s", symbol, e)
            return []
        return [
            {
                "symbol": row[0], "timeframe": row[1],
                "open_time": row[2], "close_time": row[3],
                "open": row[4], "high": row[5], "low": row[6], "close": row[7],
                "volume_ticks": row[8], "is_closed": True,
                "gap_filled": bool(row[9]),
            }
            for row in reversed(result.result_set)
        ]

    # Higher timeframes: aggregate from 1m candles on-the-fly
    sql = (
        "SELECT "
        "toStartOfInterval(open_time, INTERVAL {tf:UInt32} SECOND) AS bucket, "
        "argMin(open, open_time) AS open, max(high) AS high, "
        "min(low) AS low, argMax(close, open_time) AS close, "
        "sum(volume_ticks) AS volume_ticks "
        f"FROM {CANDLES_TABLE} "
        "WHERE symbol = {sym:String} AND timeframe = 60 AND is_closed = 1 "
        "GROUP BY bucket ORDER BY bucket DESC LIMIT {lim:UInt32}"
    )
    try:
        result = client.query(sql, parameters={"sym": symbol, "tf": timeframe, "lim": limit})
    except Exception as e:
        logger.warning("Could not aggregate %ds candles for %s: %s", timeframe, symbol, e)
        return []
    return [
        {
            "symbol": symbol, "timeframe": timeframe,
            "open_time": row[0],
            "close_time": row[0] + timedelta(seconds=timeframe),
            "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]),
            "volume_ticks": int(row[5]), "is_closed": True, "gap_filled": False,
        }
        for row in reversed(result.result_set)
    ]


# ---------------------------------------------------------------------------
# Signal v2 persistence helpers (Phase 4-5)
# ---------------------------------------------------------------------------

def insert_signal_v2(client, signal: dict) -> None:
    """
    Insert a confirmed signal into the v2 signals table.

    The signal dict must match the Signal.to_dict() output from
    signal_lifecycle.py.  Failures are logged and swallowed (best-effort).
    """
    import json
    try:
        client.insert(
            SIGNALS_V2_TABLE,
            [(
                signal["signal_id"],
                signal["symbol"],
                signal["direction"],
                signal["state"],
                signal["created_at"],
                float(signal.get("entry", 0)),
                float(signal.get("stop_loss", 0)),
                float(signal.get("take_profit", 0)),
                float(signal.get("entry_zone_low", 0)),
                float(signal.get("entry_zone_high", 0)),
                float(signal.get("confidence_score", 0)),
                json.dumps(signal.get("confidence_evidence", {})),
                signal.get("explanation", ""),
                signal.get("strategy", ""),
                signal.get("timeframe", ""),
                signal.get("market_regime", ""),
                float(signal.get("risk_reward_ratio", 0)),
            )],
            column_names=[
                "signal_id", "symbol", "direction", "state", "created_at",
                "entry", "stop_loss", "take_profit",
                "entry_zone_low", "entry_zone_high",
                "confidence_score", "confidence_evidence", "explanation",
                "strategy", "timeframe", "market_regime", "risk_reward_ratio",
            ],
        )
    except Exception as e:
        if _is_access_denied(e):
            logger.warning(
                "ClickHouse ACCESS_DENIED inserting signal into `%s` — "
                "user lacks INSERT grants. Signal broadcast via Pusher OK.",
                SIGNALS_V2_TABLE,
            )
        else:
            logger.warning("Failed to insert signal into `%s`: %s", SIGNALS_V2_TABLE, e)


def get_signals_v2(client, symbol: str | None = None, limit: int = 50) -> list[dict]:
    """
    Read the most recent ``limit`` confirmed signals from the v2 table.

    Returns ``[]`` when the table is missing/empty or the query fails.
    """
    where = ""
    params: dict = {"lim": limit}
    if symbol:
        where = "AND symbol = {sym:String}"
        params["sym"] = symbol

    sql = (
        "SELECT signal_id, symbol, direction, state, created_at, "
        "entry, stop_loss, take_profit, entry_zone_low, entry_zone_high, "
        "confidence_score, confidence_evidence, explanation, "
        "strategy, timeframe, market_regime, risk_reward_ratio "
        f"FROM {SIGNALS_V2_TABLE} "
        f"WHERE 1=1 {where} "
        "ORDER BY created_at DESC "
        "LIMIT {lim:UInt32}"
    )
    try:
        result = client.query(sql, parameters=params)
    except Exception as e:
        logger.warning("Could not read signals from `%s`: %s", SIGNALS_V2_TABLE, e)
        return []

    import json
    signals = []
    for row in result.result_set:
        try:
            evidence = json.loads(row[11]) if row[11] else {}
        except (json.JSONDecodeError, TypeError):
            evidence = {}
        signals.append({
            "signal_id": row[0],
            "symbol": row[1],
            "direction": row[2],
            "state": row[3],
            "created_at": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4]),
            "entry": float(row[5]),
            "stop_loss": float(row[6]),
            "take_profit": float(row[7]),
            "entry_zone_low": float(row[8]),
            "entry_zone_high": float(row[9]),
            "confidence_score": float(row[10]),
            "confidence_evidence": evidence,
            "explanation": row[12] or "",
            "strategy": row[13] or "",
            "timeframe": row[14] or "",
            "market_regime": row[15] or "",
            "risk_reward_ratio": float(row[16]),
        })
    return signals


def insert_fvg_batch(client, fvgs: list[dict]) -> None:
    """
    Insert / refresh Fair Value Gap rows in the FVG table.

    Each dict must carry the keys produced by ``FairValueGap.to_dict()``
    (fvg_id, symbol, timeframe, formation_time, direction, upper_boundary,
    lower_boundary, size, origin, status, filled_percentage, fully_filled,
    invalidated).  Failures are logged and swallowed (best-effort) so a CH
    outage never blocks the live pipeline.
    """
    if not fvgs:
        return
    normalized = []
    for f in fvgs:
        ft = f.get("formation_time")
        if hasattr(ft, "isoformat"):
            ft = ft.replace(tzinfo=timezone.utc) if ft.tzinfo is None else ft
        normalized.append((
            f["fvg_id"],
            f["symbol"],
            int(f["timeframe"]),
            ft,
            f["direction"],
            float(f["upper_boundary"]),
            float(f["lower_boundary"]),
            float(f["size"]),
            f.get("origin", "three_candle_imbalance"),
            f["status"],
            float(f.get("filled_percentage", 0.0)),
            1 if f.get("fully_filled", False) else 0,
            1 if f.get("invalidated", False) else 0,
        ))
    try:
        client.insert(
            FVG_TABLE,
            normalized,
            column_names=[
                "fvg_id", "symbol", "timeframe", "formation_time",
                "direction", "upper_boundary", "lower_boundary", "size",
                "origin", "status", "filled_percentage",
                "fully_filled", "invalidated",
            ],
        )
    except Exception as e:  # noqa: BLE001 — best-effort persistence
        if _is_access_denied(e):
            logger.warning(
                "ClickHouse ACCESS_DENIED inserting FVGs into `%s` — "
                "user lacks INSERT grants. FVGs broadcast via Pusher OK.",
                FVG_TABLE,
            )
        else:
            logger.warning("Failed to insert FVGs into `%s`: %s", FVG_TABLE, e)


def insert_ticks_batch(client, rows) -> None:
    """
    Insert a batch of ticks into the ticks table.

    ``rows`` is an iterable of tuples in one of two shapes:

    * provider-aware (preferred): ``(provider, symbol, bid, ask, spread,
      digits, datetime_ms)``
    * legacy: ``(symbol, bid, ask, spread, digits, datetime_ms)`` — the
      provider defaults to ``"pepperstone"`` (``DEFAULT_PROVIDER``).

    ``datetime_ms`` may be an epoch-millisecond int/float or a ``datetime``;
    epoch-millisecond values are converted to timezone-aware UTC datetimes so
    the ``DateTime64(3, 'UTC')`` column is populated correctly.

    Failures are logged and re-raised so the caller (tick pipeline) can apply
    its own retry/backoff policy — ticks are never silently dropped.
    """
    normalized = []
    for row in rows:
        if len(row) == 7:
            provider, symbol, bid, ask, spread, digits, ts = row
        elif len(row) == 6:
            symbol, bid, ask, spread, digits, ts = row
            provider = DEFAULT_PROVIDER
        else:
            raise ValueError(
                "Tick row must be a 6-tuple (legacy) or 7-tuple "
                "(provider-aware): (provider?, symbol, bid, ask, spread, "
                f"digits, ts); got {len(row)} items"
            )
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        normalized.append((provider, symbol, bid, ask, spread, digits, ts))

    try:
        client.insert(
            TICKS_TABLE,
            normalized,
            column_names=[
                "provider",
                "symbol",
                "bid",
                "ask",
                "spread",
                "digits",
                "timestamp",
            ],
        )
    except Exception as e:  # noqa: BLE001 — log and re-raise for caller retry
        logger.error(
            "Failed to insert %d ticks into `%s`: %s",
            len(normalized),
            TICKS_TABLE,
            e,
        )
        raise


def get_recent_ticks(
    client, symbol: str, limit: int = 500, provider: str | None = None
) -> list[dict]:
    """
    Return the most recent ``limit`` ticks for ``symbol`` (optionally scoped
    to a single ``provider``) in chronological order.

    Each dict: ``{bid, ask, spread, digits, time_str}`` where ``time_str`` is
    the server-formatted ``toString(timestamp)`` value. When ``provider`` is
    given, ``AND provider = {prov:String}`` is added to the WHERE clause.

    Returns ``[]`` when the table does not exist yet (e.g. schema creation is
    blocked by missing grants) or the query fails — tick reads are best-effort
    and must never crash the caller.
    """
    where = "symbol = {sym:String}"
    parameters: dict[str, object] = {"sym": symbol, "lim": limit}
    if provider:
        where += " AND provider = {prov:String}"
        parameters["prov"] = provider
    sql = (
        f"SELECT bid, ask, spread, digits, toString(timestamp) AS time_str "
        f"FROM {TICKS_TABLE} "
        f"WHERE {where} "
        f"ORDER BY timestamp DESC "
        f"LIMIT {{lim:UInt32}}"
    )
    try:
        result = client.query(sql, parameters=parameters)
    except Exception as e:  # noqa: BLE001 — best-effort read, never crash
        logger.warning(
            "Could not read recent ticks for %s from `%s`: %s",
            symbol,
            TICKS_TABLE,
            e,
        )
        return []
    columns = result.column_names
    # Query returns newest-first; reverse for chronological order.
    return [dict(zip(columns, row)) for row in reversed(result.result_set)]


def get_ohlc_bars(
    client,
    symbol: str,
    timeframe_minutes: int,
    limit: int = 100,
    provider: str | None = None,
) -> list[dict]:
    """
    Return OHLC bars aggregated from bid ticks over ``timeframe_minutes``
    intervals (optionally scoped to a single ``provider``), in chronological
    order.

    Each dict: ``{time, open, high, low, close}`` where ``time`` is an ISO-8601
    UTC string of the bar's interval start. Interval alignment follows the
    server timezone (ClickHouse ``toStartOfInterval`` default) — the server is
    expected to run UTC.

    Returns ``[]`` when the table does not exist yet (e.g. schema creation is
    blocked by missing grants) or the query fails — OHLC reads are best-effort
    and must never crash the caller.
    """
    where = "symbol = {sym:String}"
    parameters: dict[str, object] = {
        "tf": timeframe_minutes,
        "sym": symbol,
        "lim": limit,
    }
    if provider:
        where += " AND provider = {prov:String}"
        parameters["prov"] = provider
    sql = (
        f"SELECT toStartOfInterval(timestamp, INTERVAL {{tf:UInt32}} MINUTE) "
        f"AS bar_time, "
        f"argMin(bid, timestamp) AS open, max(bid) AS high, min(bid) AS low, "
        f"argMax(bid, timestamp) AS close "
        f"FROM {TICKS_TABLE} "
        f"WHERE {where} "
        f"GROUP BY bar_time "
        f"ORDER BY bar_time DESC "
        f"LIMIT {{lim:UInt32}}"
    )
    try:
        result = client.query(sql, parameters=parameters)
    except Exception as e:  # noqa: BLE001 — best-effort read, never crash
        logger.warning(
            "Could not read OHLC bars for %s from `%s`: %s",
            symbol,
            TICKS_TABLE,
            e,
        )
        return []
    bars = []
    for bar_time, open_, high, low, close in reversed(result.result_set):
        bars.append(
            {
                "time": (
                    bar_time.isoformat()
                    if hasattr(bar_time, "isoformat")
                    else str(bar_time)
                ),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    return bars


def get_daily_pnl(
    client, account_id: int, provider: str | None = None
) -> float:
    """
    Return today's realized PnL for ``account_id``, optionally scoped to a
    single ``provider`` (adds ``AND provider = {prov:String}`` to the
    trade-history query; ``account_id`` stays ``UInt64``).

    Returns ``0.0`` when there are no trades today or when the query fails
    (best-effort — a PnL read must never crash the caller).
    """
    where = (
        "account_id = {acc:UInt64} "
        "AND toYYYYMMDD(timestamp) = toYYYYMMDD(now())"
    )
    parameters: dict[str, object] = {"acc": account_id}
    if provider:
        where += " AND provider = {prov:String}"
        parameters["prov"] = provider
    sql = f"SELECT sum(realized_pnl) FROM {TRADE_HISTORY_TABLE} WHERE {where}"
    try:
        result = client.query(sql, parameters=parameters)
        value = result.result_set[0][0]
        return float(value) if value is not None else 0.0
    except Exception as e:  # noqa: BLE001 — PnL is best-effort
        logger.warning(
            "Could not compute daily PnL for account %s: %s", account_id, e
        )
        return 0.0