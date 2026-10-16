"""
Order Flow Engine (ICT/SMC — real institutional order flow).

This is the genuine order-flow layer that ``microstructure.py`` explicitly
declined to be: it reads candle volume (``volume_ticks``) and approximates
per-candle buy/sell pressure from where the close sits inside the candle's
range, then derives delta, cumulative delta, imbalance ratio and a regime
(ABSORPTION / EXHAUSTION / IMBALANCE / NEUTRAL).

Best-effort by design: when volume is missing it falls back to a range-based
estimate, and every path is guarded so a malformed candle never raises.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger("forex.analysis.order_flow")

REGIME_ABSORPTION = "ABSORPTION"
REGIME_EXHAUSTION = "EXHAUSTION"
REGIME_IMBALANCE = "IMBALANCE"
REGIME_NEUTRAL = "NEUTRAL"

DIRECTION_BULLISH = "BULLISH"
DIRECTION_BEARISH = "BEARISH"
DIRECTION_NEUTRAL = "NEUTRAL"

# Thresholds (tunable).
IMBALANCE_RATIO_HI = 1.5   # buy/sell >= 1.5 → bullish imbalance
IMBALANCE_RATIO_LO = 0.67  # buy/sell <= 0.67 → bearish imbalance
ABSORPTION_VOLUME_FACTOR = 1.5  # volume > 1.5x EMA → "large"
ABSORPTION_RANGE_FACTOR = 0.3   # range < 0.3x EMA → "small" (absorbed)


@dataclass
class OrderFlowEvent:
    """One candle's order-flow reading."""

    symbol: str
    timeframe: int
    timestamp: object  # datetime
    delta: float
    cumulative_delta: float
    buy_volume: float
    sell_volume: float
    imbalance_ratio: float
    regime: str
    direction: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": (
                self.timestamp.isoformat()
                if isinstance(self.timestamp, datetime)
                else str(self.timestamp)
            ),
            "delta": round(self.delta, 4),
            "cumulative_delta": round(self.cumulative_delta, 4),
            "buy_volume": round(self.buy_volume, 4),
            "sell_volume": round(self.sell_volume, 4),
            "imbalance_ratio": round(self.imbalance_ratio, 4),
            "regime": self.regime,
            "direction": self.direction,
            "reason": self.reason,
        }


class OrderFlowEngine:
    """
    Per-symbol, per-timeframe order-flow analysis from OHLCV candles.

    Usage:
        engine = OrderFlowEngine()
        engine.update("EURUSD", candle, 60)
        state = engine.get_state("EURUSD", 60)
        events = engine.get_events("EURUSD", 60, n=5)
    """

    def __init__(self, max_events: int = 100) -> None:
        self._max_events = max_events
        # (symbol, tf) → mutable per-tf state
        self._state: dict[tuple, dict] = {}
        # (symbol, tf) → bounded ring buffer of recent events
        self._events: dict[tuple, deque] = {}

    @staticmethod
    def _key(symbol: str, tf: int) -> tuple:
        return (symbol, int(tf))

    def update(
        self, symbol: str, candle: dict, tf: int
    ) -> Optional[OrderFlowEvent]:
        """
        Ingest one closed candle and return its :class:`OrderFlowEvent`.

        ``candle`` keys: open/high/low/close (float), volume_ticks (int,
        optional), open_time/close_time (datetime, optional). Returns ``None``
        on a malformed candle (never raises).
        """
        try:
            o = float(candle.get("open", 0.0) or 0.0)
            h = float(candle.get("high", 0.0) or 0.0)
            l = float(candle.get("low", 0.0) or 0.0)
            c = float(candle.get("close", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None

        rng = h - l

        # ── Volume: prefer volume_ticks, then volume; else range estimate ──
        vol = candle.get("volume_ticks")
        if not vol:
            vol = candle.get("volume")
        try:
            vol = float(vol) if vol is not None else 0.0
        except (TypeError, ValueError):
            vol = 0.0
        if vol <= 0:
            # ponytail: no real volume → use range magnitude as a proxy so the
            # engine still produces a meaningful delta instead of stalling.
            vol = max(rng, 1e-6)

        # ── Delta split by close position inside the range ─────────────────
        if rng > 0:
            buy_vol = vol * (c - l) / rng
            sell_vol = vol * (h - c) / rng
        else:
            buy_vol = sell_vol = vol / 2.0
        delta = buy_vol - sell_vol

        key = self._key(symbol, tf)
        st = self._state.get(key)
        if st is None:
            st = {
                "cumulative_delta": 0.0,
                "avg_volume": vol,
                "avg_range": rng,
                "regime": REGIME_NEUTRAL,
                "direction": DIRECTION_NEUTRAL,
                "last_delta": 0.0,
                "last_imbalance": 1.0,
            }
            self._state[key] = st

        st["cumulative_delta"] += delta
        # EMA smoothing for absorption detection (volume vs range context).
        st["avg_volume"] = st["avg_volume"] * 0.9 + vol * 0.1
        st["avg_range"] = st["avg_range"] * 0.9 + rng * 0.1

        # ── Imbalance ratio (guard divide-by-zero) ────────────────────────
        if sell_vol > 0:
            imbalance = buy_vol / sell_vol
        elif buy_vol > 0:
            imbalance = float("inf")
        else:
            imbalance = 1.0

        regime, direction, reason = self._classify(
            o, h, l, c, vol, rng, delta, imbalance, st
        )
        st["regime"] = regime
        st["direction"] = direction
        st["last_delta"] = delta
        st["last_imbalance"] = imbalance if imbalance != float("inf") else 999.0

        ts = candle.get("open_time") or candle.get("close_time")
        evt = OrderFlowEvent(
            symbol=symbol,
            timeframe=int(tf),
            timestamp=ts,
            delta=delta,
            cumulative_delta=st["cumulative_delta"],
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            imbalance_ratio=imbalance if imbalance != float("inf") else 999.0,
            regime=regime,
            direction=direction,
            reason=reason,
        )
        buf = self._events.setdefault(key, deque(maxlen=self._max_events))
        buf.append(evt)
        return evt

    def _classify(
        self,
        o: float,
        h: float,
        l: float,
        c: float,
        vol: float,
        rng: float,
        delta: float,
        imbalance: float,
        st: dict,
    ) -> tuple[str, str, str]:
        """Return (regime, direction, reason). Order: exhaustion → imbalance → absorption → neutral."""
        price_up = c > o
        price_down = c < o

        # ── EXHAUSTION: price moved one way, delta diverged the other ──────
        if (price_up and delta < 0) or (price_down and delta > 0):
            return (
                REGIME_EXHAUSTION,
                DIRECTION_BEARISH if delta < 0 else DIRECTION_BULLISH,
                "price moved opposite to delta (exhaustion)",
            )

        # ── IMBALANCE: lopsided buy/sell ───────────────────────────────────
        if imbalance > IMBALANCE_RATIO_HI or imbalance < IMBALANCE_RATIO_LO:
            if imbalance >= 1.0:
                return (
                    REGIME_IMBALANCE,
                    DIRECTION_BULLISH,
                    f"buy/sell imbalance {imbalance:.2f}",
                )
            return (
                REGIME_IMBALANCE,
                DIRECTION_BEARISH,
                f"buy/sell imbalance {imbalance:.2f}",
            )

        # ── ABSORPTION: large volume, small range (price absorbed) ─────────
        if (
            st["avg_volume"] > 0
            and vol > st["avg_volume"] * ABSORPTION_VOLUME_FACTOR
            and st["avg_range"] > 0
            and rng < st["avg_range"] * ABSORPTION_RANGE_FACTOR
        ):
            return (
                REGIME_ABSORPTION,
                DIRECTION_NEUTRAL,
                "high volume absorbed into small range",
            )

        # ── NEUTRAL ────────────────────────────────────────────────────────
        if delta > 0:
            return (REGIME_NEUTRAL, DIRECTION_BULLISH, "balanced, mild buying")
        if delta < 0:
            return (REGIME_NEUTRAL, DIRECTION_BEARISH, "balanced, mild selling")
        return (REGIME_NEUTRAL, DIRECTION_NEUTRAL, "balanced")

    def get_events(self, symbol: str, tf: int, n: int = 1) -> list[OrderFlowEvent]:
        """Return the ``n`` most recent events (oldest→newest) for a symbol/tf."""
        buf = self._events.get(self._key(symbol, tf))
        if not buf:
            return []
        return list(buf)[-n:]

    def get_timeframes(self, symbol: str) -> list[int]:
        """Return the timeframes this engine currently holds state for."""
        return sorted({k[1] for k in self._state if k[0] == symbol})

    def get_state(self, symbol: str, tf: int) -> dict:
        """Current order-flow snapshot for a symbol/tf (neutral defaults if none)."""
        st = self._state.get(self._key(symbol, tf))
        if st is None:
            return {
                "delta": 0.0,
                "cumulative_delta": 0.0,
                "regime": REGIME_NEUTRAL,
                "direction": DIRECTION_NEUTRAL,
                "imbalance_ratio": 1.0,
            }
        return {
            "delta": round(st["last_delta"], 4),
            "cumulative_delta": round(st["cumulative_delta"], 4),
            "regime": st["regime"],
            "direction": st["direction"],
            "imbalance_ratio": round(st["last_imbalance"], 4),
        }

    def reset(self) -> None:
        self._state.clear()
        self._events.clear()
