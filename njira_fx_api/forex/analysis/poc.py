"""
POC / Reference-Level Engine (Phase 3D — improvements.md §29).

Computes tick-derived point-of-control (price level with highest tick
concentration) per timeframe.  Supports session, range, and rolling POC.

IMPORTANT: This is NOT true volume POC — it is tick-concentration POC
from bid/ask data.  If actual volume becomes available later, swap the
calculation without rewriting the signal engine.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("forex.analysis.poc")


@dataclass
class POCResult:
    """POC for one timeframe."""
    poc_price: float
    tick_count: int  # total ticks in the window
    bin_count: int   # number of price bins
    window_size: int  # number of candles in the window
    tf: int
    poc_type: str  # "rolling", "session", "range"


class POCEngine:
    """
    Per-symbol, per-timeframe POC calculation.

    Uses a rolling window of candles + tick data to find the price level
    with the highest tick concentration.

    Usage:
        engine = POCEngine()
        poc = engine.compute(candles, ticks)
        price = engine.get_poc(tf)
    """

    def __init__(
        self,
        price_bin_pips: float = 0.5,
        rolling_window_candles: int = 20,
        min_ticks: int = 10,
    ) -> None:
        self._bin_pips = price_bin_pips
        self._window = rolling_window_candles
        self._min_ticks = min_ticks
        self._pocs: dict[int, POCResult] = {}  # tf → POCResult
        # Rolling tick buffer: {tf: [(price, timestamp), ...]}
        self._tick_buffers: dict[int, list[tuple[float, int]]] = defaultdict(list)

    def feed_tick(self, tf: int, bid: float, ask: float, timestamp_ms: int) -> None:
        """Feed a raw tick for POC calculation at a specific timeframe."""
        mid = (bid + ask) / 2.0
        self._tick_buffers[tf].append((mid, timestamp_ms))
        # Keep only last 1000 ticks per TF to bound memory
        if len(self._tick_buffers[tf]) > 1000:
            self._tick_buffers[tf] = self._tick_buffers[tf][-1000:]

    def compute_from_candles(self, candles: list[dict], tf: int) -> Optional[POCResult]:
        """
        Compute POC from closed candles (tick-derived approximation).

        Uses the close price of each candle as a proxy for tick activity
        at that level.  Less accurate than raw ticks but works for
        historical data.
        """
        if len(candles) < 3:
            return None

        recent = candles[-self._window:]
        return self._compute_poc_from_prices(
            [c["close"] for c in recent], tf, "rolling"
        )

    def compute_from_ticks(self, tf: int) -> Optional[POCResult]:
        """
        Compute POC from the tick buffer for a specific timeframe.

        More accurate than candle-based POC when tick data is available.
        """
        ticks = self._tick_buffers.get(tf, [])
        if len(ticks) < self._min_ticks:
            return None

        prices = [price for price, _ in ticks]
        return self._compute_poc_from_prices(prices, tf, "rolling")

    def get_poc(self, tf: int) -> Optional[float]:
        """Get the latest POC price for a timeframe."""
        result = self._pocs.get(tf)
        return result.poc_price if result else None

    def get_result(self, tf: int) -> Optional[POCResult]:
        return self._pocs.get(tf)

    # ── Private ─────────────────────────────────────────────────────────

    def _compute_poc_from_prices(
        self, prices: list[float], tf: int, poc_type: str,
    ) -> Optional[POCResult]:
        """Core POC calculation: bin prices and find the densest bin."""
        if not prices:
            return None

        # Convert pip tolerance to price units (assuming 5-digit pairs)
        bin_size = self._bin_pips * 0.0001

        # Bin prices
        bins: dict[float, int] = defaultdict(int)
        for price in prices:
            bin_key = round(price / bin_size) * bin_size
            bins[bin_key] += 1

        if not bins:
            return None

        # Find the bin with the most ticks
        poc_price = max(bins, key=bins.get)
        result = POCResult(
            poc_price=round(poc_price, 5),
            tick_count=len(prices),
            bin_count=len(bins),
            window_size=len(prices),
            tf=tf,
            poc_type=poc_type,
        )
        self._pocs[tf] = result
        return result

    def reset(self) -> None:
        self._pocs.clear()
        self._tick_buffers.clear()
