"""
Candle Range Theory (CRT) detector.

Reference range = high/low of the LAST `range_bars` bars EXCLUDING the most recent bar.
Price sweeps ONE side, then delivers to the OTHER side.

Standalone by design (project rule §1): imports nothing but stdlib + sessions.
All thresholds live in the frozen Config dataclass.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from forex.analysis import sessions

logger = logging.getLogger("forex.analysis.candle_range_theory")

CONCEPT_NAME = "candle_range_theory"
CONCEPT_SOURCES = ("ict_crt", "candle_range_theory")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class CrtConfig:
    range_bars: int = 5
    sweep_confirm: str = "close"          # "close" | "wick"
    min_sweep_depth_ticks: float = 0.0
    max_bars_deliver: int = 20
    tick_size: float = 0.00001
    pip_size: float = 0.0001
    min_range_pips: float = 5.0


DEFAULT_CRT_CONFIG = CrtConfig()


# ── Result types ────────────────────────────────────────────────────────────

class CrtState(Enum):
    FORMING = "FORMING"
    SWEPT_HIGH = "SWEPT_HIGH"
    SWEPT_LOW = "SWEPT_LOW"
    DELIVERED_UP = "DELIVERED_UP"
    DELIVERED_DOWN = "DELIVERED_DOWN"
    UNKNOWN = "UNKNOWN"


class CrtEvent(Enum):
    CRT_RANGE_SET = "CRT_RANGE_SET"
    CRT_SWEPT_HIGH = "CRT_SWEPT_HIGH"
    CRT_SWEPT_LOW = "CRT_SWEPT_LOW"
    CRT_DELIVERED = "CRT_DELIVERED"


@dataclass
class Crt:
    symbol: str = ""
    timeframe: int = 0
    range_low: float = 0.0
    range_high: float = 0.0
    swept_side: str = ""           # "HIGH" | "LOW" | ""
    sweep_depth_ticks: float = 0.0
    delivery_target: float = 0.0
    bars_since_sweep: int = 0
    state: CrtState = CrtState.UNKNOWN
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "range_low": round(self.range_low, 6),
            "range_high": round(self.range_high, 6),
            "swept_side": self.swept_side,
            "sweep_depth_ticks": round(self.sweep_depth_ticks, 4),
            "delivery_target": round(self.delivery_target, 6),
            "bars_since_sweep": self.bars_since_sweep,
            "state": self.state.value,
        }


# ── Pure helpers (no state, no I/O) ─────────────────────────────────────────

def _val(candle: dict, *keys, default=None):
    """Tolerant candle access — same tolerance as simple_snr._val."""
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    return _val(candle, "open_time", "time", "timestamp", "date")


def _compute_reference_range(candles: list[dict], range_bars: int) -> tuple[float, float] | None:
    """
    Compute reference range from the LAST `range_bars` bars, excluding the bar
    being tested for a sweep.

    The sweep is checked on `candles[-2]` - the last CLOSED bar, because the
    caller may still be updating `candles[-1]`. That bar must therefore sit
    OUTSIDE its own reference range, otherwise a close sweep would have to
    exceed the bar's own high and could never fire.

    Example: if range_bars=5 and we have 20 candles (indices 0-19), the
    reference range uses indices 13..17 - the 5 bars before the swept bar at
    index 18.
    """
    if len(candles) <= range_bars:
        return None

    # Exclude the forming bar (index -1) and the swept bar (index -2)
    ref_candles = candles[-(range_bars + 2):-2]
    if len(ref_candles) < range_bars:
        return None

    highs = [float(_val(c, "high", "h", default=0.0)) for c in ref_candles]
    lows = [float(_val(c, "low", "l", default=0.0)) for c in ref_candles]

    range_high = max(highs)
    range_low = min(lows)

    if range_high <= range_low:
        return None

    return range_low, range_high


def _check_sweep(candle: dict, range_low: float, range_high: float,
                 sweep_confirm: str, min_depth_ticks: float, tick_size: float) -> tuple[str, float] | None:
    """
    Check if candle sweeps either side of the reference range.
    Returns (swept_side, sweep_depth_ticks) or None.
    swept_side is "HIGH" or "LOW".
    """
    high = float(_val(candle, "high", "h", default=0.0))
    low = float(_val(candle, "low", "l", default=0.0))
    close = float(_val(candle, "close", "c", default=0.0))

    min_depth_price = min_depth_ticks * tick_size

    # Check sweep of HIGH side
    if sweep_confirm == "close":
        swept_high = close > range_high + min_depth_price
        sweep_depth_high = (close - range_high) / tick_size if swept_high else 0.0
    else:  # "wick"
        swept_high = high > range_high + min_depth_price
        sweep_depth_high = (high - range_high) / tick_size if swept_high else 0.0

    # Check sweep of LOW side
    if sweep_confirm == "close":
        swept_low = close < range_low - min_depth_price
        sweep_depth_low = (range_low - close) / tick_size if swept_low else 0.0
    else:  # "wick"
        swept_low = low < range_low - min_depth_price
        sweep_depth_low = (range_low - low) / tick_size if swept_low else 0.0

    if swept_high and not swept_low:
        return "HIGH", sweep_depth_high
    if swept_low and not swept_high:
        return "LOW", sweep_depth_low
    # Both sides swept in same bar — ambiguous, treat as no sweep
    return None


# ── Engine ──────────────────────────────────────────────────────────────────

class CrtEngine:
    """Per-symbol CRT state. Pure: no clock, no I/O, no network."""

    def __init__(self, config: CrtConfig = DEFAULT_CRT_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], Crt] = {}
        self._events: list[dict] = []

    def analyze(
        self,
        candles: list[dict],
        tf: int = 0,
        symbol: str = "",
        zones: list[dict] | None = None,
        context: dict | None = None,
    ) -> list[dict]:
        if len(candles) < 2:
            return []

        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        zones = zones or []
        context = context or {}

        key = (symbol, tf)
        current = self._state.get(key)

        events: list[dict] = []
        current_time = _time_key(candles[-1])

        # Compute reference range
        ref_range = _compute_reference_range(candles, self.config.range_bars)
        if ref_range is None:
            # Not enough bars or degenerate range
            return []

        range_low, range_high = ref_range
        range_span = range_high - range_low
        min_range_price = self.config.min_range_pips * self.config.pip_size

        if range_span < min_range_price:
            # Degenerate range
            return []

        # If no current state, initialize FORMING
        if current is None:
            record = Crt(
                symbol=symbol,
                timeframe=tf,
                range_low=range_low,
                range_high=range_high,
                state=CrtState.FORMING,
                created_at=current_time,
            )
            events.append(self._event(CrtEvent.CRT_RANGE_SET, record))
            self._state[key] = record
            return events

        # Existing state — check for transitions
        new_state = current
        new_range_low = current.range_low
        new_range_high = current.range_high
        new_swept_side = current.swept_side
        new_sweep_depth = current.sweep_depth_ticks
        new_delivery_target = current.delivery_target
        new_bars_since_sweep = current.bars_since_sweep

        if current.state == CrtState.FORMING:
            # Check for sweep on the latest completed bar (index -2)
            sweep_candle = candles[-2]
            sweep = _check_sweep(
                sweep_candle, range_low, range_high,
                self.config.sweep_confirm, self.config.min_sweep_depth_ticks, self.config.tick_size
            )
            if sweep:
                swept_side, depth_ticks = sweep
                new_state = (
                    CrtState.SWEPT_HIGH if swept_side == "HIGH" else CrtState.SWEPT_LOW
                )
                new_swept_side = swept_side
                new_sweep_depth = depth_ticks
                new_delivery_target = range_low if swept_side == "HIGH" else range_high
                new_bars_since_sweep = 0

                # The event carries the sweep it just detected rather than the
                # pre-sweep record, so the payload is self-describing.
                swept_rec = Crt(
                    symbol=symbol,
                    timeframe=tf,
                    range_low=new_range_low,
                    range_high=new_range_high,
                    swept_side=new_swept_side,
                    sweep_depth_ticks=new_sweep_depth,
                    delivery_target=new_delivery_target,
                    bars_since_sweep=0,
                    state=new_state,
                    created_at=current_time,
                )
                events.append(self._event(
                    CrtEvent.CRT_SWEPT_HIGH if swept_side == "HIGH" else CrtEvent.CRT_SWEPT_LOW,
                    swept_rec,
                ))

        elif current.state in (CrtState.SWEPT_HIGH, CrtState.SWEPT_LOW):
            # Count bars since sweep
            new_bars_since_sweep = current.bars_since_sweep + 1

            # Check for delivery on the latest completed bar
            delivery_candle = candles[-2]
            delivery_high = float(_val(delivery_candle, "high", "h", default=0.0))
            delivery_low = float(_val(delivery_candle, "low", "l", default=0.0))

            target = current.delivery_target
            delivered = False

            if current.state == CrtState.SWEPT_HIGH:
                # Swept high, delivering down to range_low
                if delivery_low <= target:
                    delivered = True
                    new_state = CrtState.DELIVERED_DOWN
            else:
                # Swept low, delivering up to range_high
                if delivery_high >= target:
                    delivered = True
                    new_state = CrtState.DELIVERED_UP

            if delivered:
                # Carry the delivered state, not the stale SWEPT record, so the
                # payload reports where the cycle actually got to.
                delivered_rec = Crt(
                    symbol=symbol,
                    timeframe=tf,
                    range_low=current.range_low,
                    range_high=current.range_high,
                    swept_side=current.swept_side,
                    sweep_depth_ticks=current.sweep_depth_ticks,
                    delivery_target=current.delivery_target,
                    bars_since_sweep=new_bars_since_sweep,
                    state=new_state,
                    created_at=current_time,
                )
                events.append(self._event(CrtEvent.CRT_DELIVERED, delivered_rec))

            # Check max bars delivery timeout
            if new_bars_since_sweep >= self.config.max_bars_deliver:
                # Reset to FORMING with new range
                record = Crt(
                    symbol=symbol,
                    timeframe=tf,
                    range_low=range_low,
                    range_high=range_high,
                    state=CrtState.FORMING,
                    created_at=current_time,
                )
                events.append(self._event(CrtEvent.CRT_RANGE_SET, record))
                self._state[key] = record
                return events

        elif current.state in (CrtState.DELIVERED_UP, CrtState.DELIVERED_DOWN):
            # After delivery, reset to FORMING with new range
            record = Crt(
                symbol=symbol,
                timeframe=tf,
                range_low=range_low,
                range_high=range_high,
                state=CrtState.FORMING,
                created_at=current_time,
            )
            events.append(self._event(CrtEvent.CRT_RANGE_SET, record))
            self._state[key] = record
            return events

        # Update state if changed
        if new_state != current:
            # Need to create a new Crt instance with updated values
            record = Crt(
                symbol=symbol,
                timeframe=tf,
                range_low=new_range_low,
                range_high=new_range_high,
                swept_side=new_swept_side,
                sweep_depth_ticks=new_sweep_depth,
                delivery_target=new_delivery_target,
                bars_since_sweep=new_bars_since_sweep,
                state=new_state,
                created_at=current_time,
            )
            self._state[key] = record
        else:
            # Just update bars_since_sweep if in SWEPT state
            if current.state in (CrtState.SWEPT_HIGH, CrtState.SWEPT_LOW):
                record = Crt(
                    symbol=symbol,
                    timeframe=tf,
                    range_low=current.range_low,
                    range_high=current.range_high,
                    swept_side=current.swept_side,
                    sweep_depth_ticks=current.sweep_depth_ticks,
                    delivery_target=current.delivery_target,
                    bars_since_sweep=new_bars_since_sweep,
                    state=current.state,
                    created_at=current.created_at,
                )
                self._state[key] = record

        # Cap events
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]

        return events

    def _event(self, event: CrtEvent, record: Crt, **extra) -> dict:
        payload = {"event": event.value, "timestamp": record.created_at}
        payload.update(record.to_dict())
        payload.update(extra)
        return payload

    def get_active(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    def reset(self) -> None:
        self._state.clear()
        self._events.clear()