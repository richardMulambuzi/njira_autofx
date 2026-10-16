"""
Spread Engine (m.md §33 — Spread as a first-class market condition).

Tracks spread as an independent, per-symbol, historically-normalized
market condition rather than a single universal threshold:

- current spread
- rolling average / median spread
- spread percentile (where the current sits vs recent history)
- spread volatility (std dev)
- spread expansion rate (current avg vs previous avg)
- spread regime (TIGHT / NORMAL / WIDE / EXTREME)
- z-score (how many std devs from the mean)

Signals are downgraded or rejected when spread conditions are abnormal.
Normalization is per-symbol and relative to that symbol's own history, so a
"wide" spread for EURUSD is not the same as a "wide" spread for an exotic.

This module is intentionally focused on spread only. The microstructure
engine continues to own bid/ask imbalance, momentum and tick velocity; this
engine owns spread analytics and the confidence bonus/penalty scoring that
the signal engine consumes.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("forex.analysis.spread")


@dataclass
class SpreadMetrics:
    """Current spread state for a symbol (all values in pips)."""

    current: float  # current spread in pips
    average: float  # rolling average spread
    median: float  # rolling median spread
    percentile: float  # where current sits (0.0 to 1.0)
    std_dev: float  # spread volatility
    expansion_rate: float  # current avg vs previous avg (>1 = expanding)
    regime: str  # "TIGHT", "NORMAL", "WIDE", "EXTREME"
    z_score: float  # how many std devs from mean


class SpreadEngine:
    """
    Per-symbol spread analytics with bounded memory and thread safety.

    Usage:
        engine = SpreadEngine(lookback=200)
        engine.update("EURUSD", 0.8)        # feed each tick spread
        metrics = engine.get_metrics("EURUSD")
        if not engine.is_acceptable("EURUSD"):
            ...  # skip / downgrade signal
        bonus = engine.get_spread_bonus("EURUSD")     # +3 tight, 0 otherwise
        penalty = engine.get_spread_penalty("EURUSD")  # 0 / -2 / -5
    """

    def __init__(self, lookback: int = 200) -> None:
        self._lookback = max(1, int(lookback))
        self._spreads: dict[str, deque] = {}  # symbol → deque of spreads
        self._prev_avg: dict[str, float] = {}  # symbol → previous average
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def update(self, symbol: str, spread_pips: float) -> Optional[SpreadMetrics]:
        """Update with a new tick spread. Returns current metrics (best-effort)."""
        try:
            spread = float(spread_pips)
        except (TypeError, ValueError):
            logger.debug("SpreadEngine.update: bad spread for %s: %r", symbol, spread_pips)
            return None
        if spread < 0:
            spread = 0.0
        with self._lock:
            if symbol not in self._spreads:
                self._spreads[symbol] = deque(maxlen=self._lookback)
                self._prev_avg[symbol] = spread
            self._spreads[symbol].append(spread)
            metrics = self._compute(symbol)
            if metrics is not None:
                # Remember this average so the next tick can measure expansion.
                self._prev_avg[symbol] = metrics.average
            return metrics

    def get_metrics(self, symbol: str) -> Optional[SpreadMetrics]:
        """Get current spread metrics for a symbol (None if no data yet)."""
        with self._lock:
            return self._compute(symbol)

    def is_acceptable(self, symbol: str, max_percentile: float = 0.85) -> bool:
        """
        Is the spread within acceptable bounds?

        Per-symbol, history-relative: a symbol is acceptable when its current
        spread sits at or below ``max_percentile`` of its own recent
        distribution. No universal fixed threshold.
        """
        with self._lock:
            m = self._compute(symbol)
        if m is None:
            # Best-effort: without history we cannot judge — do not block.
            return True
        return m.percentile <= max_percentile

    def get_spread_penalty(self, symbol: str) -> float:
        """
        Confidence penalty from abnormal spread.

        0.0 = fine (TIGHT/NORMAL), -2.0 = WIDE, -5.0 = EXTREME.
        """
        with self._lock:
            m = self._compute(symbol)
        if m is None:
            return 0.0
        if m.regime == "EXTREME":
            return -5.0
        if m.regime == "WIDE":
            return -2.0
        return 0.0

    def get_spread_bonus(self, symbol: str) -> float:
        """
        Confidence bonus for a tight spread.

        0.0 = normal/wide/extreme, +3.0 = TIGHT (cheap to trade).
        """
        with self._lock:
            m = self._compute(symbol)
        if m is None:
            return 0.0
        if m.regime == "TIGHT":
            return 3.0
        return 0.0

    def reset(self) -> None:
        """Clear all per-symbol state."""
        with self._lock:
            self._spreads.clear()
            self._prev_avg.clear()

    # ── Internals ──────────────────────────────────────────────────────────

    def _regime(self, percentile: float, z_score: float) -> str:
        """Dynamic, per-symbol regime classification."""
        if percentile >= 0.95 or z_score >= 2.5:
            return "EXTREME"
        if percentile >= 0.80 or z_score >= 1.5:
            return "WIDE"
        if percentile <= 0.15 or z_score <= -1.0:
            return "TIGHT"
        return "NORMAL"

    def _compute(self, symbol: str) -> Optional[SpreadMetrics]:
        """
        Compute metrics from the current buffer (lock must be held by caller).
        """
        buf = self._spreads.get(symbol)
        if not buf:
            return None

        values = list(buf)
        n = len(values)
        current = values[-1]
        mean = sum(values) / n

        # Standard deviation (sample).
        if n > 1:
            var = sum((v - mean) ** 2 for v in values) / (n - 1)
            std_dev = var ** 0.5
        else:
            std_dev = 0.0

        # Median.
        sorted_v = sorted(values)
        mid = n // 2
        median = (
            (sorted_v[mid - 1] + sorted_v[mid]) / 2.0
            if n % 2 == 0
            else sorted_v[mid]
        )

        # Percentile: mid-rank ("where does current sit" in the recent
        # distribution). Using count_below + 0.5*count_equal (instead of a raw
        # "<=" count) keeps the percentile at 0.5 for a stable, low-variance
        # spread instead of letting float-noise ordering push it to ~1.0 and
        # falsely flagging a normal-for-that-symbol spread as EXTREME. This is
        # what makes per-symbol, history-relative normalization actually work.
        count_below = sum(1 for s in sorted_v if s < current)
        count_equal = sum(1 for s in sorted_v if s == current)
        percentile = (count_below + 0.5 * count_equal) / n

        # Z-score.
        z_score = (current - mean) / std_dev if std_dev > 0 else 0.0

        # Expansion rate: current average vs the previous average.
        prev_avg = self._prev_avg.get(symbol, mean)
        expansion_rate = mean / prev_avg if prev_avg > 0 else 1.0

        regime = self._regime(percentile, z_score)

        return SpreadMetrics(
            current=current,
            average=mean,
            median=median,
            percentile=percentile,
            std_dev=std_dev,
            expansion_rate=expansion_rate,
            regime=regime,
            z_score=z_score,
        )
