"""
ClickHouse schema and helpers for persisting Volume Profile data (improve.md §77).

Three tables hold the computed profiles, their price bins, and the discrete
events (POC/VAH/VAL breaches, HVN/LVN touches) used by the volume-profile
engine for backtesting and live monitoring.

All DDL is idempotent (``CREATE TABLE IF NOT EXISTS``) and every failure —
including the verified ``ACCESS_DENIED`` (error 497) on the ``admin`` user —
is logged and swallowed so the API never crashes over schema issues. This
mirrors the defensive pattern in ``forex.clickhouse.schema``.

Table names are environment-driven (fully qualified ``<database>.<table>``)
and default to the ``default`` database, exactly like the other CH tables.

Query helpers use clickhouse-connect's ``parameters=`` binding (never
``settings=``) for safe value substitution. Table names are trusted
environment configuration and are interpolated directly into the SQL — they
are never user input.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from forex.clickhouse.connection import CLICKHOUSE_DATABASE
from forex.clickhouse.schema import _is_access_denied

logger = logging.getLogger("forex.analysis.volume_profile_clickhouse")

# ---------------------------------------------------------------------------
# Environment-driven table names (fully qualified: <database>.<table>)
# ---------------------------------------------------------------------------
VOLUME_PROFILES_TABLE = os.environ.get(
    "CLICKHOUSE_VOLUME_PROFILES_TABLE", "default.volume_profiles"
)
VOLUME_PROFILE_BINS_TABLE = os.environ.get(
    "CLICKHOUSE_VOLUME_PROFILE_BINS_TABLE", "default.volume_profile_bins"
)
VOLUME_PROFILE_EVENTS_TABLE = os.environ.get(
    "CLICKHOUSE_VOLUME_PROFILE_EVENTS_TABLE", "default.volume_profile_events"
)

# ---------------------------------------------------------------------------
# DDL (idempotent MergeTree tables)
# ---------------------------------------------------------------------------
_VOLUME_PROFILES_DDL = f"""
CREATE TABLE IF NOT EXISTS {VOLUME_PROFILES_TABLE} (
    profile_id   String DEFAULT generateUUIDv4(),
    symbol       LowCardinality(String),
    profile_type LowCardinality(String),
    timeframe    UInt32,
    window_start DateTime64(3, 'UTC'),
    window_end   DateTime64(3, 'UTC'),
    bin_size     Float64,
    methodology  LowCardinality(String),
    total_activity UInt64,
    poc          Float64,
    vah          Float64,
    val          Float64,
    profile_high Float64,
    profile_low  Float64,
    hvn_count    UInt16,
    lvn_count    UInt16,
    profile_shape LowCardinality(String),
    entropy      Float32,
    vwap         Float64,
    developing   UInt8,
    reliability  Float32,
    data_complete UInt8,
    calculated_at DateTime64(3, 'UTC'),
    created_at   DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(window_start)
ORDER BY (symbol, profile_type, window_start, methodology)
TTL window_start + INTERVAL 90 DAY DELETE;
"""

_VOLUME_PROFILE_BINS_DDL = f"""
CREATE TABLE IF NOT EXISTS {VOLUME_PROFILE_BINS_TABLE} (
    profile_id   String,
    symbol       LowCardinality(String),
    price        Float64,
    activity     UInt64,
    relative_activity Float32,
    percentage   Float32,
    delta        Int64,
    is_poc       UInt8,
    is_hvn       UInt8,
    is_lvn       UInt8,
    created_at   DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (profile_id, price);
"""

_VOLUME_PROFILE_EVENTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {VOLUME_PROFILE_EVENTS_TABLE} (
    event_id     String DEFAULT generateUUIDv4(),
    symbol       LowCardinality(String),
    event_type   LowCardinality(String),
    profile_type LowCardinality(String),
    price        Float64,
    old_value    Nullable(Float64),
    new_value    Nullable(Float64),
    evidence     String,
    confidence   Float32,
    timestamp    DateTime64(3, 'UTC')
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp);
"""


def _ensure_profile_table(client, table: str, ddl: str) -> None:
    """Idempotently create ``table``; log and swallow any failure (never crash)."""
    try:
        client.command(ddl)
        logger.info("ClickHouse table `%s` ensured", table)
    except Exception as e:  # noqa: BLE001 — DDL must never crash the pipeline
        if _is_access_denied(e):
            logger.error(
                "ClickHouse ACCESS_DENIED creating `%s` — user '%s' lacks "
                "CREATE TABLE grants; schema will be created once grants are "
                "fixed. Error: %s",
                table,
                os.environ.get("CLICKHOUSE_USER", "admin"),
                e,
            )
        else:
            logger.warning("Could not ensure ClickHouse table `%s`: %s", table, e)


def ensure_profile_schema(client) -> None:
    """
    Idempotently create the volume_profiles, volume_profile_bins, and
    volume_profile_events tables. Call once on startup.
    """
    for table, ddl in (
        (VOLUME_PROFILES_TABLE, _VOLUME_PROFILES_DDL),
        (VOLUME_PROFILE_BINS_TABLE, _VOLUME_PROFILE_BINS_DDL),
        (VOLUME_PROFILE_EVENTS_TABLE, _VOLUME_PROFILE_EVENTS_DDL),
    ):
        _ensure_profile_table(client, table, ddl)


# ---------------------------------------------------------------------------
# Insert helpers (best-effort; log + swallow, never crash the caller)
# ---------------------------------------------------------------------------

def insert_profile_batch(client, profiles: list[dict]) -> None:
    """
    Batch insert computed profiles into the volume_profiles table.

    ``profiles`` is a list of dicts whose keys match the table columns.
    Failures are logged and swallowed (best-effort persistence).
    """
    if not profiles:
        return
    try:
        client.insert(
            VOLUME_PROFILES_TABLE,
            profiles,
            column_names=[
                "profile_id", "symbol", "profile_type", "timeframe",
                "window_start", "window_end", "bin_size", "methodology",
                "total_activity", "poc", "vah", "val",
                "profile_high", "profile_low", "hvn_count", "lvn_count",
                "profile_shape", "entropy", "vwap", "developing",
                "reliability", "data_complete", "calculated_at",
            ],
        )
    except Exception as e:  # noqa: BLE001 — best-effort persistence
        if _is_access_denied(e):
            logger.warning(
                "ClickHouse ACCESS_DENIED inserting profiles into `%s` — "
                "user lacks INSERT grants.", VOLUME_PROFILES_TABLE,
            )
        else:
            logger.warning(
                "Failed to insert %d profiles into `%s`: %s",
                len(profiles), VOLUME_PROFILES_TABLE, e,
            )


def insert_profile_bins_batch(client, bins: list[dict]) -> None:
    """
    Batch insert price bins into the volume_profile_bins table.

    ``bins`` is a list of dicts whose keys match the table columns.
    Failures are logged and swallowed (best-effort persistence).
    """
    if not bins:
        return
    try:
        client.insert(
            VOLUME_PROFILE_BINS_TABLE,
            bins,
            column_names=[
                "profile_id", "symbol", "price", "activity",
                "relative_activity", "percentage", "delta",
                "is_poc", "is_hvn", "is_lvn",
            ],
        )
    except Exception as e:  # noqa: BLE001 — best-effort persistence
        if _is_access_denied(e):
            logger.warning(
                "ClickHouse ACCESS_DENIED inserting bins into `%s` — "
                "user lacks INSERT grants.", VOLUME_PROFILE_BINS_TABLE,
            )
        else:
            logger.warning(
                "Failed to insert %d bins into `%s`: %s",
                len(bins), VOLUME_PROFILE_BINS_TABLE, e,
            )


def insert_profile_event(client, event: dict) -> None:
    """
    Insert a single profile event into the volume_profile_events table.

    ``event`` is a dict whose keys match the table columns.
    Failures are logged and swallowed (best-effort persistence).
    """
    try:
        client.insert(
            VOLUME_PROFILE_EVENTS_TABLE,
            [event],
            column_names=[
                "event_id", "symbol", "event_type", "profile_type",
                "price", "old_value", "new_value", "evidence",
                "confidence", "timestamp",
            ],
        )
    except Exception as e:  # noqa: BLE001 — best-effort persistence
        if _is_access_denied(e):
            logger.warning(
                "ClickHouse ACCESS_DENIED inserting event into `%s` — "
                "user lacks INSERT grants.", VOLUME_PROFILE_EVENTS_TABLE,
            )
        else:
            logger.warning(
                "Failed to insert event into `%s`: %s",
                VOLUME_PROFILE_EVENTS_TABLE, e,
            )


# ---------------------------------------------------------------------------
# Read helpers (best-effort; return None / [] on error, never crash)
# ---------------------------------------------------------------------------

def get_latest_profile(
    client, symbol: str, methodology: str = "tick_count"
) -> Optional[dict]:
    """
    Return the most recent non-developing profile for ``symbol`` / ``methodology``
    as a dict, or ``None`` if none exists or the query fails.
    """
    sql = (
        f"SELECT profile_id, symbol, profile_type, timeframe, window_start, "
        f"window_end, bin_size, methodology, total_activity, poc, vah, val, "
        f"profile_high, profile_low, hvn_count, lvn_count, profile_shape, "
        f"entropy, vwap, developing, reliability, data_complete, calculated_at "
        f"FROM {VOLUME_PROFILES_TABLE} "
        f"WHERE symbol = {{sym:String}} AND methodology = {{meth:String}} "
        f"AND developing = 0 "
        f"ORDER BY window_start DESC "
        f"LIMIT 1"
    )
    try:
        result = client.query(
            sql, parameters={"sym": symbol, "meth": methodology}
        )
    except Exception as e:  # noqa: BLE001 — best-effort read, never crash
        logger.warning(
            "Could not read latest profile for %s from `%s`: %s",
            symbol, VOLUME_PROFILES_TABLE, e,
        )
        return None
    if not result.result_set:
        return None
    return dict(zip(result.column_names, result.result_set[0]))


def get_profile_history(
    client,
    symbol: str,
    start: str,
    end: str,
    profile_type: str = "daily",
) -> list[dict]:
    """
    Return historical profiles for ``symbol`` within ``[start, end]`` (ISO-8601
    strings) of ``profile_type``, in chronological order, for backtesting.

    Returns ``[]`` when no rows match or the query fails (best-effort).
    """
    sql = (
        f"SELECT profile_id, symbol, profile_type, timeframe, window_start, "
        f"window_end, bin_size, methodology, total_activity, poc, vah, val, "
        f"profile_high, profile_low, hvn_count, lvn_count, profile_shape, "
        f"entropy, vwap, developing, reliability, data_complete, calculated_at "
        f"FROM {VOLUME_PROFILES_TABLE} "
        f"WHERE symbol = {{sym:String}} AND profile_type = {{pt:String}} "
        f"AND window_start BETWEEN toDateTime64({{start:String}}, 3, 'UTC') "
        f"AND toDateTime64({{end:String}}, 3, 'UTC') "
        f"ORDER BY window_start ASC"
    )
    try:
        result = client.query(
            sql,
            parameters={"sym": symbol, "pt": profile_type, "start": start, "end": end},
        )
    except Exception as e:  # noqa: BLE001 — best-effort read, never crash
        logger.warning(
            "Could not read profile history for %s from `%s`: %s",
            symbol, VOLUME_PROFILES_TABLE, e,
        )
        return []
    return [dict(zip(result.column_names, row)) for row in result.result_set]
