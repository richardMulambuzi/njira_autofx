"""
Startup history preload and state reconciliation.

On boot, loads required historical candles from ClickHouse into the hot
cache so the analytical engines can function immediately without waiting
for live candles to accumulate.

Also reconciles the live tick stream against the latest persisted candle
to detect any gaps that occurred while the engine was offline.

Phase 2.6 — improvements.md §22.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forex.analysis.cache import HotCache
    from forex.analysis.requirements import TimeframeRequirement

logger = logging.getLogger("forex.analysis.startup")


def preload_history(
    client,
    cache: "HotCache",
    symbols: list[str],
    requirements: list["TimeframeRequirement"],
) -> dict[str, dict[int, int]]:
    """
    Load historical candles from ClickHouse into the hot cache.

    Parameters
    ----------
    client:
        ClickHouse client.
    cache:
        The hot cache instance to populate.
    symbols:
        List of symbol names to preload.
    requirements:
        List of TimeframeRequirement (from ``requirements.py``).

    Returns
    -------
    dict mapping {symbol: {timeframe: candles_loaded}} for observability.
    """
    from forex.clickhouse.schema import get_candles_from_ch

    stats: dict[str, dict[int, int]] = {}
    for symbol in symbols:
        stats[symbol] = {}
        for req in requirements:
            candles = get_candles_from_ch(
                client, symbol, req.timeframe_seconds, req.desired_candles
            )
            cache.add_batch(candles)
            stats[symbol][req.timeframe_seconds] = len(candles)
            if len(candles) < req.min_candles:
                logger.warning(
                    "[STARTUP] %s tf=%ds: only %d candles (need %d) — "
                    "analysis may be limited until more data accumulates",
                    symbol, req.timeframe_seconds, len(candles), req.min_candles,
                )
            else:
                logger.info(
                    "[STARTUP] %s tf=%ds: loaded %d candles",
                    symbol, req.timeframe_seconds, len(candles),
                )
    return stats


def reconcile_last_candle(
    client, symbol: str, timeframe: int
) -> dict | None:
    """
    Fetch the most recent closed candle from ClickHouse for reconciliation.

    Returns the candle dict, or None if no candles exist.
    Used by the tick pipeline to check if the live stream is ahead of, behind,
    or consistent with persisted data.
    """
    from forex.clickhouse.schema import get_candles_from_ch

    candles = get_candles_from_ch(client, symbol, timeframe, limit=1)
    return candles[0] if candles else None


def ensure_continuity(
    client,
    provider,
    symbol: str,
    timeframe: int = 60,
    max_gap_minutes: int = 5,
) -> dict:
    """
    Detect and backfill the gap between the last persisted candle and now.

    Phase 3 continuity (improve.md §48): on startup, ensure the live tick stream
    continues seamlessly from the last historical 1M candle with no
    discontinuity. The missing interval is backfilled with the SAME backward-paging
    + aggregation + idempotent-insert logic as ``backfill_history`` (shared via
    ``forex.analysis.backfill.backfill_1m_range``).

    Returns one of::

        {"status": "no_data", "symbol": ...}            # nothing persisted yet
        {"status": "fresh", "gap_minutes": N}           # gap within tolerance
        {"status": "backfilled", "symbol": ...,         # gap backfilled
         "backfilled_candles": N, "gap_minutes": G}
        {"status": "error", "reason": ...}              # best-effort, never crash

    Best-effort: any failure is logged and reported as ``error``; startup must
    never crash over a ClickHouse or provider hiccup.
    """
    last = reconcile_last_candle(client, symbol, timeframe)
    if last is None:
        return {"status": "no_data", "symbol": symbol}

    last_open = last["open_time"]
    if isinstance(last_open, datetime):
        if last_open.tzinfo is None:
            last_open = last_open.replace(tzinfo=timezone.utc)
    else:
        # Defensive: tolerate an epoch value if the contract ever changes.
        last_open = datetime.fromtimestamp(int(last_open), tz=timezone.utc)

    now = datetime.now(timezone.utc)
    gap_minutes = (now - last_open).total_seconds() / 60.0
    if gap_minutes <= max_gap_minutes:
        return {"status": "fresh", "gap_minutes": round(gap_minutes, 2)}

    # Gap detected — backfill the missing completed minutes. The in-progress
    # minute is owned by the live stream, so we stop one second before its
    # start to avoid inserting a partial candle that the live close would have
    # to overwrite.
    start_epoch = int(last_open.timestamp())
    now_epoch = int(now.timestamp())
    end_epoch = now_epoch - (now_epoch % 60) - 1
    if end_epoch < start_epoch:
        # Nothing completed to backfill; live stream owns the rest.
        return {"status": "fresh", "gap_minutes": round(gap_minutes, 2)}

    try:
        from forex.analysis.backfill import backfill_1m_range

        summary = backfill_1m_range(
            client, provider, symbol, start_epoch, end_epoch
        )
        return {
            "status": "backfilled",
            "symbol": symbol,
            "backfilled_candles": summary.get("inserted", 0),
            "gap_minutes": round(gap_minutes, 2),
        }
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash startup
        logger.warning("[CONTINUITY] backfill failed for %s: %s", symbol, exc)
        return {"status": "error", "reason": str(exc)}
