"""
Shared 1-minute candle backfill core (improve.md §2 / Phase 3 continuity).

Backward-paged tick fetch -> aggregate to 1M OHLC -> validate -> idempotent
insert into ClickHouse. Extracted from ``management/commands/backfill_history.py``
so the one-shot backfill command AND the startup ``ensure_continuity`` gap-filler
share a single implementation (no duplication).

The cTrader trendbars API IGNORES ``fromTimestamp`` and returns the last ``limit``
bars ending at ``toTimestamp``. Forward paging therefore returned the same window
every iteration. The API DOES honor ``toTimestamp``, so we walk backward: each
page's oldest tick becomes the new upper bound minus 1us, pulling the next-older
window. Pages are collected, then aggregated in reverse so ticks arrive in
globally-ascending order (correct OHLC open/close/high/low).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from forex.broker.errors import BrokerError
from forex.clickhouse.schema import CANDLES_TABLE, insert_candle_batch

logger = logging.getLogger("forex.analysis.backfill")

TF_1M = 60
GAP_TOLERANCE_S = 5  # allow a few seconds of drift beyond the 60s 1M spacing
MAX_PAGES = 2_000_000  # hard loop guard (defensive)


def backfill_1m_range(
    client,
    provider,
    symbol: str,
    start_epoch: int,
    end_epoch: int,
    page: int = 1000,
    batch: int = 5000,
) -> dict:
    """Backfill 1M OHLC candles for ``symbol`` over [start_epoch, end_epoch].

    Fetches ticks via ``provider.get_ticks_range`` (windowed, backward-paged),
    aggregates them to 1-minute candles (bounded memory), validates OHLC, and
    persists via ``insert_candle_batch`` (idempotent: existing open_times are
    skipped). Returns a summary dict:

        {fetched, inserted, skipped_duplicates, invalid, gaps, oldest, newest}
    """
    fetched = 0
    inserted = 0
    skipped_dup = 0
    invalid = 0
    gaps = 0
    oldest = None
    newest = None

    pending: list[dict] = []
    cur_minute = None
    cur = None
    prev_built = None

    def finalize(bucket: dict) -> None:
        nonlocal skipped_dup, invalid, gaps, oldest, newest, prev_built
        m = bucket["_epoch"]
        # monotonic + in-run duplicate detection
        if prev_built is not None and m <= prev_built:
            skipped_dup += 1
            logger.debug("Out-of-order/duplicate minute %s for %s", m, symbol)
            return
        # within range
        if m < start_epoch or m > end_epoch:
            invalid += 1
            return
        o, h, l, c = bucket["open"], bucket["high"], bucket["low"], bucket["close"]
        if not (h >= max(o, c) and l <= min(o, c) and h >= l and o > 0 and c > 0):
            invalid += 1
            logger.warning(
                "Invalid OHLC for %s @ %s: O=%s H=%s L=%s C=%s",
                symbol, _iso(m), o, h, l, c,
            )
            return
        # gap detection (missing 1M intervals)
        if prev_built is not None:
            delta = m - prev_built
            if delta > 60 + GAP_TOLERANCE_S:
                gaps += 1
                missing = (delta // 60) - 1
                logger.info(
                    "Gap for %s: %d missing 1M candle(s) between %s and %s",
                    symbol, missing, _iso(prev_built), _iso(m),
                )
        prev_built = m
        if oldest is None or m < oldest:
            oldest = m
        if newest is None or m > newest:
            newest = m
        pending.append(bucket)

    def flush() -> None:
        nonlocal inserted, skipped_dup
        if not pending:
            return
        lo = min(c["_epoch"] for c in pending)
        hi = max(c["_epoch"] for c in pending)
        existing = _existing_open_times(client, symbol, lo, hi)
        to_insert = []
        for c in pending:
            if c["_epoch"] in existing:
                skipped_dup += 1
            else:
                to_insert.append(c)
        if to_insert:
            insert_candle_batch(client, to_insert)
            inserted += len(to_insert)
        pending.clear()

    # ---- windowed, paged fetch (BACKWARD paging) ----
    window_start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_epoch, tz=timezone.utc)
    iterations = 0
    pages: list = []
    while end_dt > window_start and iterations < MAX_PAGES:
        iterations += 1
        page_rows = _fetch_page(provider, symbol, window_start, end_dt, page)
        if not page_rows:
            break
        fetched += len(page_rows)
        pages.append(page_rows)
        oldest_ts = page_rows[0].timestamp
        if oldest_ts.tzinfo is None:
            oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)
        end_dt = oldest_ts - timedelta(microseconds=1)

    # ---- aggregate collected pages in globally-ascending order ----
    for page_rows in reversed(pages):
        for tick in page_rows:
            ts = tick.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            epoch_s = int(ts.timestamp())
            minute = epoch_s - (epoch_s % 60)
            if minute != cur_minute:
                if cur is not None:
                    finalize(cur)
                cur_minute = minute
                cur = _new_bucket(symbol, minute)
            bid = float(tick.bid)
            cur["_n"] += 1
            if cur["_n"] == 1:
                cur["open"] = bid
            cur["high"] = max(cur["high"], bid)
            cur["low"] = min(cur["low"], bid)
            cur["close"] = bid
            cur["volume_ticks"] = cur["_n"]

        if len(pending) >= batch:
            flush()

    if cur is not None:
        finalize(cur)
        cur = None
    flush()

    return {
        "fetched": fetched,
        "inserted": inserted,
        "skipped_duplicates": skipped_dup,
        "invalid": invalid,
        "gaps": gaps,
        "oldest": oldest,
        "newest": newest,
    }


def _fetch_page(provider, symbol, window_start, end_dt, page) -> list:
    """Fetch one page of ticks, retrying transient BrokerError timeouts.

    Retries the SAME page up to 3 attempts (2 retries) with a 5s sleep
    between attempts. After 3 failures the last BrokerError is re-raised so
    the caller can decide how to handle the symbol-level failure.
    """
    last_exc = None
    for attempt in range(3):
        try:
            return provider.get_ticks_range(symbol, window_start, end_dt, limit=page)
        except BrokerError as exc:
            last_exc = exc
            if attempt < 2:
                logger.warning(
                    "get_ticks_range transient error for %s (attempt %d/3): %s "
                    "— retrying in 5s",
                    symbol, attempt + 1, exc,
                )
                time.sleep(5)
                continue
            logger.warning(
                "get_ticks_range failed for %s after 3 attempts: %s",
                symbol, exc,
            )
            raise
    # Unreachable: the loop always raises on the final attempt.
    raise last_exc  # pragma: no cover


def _new_bucket(symbol: str, minute_epoch: int) -> dict:
    open_time = datetime.fromtimestamp(minute_epoch, tz=timezone.utc)
    return {
        "symbol": symbol,
        "timeframe": TF_1M,
        "open_time": open_time,
        "close_time": open_time + timedelta(minutes=1),
        "open": 0.0,
        "high": float("-inf"),
        "low": float("inf"),
        "close": 0.0,
        "volume_ticks": 0,
        "is_closed": True,
        "gap_filled": False,
        "_epoch": minute_epoch,
        "_n": 0,
    }


def _existing_open_times(client, symbol: str, lo_epoch: int, hi_epoch: int) -> set:
    """Return the set of already-persisted 1M open_time epochs for the symbol
    in [lo_epoch, hi_epoch]. Used for idempotent inserts."""
    sql = (
        f"SELECT toUnixTimestamp(open_time) FROM {CANDLES_TABLE} "
        f"WHERE symbol = {{sym:String}} AND timeframe = {TF_1M} "
        f"AND toUnixTimestamp(open_time) BETWEEN {{lo:UInt32}} AND {{hi:UInt32}}"
    )
    try:
        rows = client.query(
            sql, parameters={"sym": symbol, "lo": lo_epoch, "hi": hi_epoch}
        ).result_set
    except Exception as exc:  # noqa: BLE001 — best-effort dedup
        logger.warning("Existing-candle query failed for %s: %s", symbol, exc)
        return set()
    return {int(r[0]) for r in rows if r and r[0] is not None}


def _iso(epoch_s: int | None) -> str:
    if epoch_s is None:
        return "n/a"
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
