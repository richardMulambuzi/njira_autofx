"""
Per-symbol, multi-timeframe candle aggregation engine.

Consumes raw (or repaired) ticks and produces deterministic OHLC candles for
configurable timeframes.  Designed for both live streaming and historical
backtesting via the same interface.

Tick contract (what we receive):
    (symbol: str, bid: float, ask: float, spread: float, digits: int, timestamp_ms: int)

Candle close event (what we emit via callback):
    {"symbol": str, "timeframe": int, "open": float, "high": float,
     "low": float, "close": float, "volume_ticks": int,
     "open_time": datetime, "close_time": datetime, "is_closed": True}

No look-ahead bias:
    forming candle = ticks received so far only.
    closed candle   = emitted exactly once when the interval boundary is crossed.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("forex.analysis.candle_engine")

# ── Default timeframes (seconds) ────────────────────────────────────────
# Only 1-minute candles are persisted to ClickHouse.  Higher timeframes
# (15m, 1h, 4h) are aggregated on-the-fly from 1m candles via SQL or
# the hot cache — no need to store them separately.
DEFAULT_TIMEFRAMES = (60,)  # 1m only — higher TFs aggregated on demand


# ── Internal per-timeframe state ────────────────────────────────────────
@dataclass
class _CandleState:
    """Mutable forming candle + recent closed-candle history for one timeframe."""
    open_time: Optional[datetime] = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume_ticks: int = 0
    closed: deque = field(default_factory=lambda: deque(maxlen=500))

    def reset(self, ts: datetime, price: float) -> None:
        self.open_time = ts
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.volume_ticks = 1

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume_ticks += 1


# ── Public aggregator ──────────────────────────────────────────────────
class CandleAggregator:
    """
    Multi-timeframe candle aggregator for a single symbol.

    Parameters
    ----------
    symbol:
        Instrument name (e.g. ``"EURUSD"``).
    timeframes:
        Tuple of candle intervals in seconds.  Default: ``(60, 900, 3600, 14400)``.
    on_candle_close:
        Optional callback invoked with the closed-candle dict when an
        interval boundary is crossed.  Called synchronously from `feed_tick`;
        keep it fast (append to a queue, don't do I/O).
    max_closed:
        Maximum closed candles kept per timeframe in memory (for the hot
        cache / analytical access).  Oldest are evicted first.
    """

    def __init__(
        self,
        symbol: str,
        timeframes: tuple[int, ...] = DEFAULT_TIMEFRAMES,
        on_candle_close: Optional[Callable[[dict[str, Any]], None]] = None,
        max_closed: int = 500,
    ) -> None:
        self.symbol = symbol
        self.timeframes = timeframes
        self.on_candle_close = on_candle_close
        self._max_closed = max_closed
        self._states: dict[int, _CandleState] = {
            tf: _CandleState() for tf in timeframes
        }

    # ── Public API ──────────────────────────────────────────────────────

    def feed_tick(
        self, bid: float, ask: float, timestamp_ms: int
    ) -> list[dict[str, Any]]:
        """
        Process one tick and return a list of candles that closed as a result
        (usually 0 or 1 per timeframe, but a long gap can close multiple).

        Parameters
        ----------
        bid, ask:
            Raw prices (repaired, both > 0).
        timestamp_ms:
            cTrader epoch-millisecond timestamp.

        Returns
        -------
        list[dict]
            Closed candle dicts (is_closed=True).  Empty when no boundary was
            crossed.
        """
        ts = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        price = (bid + ask) / 2.0  # canonical mid-price

        closed_candles: list[dict[str, Any]] = []
        for tf in self.timeframes:
            closed = self._process_timeframe(tf, ts, price)
            if closed is not None:
                closed_candles.append(closed)
        return closed_candles

    def feed_batch(
        self, ticks: list[tuple[str, float, float, float, int, int]]
    ) -> list[dict[str, Any]]:
        """
        Backtesting / historical replay interface.

        Accepts a list of tick tuples (same shape as the live pipeline) and
        processes them in order, returning all candles that closed.

        Tuples where symbol != self.symbol are silently skipped.
        """
        closed: list[dict[str, Any]] = []
        for symbol, bid, ask, _spread, _digits, ts_ms in ticks:
            if symbol != self.symbol:
                continue
            if bid <= 0 or ask <= 0:
                continue
            closed.extend(self.feed_tick(bid, ask, ts_ms))
        return closed

    def get_forming(self, tf: int) -> Optional[dict[str, Any]]:
        """Return the current forming candle for ``tf`` seconds, or None."""
        state = self._states.get(tf)
        if state is None or state.open_time is None:
            return None
        return self._candle_dict(state, is_closed=False, tf=tf)

    def get_closed(self, tf: int, n: int = 0) -> list[dict[str, Any]]:
        """
        Return up to ``n`` most-recently closed candles for ``tf`` seconds.
        ``n=0`` returns all (up to ``max_closed``).
        """
        state = self._states.get(tf)
        if state is None:
            return []
        candles = list(state.closed)
        if n > 0:
            candles = candles[-n:]
        return candles

    def reset(self) -> None:
        """Clear all forming + closed state (e.g. between backtest runs)."""
        for state in self._states.values():
            state.open_time = None
            state.closed.clear()

    # ── Internal ────────────────────────────────────────────────────────

    def _floor_interval(self, ts: datetime, tf: int) -> datetime:
        """
        Floor ``ts`` to the start of the current interval of ``tf`` seconds.

        Deterministic: ``floor(ts, 300)`` at 14:37:22 UTC → 14:35:00 UTC.
        """
        epoch = ts.timestamp()
        floored_epoch = (int(epoch) // tf) * tf
        return datetime.fromtimestamp(floored_epoch, tz=timezone.utc)

    def _process_timeframe(
        self, tf: int, ts: datetime, price: float
    ) -> Optional[dict[str, Any]]:
        """
        Update forming candle for ``tf``.  Returns the closed candle dict
        when an interval boundary was crossed, else None.
        """
        state = self._states[tf]
        interval_start = self._floor_interval(ts, tf)

        # ── First tick ever → start forming ──
        if state.open_time is None:
            state.reset(interval_start, price)
            return None

        # ── Still in the same interval → update forming ──
        if interval_start == state.open_time:
            state.update(price)
            return None

        # ── Boundary crossed → close previous, maybe catch up on gaps ──
        closed_candle: Optional[dict[str, Any]] = None

        # Close the forming candle ONCE with correct close_time.
        close_time = datetime.fromtimestamp(
            state.open_time.timestamp() + tf, tz=timezone.utc
        )
        closed_candle = self._close_and_emit(state, tf, close_time)

        # If we skipped intervals (gap), emit empty candles for the gaps.
        # ponytail: gap-fill candles use previous close as O/H/L/C —
        # honest about missing data; upgrade: interpolate or mark gap quality.
        prev_close = closed_candle["close"]
        gap_start = datetime.fromtimestamp(
            state.open_time.timestamp() + tf, tz=timezone.utc
        )
        while gap_start < interval_start:
            gap_candle = self._make_gap_candle(prev_close, gap_start, tf)
            state.closed.append(gap_candle)
            if self.on_candle_close is not None:
                self.on_candle_close(gap_candle)
            gap_start = datetime.fromtimestamp(
                gap_start.timestamp() + tf, tz=timezone.utc
            )

        # Start the new forming candle
        state.reset(interval_start, price)
        return closed_candle

    def _close_and_emit(
        self, state: _CandleState, tf: int, interval_start: datetime
    ) -> dict[str, Any]:
        """Finalize the forming candle, push to closed deque, invoke callback."""
        candle = self._candle_dict(state, is_closed=True, tf=tf)
        candle["close_time"] = interval_start  # close_time = next interval start
        state.closed.append(candle)
        if self.on_candle_close is not None:
            self.on_candle_close(candle)
        return candle

    def _make_gap_candle(
        self, prev_close: float, gap_start: datetime, tf: int
    ) -> dict[str, Any]:
        """Create a gap-fill candle (no ticks received) at previous close."""
        return {
            "symbol": self.symbol,
            "timeframe": tf,
            "open": prev_close,
            "high": prev_close,
            "low": prev_close,
            "close": prev_close,
            "volume_ticks": 0,
            "open_time": gap_start,
            "close_time": datetime.fromtimestamp(
                gap_start.timestamp() + tf, tz=timezone.utc
            ),
            "is_closed": True,
            "gap_filled": True,
        }

    def _candle_dict(
        self, state: _CandleState, is_closed: bool, tf: int = 0
    ) -> dict[str, Any]:
        """Build the standard candle dict from internal state."""
        close_time = None
        if state.open_time is not None and tf > 0:
            close_time = datetime.fromtimestamp(
                state.open_time.timestamp() + tf, tz=timezone.utc
            )
        return {
            "symbol": self.symbol,
            "timeframe": tf,
            "open": round(state.open, 6),
            "high": round(state.high, 6),
            "low": round(state.low, 6),
            "close": round(state.close, 6),
            "volume_ticks": state.volume_ticks,
            "open_time": state.open_time,
            "close_time": close_time,
            "is_closed": is_closed,
        }


# ── Multi-symbol registry ──────────────────────────────────────────────
class CandleRegistry:
    """
    Manages one ``CandleAggregator`` per symbol.

    The tick engine calls ``registry.feed_tick(...)`` for every repaired tick;
    the registry routes to the correct symbol aggregator and collects closed
    candles.
    """

    def __init__(
        self,
        timeframes: tuple[int, ...] = DEFAULT_TIMEFRAMES,
        on_candle_close: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.timeframes = timeframes
        self._on_candle_close = on_candle_close
        self._aggregators: dict[str, CandleAggregator] = {}

    def feed_tick(
        self, symbol: str, bid: float, ask: float, timestamp_ms: int
    ) -> list[dict[str, Any]]:
        """
        Route a tick to the correct symbol aggregator.

        Returns closed candles (usually empty; non-empty on boundary crossing).
        """
        agg = self._aggregators.get(symbol)
        if agg is None:
            agg = CandleAggregator(
                symbol, self.timeframes, self._on_candle_close
            )
            self._aggregators[symbol] = agg
        return agg.feed_tick(bid, ask, timestamp_ms)

    def get_aggregator(self, symbol: str) -> Optional[CandleAggregator]:
        return self._aggregators.get(symbol)

    def symbols(self) -> list[str]:
        return list(self._aggregators.keys())

    def reset(self) -> None:
        for agg in self._aggregators.values():
            agg.reset()
