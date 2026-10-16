"""
Tick gap detection.

Monitors the tick stream per symbol and logs warnings when the time since
the last tick exceeds a configurable threshold.  Gaps are common during
market closures (weekends, holidays) and should be logged but not treated
as errors.

Phase 2.7 — improvements.md §23.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("forex.analysis.gap_detector")


class GapDetector:
    """
    Per-symbol gap detection.

    Parameters
    ----------
    threshold_seconds:
        If the gap between two consecutive ticks exceeds this value, a gap
        is logged.  Default 300s (5 minutes) — covers lunch-hour lulls
        without false positives on normal spread widening.
    """

    def __init__(self, threshold_seconds: float = 300.0) -> None:
        self._threshold = threshold_seconds
        self._last_ts: dict[str, float] = {}  # symbol → epoch seconds

    def check(self, symbol: str, timestamp_ms: int) -> bool:
        """
        Check for a gap.  Returns True if a gap was detected.

        Call this for every repaired tick before feeding to the candle engine.
        """
        ts_sec = timestamp_ms / 1000.0
        last = self._last_ts.get(symbol)
        self._last_ts[symbol] = ts_sec

        if last is None:
            return False  # first tick — no gap

        gap = ts_sec - last
        if gap > self._threshold:
            gap_min = gap / 60
            logger.warning(
                "[GAP] %s: %.1f min gap since last tick (%s → %s)",
                symbol,
                gap_min,
                datetime.fromtimestamp(last, tz=timezone.utc).strftime("%H:%M:%S"),
                datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%H:%M:%S"),
            )
            return True
        return False

    def reset(self) -> None:
        self._last_ts.clear()
