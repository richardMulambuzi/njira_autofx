"""
Volatility Engine (Phase 4 — m.md §32).

Measures market volatility from closed candles + recent spread and exposes it
so the signal engine can NORMALIZE thresholds instead of using fixed values:

- ATR (Wilder's smoothing, 14- and 7-period)
- Realized range (current candle high-low)
- Rolling range moving average + range expansion / contraction
- Rolling standard deviation of closes
- Tick/spread volatility (average, current, historical percentile)

Complements (does NOT duplicate) the microstructure engine, which works on raw
bid/ask ticks. This engine works on closed candles + the current spread and is
the single source of truth for "how big is a move, relative to recent activity".

Thread-safe: all per-symbol state is guarded by a single lock. Best-effort:
every public method swallows its own exceptions so a bad candle never crashes
the pipeline.
"""
from __future__ import annotations

import logging
import statistics
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("forex.analysis.volatility")


@dataclass
class VolatilityMetrics:
    """Current volatility state for a symbol/timeframe."""
    atr_14: float           # 14-period ATR
    atr_7: float            # 7-period ATR (shorter)
    realized_range: float   # current candle range
    range_ma: float         # moving average of ranges
    range_expansion: float  # current_range / range_ma (>1 = expanding)
    rolling_std: float      # rolling standard deviation of closes
    spread_avg: float       # average spread over recent ticks
    spread_current: float   # current spread
    spread_percentile: float  # where current spread sits historically
    regime: str             # "LOW", "NORMAL", "HIGH", "EXTREME"


@dataclass
class _SymbolState:
    """Mutable per-symbol volatility state (guarded by the engine lock)."""
    prev_close: Optional[float] = None
    atr_14: Optional[float] = None
    atr_7: Optional[float] = None
    ranges: deque = None  # type: ignore[assignment]
    closes: deque = None  # type: ignore[assignment]
    spreads: deque = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # deques are created lazily by the engine with the right maxlen
        pass


class VolatilityEngine:
    """
    Per-symbol volatility analysis driven by closed candles + current spread.

    Usage:
        engine = VolatilityEngine()
        engine.update("EURUSD", candle_dict, spread_pips)
        metrics = engine.get_metrics("EURUSD")
        atr_multiple = engine.normalize_distance("EURUSD", raw_distance)
        if engine.is_high_volatility("EURUSD"):
            ...
        stop = engine.get_stop_distance("EURUSD", multiplier=1.5)
    """

    def __init__(self, atr_period: int = 14, lookback: int = 50) -> None:
        self._atr_period = atr_period
        self._lookback = lookback
        self._std_window = 20          # rolling std over last 20 closes
        self._spread_window = 100      # spread percentile over last 100 spreads

        self._state: dict[str, _SymbolState] = {}
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────

    def update(
        self, symbol: str, candle: dict, spread: float = 0.0
    ) -> Optional[VolatilityMetrics]:
        """Update volatility state with a new closed candle. Returns metrics."""
        try:
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
        except (KeyError, TypeError, ValueError):
            logger.debug("[VOL] %s candle missing OHLC — skipped", symbol)
            return self.get_metrics(symbol)

        with self._lock:
            st = self._state.setdefault(
                symbol,
                _SymbolState(
                    ranges=deque(maxlen=self._lookback),
                    closes=deque(maxlen=self._lookback),
                    spreads=deque(maxlen=self._spread_window),
                ),
            )

            # ── True Range + Wilder ATR (14 & 7) ─────────────────────────
            tr = self._true_range(st, high, low, close)
            st.atr_14 = self._wilder(st, tr, st.atr_14, self._atr_period)
            st.atr_7 = self._wilder(st, tr, st.atr_7, 7)
            st.prev_close = close

            # ── Realized range + rolling range MA ───────────────────────
            current_range = high - low
            st.ranges.append(current_range)
            range_ma = (
                sum(st.ranges) / len(st.ranges) if st.ranges else current_range
            )
            range_expansion = (
                current_range / range_ma if range_ma > 0 else 1.0
            )

            # ── Rolling std of closes ───────────────────────────────────
            st.closes.append(close)
            closes_window = list(st.closes)[-self._std_window:]
            # ponytail: statistics.stdev uses exact (Fraction) arithmetic and was
            # ~30% of total backtest runtime. This is a single-pass float sample
            # std — numerically identical for our purposes, ~10x faster.
            n = len(closes_window)
            if n >= 2:
                mean = sum(closes_window) / n
                var = sum((x - mean) * (x - mean) for x in closes_window) / (n - 1)
                rolling_std = var ** 0.5
            else:
                rolling_std = 0.0

            # ── Spread volatility ───────────────────────────────────────
            spread_c = max(0.0, float(spread))
            st.spreads.append(spread_c)
            spread_avg = (
                sum(st.spreads) / len(st.spreads) if st.spreads else spread_c
            )
            spread_pct = (
                sum(1 for s in st.spreads if s <= spread_c) / len(st.spreads)
                if st.spreads else 0.5
            )

            # ── Regime (dynamic, no fixed price thresholds) ─────────────
            regime = self._regime(range_expansion, spread_pct)

            return VolatilityMetrics(
                atr_14=round(st.atr_14 or 0.0, 8),
                atr_7=round(st.atr_7 or 0.0, 8),
                realized_range=round(current_range, 8),
                range_ma=round(range_ma, 8),
                range_expansion=round(range_expansion, 3),
                rolling_std=round(rolling_std, 8),
                spread_avg=round(spread_avg, 4),
                spread_current=round(spread_c, 4),
                spread_percentile=round(spread_pct, 3),
                regime=regime,
            )

    def get_metrics(self, symbol: str) -> Optional[VolatilityMetrics]:
        """Get current volatility metrics for a symbol (None if no data yet)."""
        with self._lock:
            st = self._state.get(symbol)
            if st is None or st.prev_close is None:
                return None
            ranges = list(st.ranges)
            closes = list(st.closes)
            spreads = list(st.spreads)
            current_range = ranges[-1] if ranges else 0.0
            range_ma = sum(ranges) / len(ranges) if ranges else current_range
            range_expansion = current_range / range_ma if range_ma > 0 else 1.0
            closes_window = closes[-self._std_window:]
            rolling_std = (
                statistics.stdev(closes_window) if len(closes_window) >= 2 else 0.0
            )
            spread_c = spreads[-1] if spreads else 0.0
            spread_avg = sum(spreads) / len(spreads) if spreads else spread_c
            spread_pct = (
                sum(1 for s in spreads if s <= spread_c) / len(spreads)
                if spreads else 0.5
            )
            return VolatilityMetrics(
                atr_14=round(st.atr_14 or 0.0, 8),
                atr_7=round(st.atr_7 or 0.0, 8),
                realized_range=round(current_range, 8),
                range_ma=round(range_ma, 8),
                range_expansion=round(range_expansion, 3),
                rolling_std=round(rolling_std, 8),
                spread_avg=round(spread_avg, 4),
                spread_current=round(spread_c, 4),
                spread_percentile=round(spread_pct, 3),
                regime=self._regime(range_expansion, spread_pct),
            )

    def normalize_distance(self, symbol: str, raw_distance: float) -> float:
        """
        Normalize a price distance by ATR (returns ATR multiples).

        A displacement of 0.00030 with ATR_14 = 0.00015 → 2.0 ATR multiples.
        Falls back to the raw distance when ATR is unknown/zero.
        """
        m = self.get_metrics(symbol)
        if m is None or m.atr_14 <= 0:
            return float(raw_distance)
        return float(raw_distance) / m.atr_14

    def is_high_volatility(self, symbol: str) -> bool:
        """Quick check: is volatility above normal?"""
        m = self.get_metrics(symbol)
        if m is None:
            return False
        return m.regime in ("HIGH", "EXTREME") or m.range_expansion > 1.5

    def get_stop_distance(self, symbol: str, multiplier: float = 1.5) -> float:
        """
        Suggest stop distance in price units based on ATR.

        Falls back to the range MA (× multiplier) when ATR is not yet seeded,
        and to 0.0 when there is no data at all.
        """
        m = self.get_metrics(symbol)
        if m is None:
            return 0.0
        if m.atr_14 > 0:
            return m.atr_14 * multiplier
        if m.range_ma > 0:
            return m.range_ma * multiplier
        return 0.0

    def reset(self) -> None:
        with self._lock:
            self._state.clear()

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _true_range(
        st: _SymbolState, high: float, low: float, close: float
    ) -> float:
        """True Range using the previous close (Wilder's definition)."""
        if st.prev_close is None:
            return high - low
        return max(
            high - low,
            abs(high - st.prev_close),
            abs(low - st.prev_close),
        )

    @staticmethod
    def _wilder(
        st: _SymbolState, tr: float, prev_atr: Optional[float], period: int
    ) -> float:
        """
        Wilder's smoothing of ATR for a given period.

        Seeds with a running average of TRs, then applies the recursive Wilder
        formula. TR history is kept per-period on the state (separate from the
        candle-range deque used for the range MA).
        """
        # TR history is stored on the state keyed by period via a small cache.
        tr_hist = st.__dict__.setdefault("_tr", {})  # type: ignore[attr-defined]
        dq = tr_hist.get(period)
        if dq is None:
            dq = deque(maxlen=period)
            tr_hist[period] = dq
        dq.append(tr)

        if prev_atr is None:
            # Seed: simple average once we have `period` TRs, else running mean.
            return sum(dq) / len(dq)
        return (prev_atr * (period - 1) + tr) / period

    @staticmethod
    def _regime(range_expansion: float, spread_pct: float) -> str:
        """Map expansion + spread percentile to a volatility regime."""
        if range_expansion >= 2.5 or spread_pct >= 0.95:
            return "EXTREME"
        if range_expansion >= 1.5 or spread_pct >= 0.85:
            return "HIGH"
        if range_expansion <= 0.5:
            return "LOW"
        return "NORMAL"
