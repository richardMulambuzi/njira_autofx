"""
Tick Microstructure / Order-Flow Proxy Engine (Phase 3E — improvements.md §30).

Computes metrics from bid/ask tick data:
- Tick direction (up/down/flat)
- Price change magnitude
- Tick velocity (ticks per second)
- Spread expansion/compression
- Bid/ask quote imbalance proxy
- Short-term momentum
- Rejection velocity

NEVER labels this as true institutional order flow — always "tick
imbalance proxy" or "microstructure metrics".
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("forex.analysis.microstructure")


@dataclass
class MicroMetrics:
    """Snapshot of microstructure metrics for one symbol."""
    tick_direction: str  # "UP", "DOWN", "FLAT"
    price_change: float  # mid-to-mid change in pips
    tick_velocity: float  # ticks per second in the window
    spread_pips: float  # current spread
    spread_vs_avg: float  # current / average (1.0 = normal)
    spread_regime: str  # "NORMAL", "EXPANDED", "COMPRESSED"
    quote_imbalance: float  # ratio of up-ticks to total (-1.0 to 1.0)
    momentum: float  # short-term directional momentum (-1.0 to 1.0)
    rejection_velocity: float  # speed of reversal after extreme move


class MicrostructureEngine:
    """
    Per-symbol tick microstructure analysis.

    Usage:
        engine = MicrostructureEngine()
        engine.feed("EURUSD", 1.1, 1.1002, 0.2, 1000000000)
        metrics = engine.get_metrics("EURUSD")
    """

    def __init__(
        self,
        velocity_window_ms: int = 5000,
        momentum_window: int = 20,
        spread_avg_window: int = 100,
    ) -> None:
        self._velocity_window = velocity_window_ms
        self._momentum_window = momentum_window
        self._spread_avg_window = spread_avg_window

        # Per-symbol state
        self._last_mid: dict[str, float] = {}
        self._last_ts: dict[str, int] = {}
        self._recent_ticks: dict[str, deque] = {}  # [(mid, ts_ms, spread)]
        self._directions: dict[str, deque] = {}  # ["UP"/"DOWN"/"FLAT", ...]
        self._spreads: dict[str, deque] = {}

    def feed(
        self, symbol: str, bid: float, ask: float,
        spread_pips: float, timestamp_ms: int,
    ) -> None:
        """Feed one tick for microstructure analysis."""
        mid = (bid + ask) / 2.0
        now = timestamp_ms

        # Tick direction
        last_mid = self._last_mid.get(symbol)
        if last_mid is not None:
            if mid > last_mid:
                direction = "UP"
            elif mid < last_mid:
                direction = "DOWN"
            else:
                direction = "FLAT"
        else:
            direction = "FLAT"

        self._last_mid[symbol] = mid
        self._last_ts[symbol] = now

        # Append to buffers
        if symbol not in self._recent_ticks:
            self._recent_ticks[symbol] = deque(maxlen=500)
            self._directions[symbol] = deque(maxlen=500)
            self._spreads[symbol] = deque(maxlen=500)

        self._recent_ticks[symbol].append((mid, now, spread_pips))
        self._directions[symbol].append(direction)
        self._spreads[symbol].append(spread_pips)

    def get_metrics(self, symbol: str) -> Optional[MicroMetrics]:
        """Compute current microstructure metrics for a symbol."""
        ticks = self._recent_ticks.get(symbol)
        dirs = self._directions.get(symbol)
        spreads = self._spreads.get(symbol)

        if not ticks or len(ticks) < 2:
            return None

        now_ms = self._last_ts.get(symbol, 0)

        # ── Tick velocity (ticks in the velocity window) ────────────────
        cutoff = now_ms - self._velocity_window
        recent = [(m, t, s) for m, t, s in ticks if t >= cutoff]
        velocity = len(recent) / (self._velocity_window / 1000.0) if self._velocity_window > 0 else 0

        # ── Price change ────────────────────────────────────────────────
        first_mid = recent[0][0] if recent else ticks[-1][0]
        last_mid = recent[-1][0] if recent else ticks[-1][0]
        price_change_pips = (last_mid - first_mid) / 0.0001

        # ── Tick direction ──────────────────────────────────────────────
        if price_change_pips > 0.1:
            tick_direction = "UP"
        elif price_change_pips < -0.1:
            tick_direction = "DOWN"
        else:
            tick_direction = "FLAT"

        # ── Spread analysis ─────────────────────────────────────────────
        current_spread = spreads[-1] if spreads else 0
        avg_spread = sum(spreads) / len(spreads) if spreads else current_spread
        spread_vs_avg = current_spread / avg_spread if avg_spread > 0 else 1.0

        if spread_vs_avg > 1.5:
            spread_regime = "EXPANDED"
        elif spread_vs_avg < 0.5:
            spread_regime = "COMPRESSED"
        else:
            spread_regime = "NORMAL"

        # ── Quote imbalance ─────────────────────────────────────────────
        recent_dirs = list(dirs)[-self._momentum_window:] if dirs else []
        up_count = sum(1 for d in recent_dirs if d == "UP")
        down_count = sum(1 for d in recent_dirs if d == "DOWN")
        total = up_count + down_count
        imbalance = (up_count - down_count) / total if total > 0 else 0.0

        # ── Momentum ────────────────────────────────────────────────────
        # Weighted by recency
        if len(recent_dirs) >= 2:
            weights = [1.0 + i / len(recent_dirs) for i in range(len(recent_dirs))]
            weighted_sum = sum(
                (1.0 if d == "UP" else -1.0) * w
                for d, w in zip(recent_dirs, weights)
            )
            max_weight = sum(weights)
            momentum = weighted_sum / max_weight if max_weight > 0 else 0.0
        else:
            momentum = 0.0

        # ── Rejection velocity ──────────────────────────────────────────
        # Speed of reversal after an extreme move
        rejection = 0.0
        if len(recent) >= 4:
            extremes = [m for m, _, _ in recent]
            max_move = max(extremes) - min(extremes)
            # If price moved far then reversed, rejection velocity is high
            if max_move > 0:
                last_move = abs(recent[-1][0] - recent[-2][0])
                rejection = last_move / max_move if max_move > 0 else 0.0

        return MicroMetrics(
            tick_direction=tick_direction,
            price_change=round(price_change_pips, 2),
            tick_velocity=round(velocity, 2),
            spread_pips=round(current_spread, 2),
            spread_vs_avg=round(spread_vs_avg, 2),
            spread_regime=spread_regime,
            quote_imbalance=round(imbalance, 3),
            momentum=round(momentum, 3),
            rejection_velocity=round(rejection, 3),
        )

    def reset(self) -> None:
        self._last_mid.clear()
        self._last_ts.clear()
        self._recent_ticks.clear()
        self._directions.clear()
        self._spreads.clear()
