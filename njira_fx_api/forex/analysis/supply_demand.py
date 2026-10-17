"""Supply & demand zones.

Answers ONE question: *is there a fresh supply or demand zone left behind by a
departure leg, and has price come back to it yet?*

Standalone by design (project rule §1). The zone is read out of the candles
this module is handed, so tuning it cannot change any other detector.

The departure gate is measured in ATR, not pips, so the same Config works on
EURUSD and XAUUSD without re-tuning. ATR is computed here, from the candles,
because importing a sibling detector is forbidden.

`zone_mode` decides what the zone *is*:
  - "last_opposing_candle" -> the last candle inside the base that ran against
    the departure (the classic order-block reading).
  - "base_extent"          -> the full high/low range of the base.

Both are legitimate readings of the same pattern; the knob exists so the two
can be measured against each other instead of one silently winning.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

CONCEPT_NAME = "supply_demand"
CONCEPT_SOURCES = (
    "Supply & demand zone model (base + departure leg)",
    "Order-block reading (last opposing candle in the base)",
)


@dataclass(frozen=True)
class SupplyDemandConfig:
    zone_mode: str = "last_opposing_candle"
    base_max_bars: int = 3
    impulse_min_atr_mult: float = 1.5
    atr_period: int = 14
    extend_zone: bool = False
    fallback_ignore_departure: bool = False
    fallback_atr_mult: float = 0.75
    break_tolerance_ticks: float = 0.0
    sweep_lookback_bars: int = 10
    pip_size: float = 0.0001


DEFAULT_SUPPLY_DEMAND_CONFIG = SupplyDemandConfig()


class SupplyDemandSide(Enum):
    DEMAND = "DEMAND"
    SUPPLY = "SUPPLY"
    UNKNOWN = "UNKNOWN"


class SupplyDemandState(Enum):
    FRESH = "FRESH"
    TOUCHED = "TOUCHED"
    BROKEN = "BROKEN"
    UNKNOWN = "UNKNOWN"


class SupplyDemandEvent(Enum):
    ZONE_FOUND = "SD_ZONE_FOUND"
    TOUCHED = "SD_TOUCHED"
    BROKEN = "SD_BROKEN"


@dataclass
class SupplyDemandZone:
    symbol: str
    timeframe: int
    side: str
    zone_mode: str
    zone_high: float
    zone_low: float
    zone_span_pips: float
    base_bars: int
    base_start_time: Optional[float]
    base_end_time: Optional[float]
    departure_time: Optional[float]
    departure_move_pips: float
    atr_pips: float
    atr_mult: float
    touched: bool
    touch_time: Optional[float]
    broken: bool
    break_time: Optional[float]
    state: str
    current_price: float
    created_at: Optional[float] = None
    used_fallback: bool = False
    swept_before_touch: bool = False
    strength: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "zone_mode": self.zone_mode,
            "zone_high": round(self.zone_high, 6),
            "zone_low": round(self.zone_low, 6),
            "zone_span_pips": round(self.zone_span_pips, 2),
            "base_bars": self.base_bars,
            "base_start_time": self.base_start_time,
            "base_end_time": self.base_end_time,
            "departure_time": self.departure_time,
            "departure_move_pips": round(self.departure_move_pips, 2),
            "atr_pips": round(self.atr_pips, 2),
            "atr_mult": round(self.atr_mult, 2),
            "touched": self.touched,
            "touch_time": self.touch_time,
            "broken": self.broken,
            "break_time": self.break_time,
            "state": self.state,
            "used_fallback": self.used_fallback,
            "swept_before_touch": self.swept_before_touch,
            "strength": round(self.strength, 4),
            "current_price": round(self.current_price, 6),
        }


# ── pure helpers ────────────────────────────────────────────────────────────

def _val(candle, *keys, default=None):
    """First present key on a candle dict, else default. Never raises."""
    if not isinstance(candle, dict):
        return default
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle) -> Optional[float]:
    value = _val(candle, "open_time", "time", "timestamp", "date")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(candle, key) -> float:
    try:
        return float(_val(candle, key, default=0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def open_of(candle) -> float:
    return _num(candle, "open")


def high_of(candle) -> float:
    return _num(candle, "high")


def low_of(candle) -> float:
    return _num(candle, "low")


def close_of(candle) -> float:
    return _num(candle, "close")


def compute_atr(candles, period: int) -> float:
    """Average true range over the last `period` bars.

    Returns 0.0 when there is no history to average - callers read that as
    "no ATR, therefore no zone", which is the safe direction.
    """
    if period <= 0 or len(candles) < 2:
        return 0.0
    window = candles[-(period + 1):]
    ranges = []
    for i in range(1, len(window)):
        prev_close = close_of(window[i - 1])
        high, low = high_of(window[i]), low_of(window[i])
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not ranges:
        return 0.0
    return sum(ranges) / len(ranges)


def find_zone(candles, config, atr):
    """Locate the most recent base + departure leg.

    Returns ``(side, base_start, base_end, departure_index)`` or ``None``.
    A candidate is only a zone when the base is quiet and the departure
    travels at least ``impulse_min_atr_mult`` ATRs away from it.
    """
    if atr <= 0:
        return None
    needed = config.impulse_min_atr_mult * atr
    for d in range(len(candles) - 1, 1, -1):
        base_end = d - 1
        base_start = max(0, d - config.base_max_bars)
        if base_start >= d:
            continue
        base = candles[base_start:base_end + 1]
        # the base must be a consolidation: no bar inside it may be as wild
        # as a full ATR, otherwise the "zone" is just noise
        if any(high_of(b) - low_of(b) > atr for b in base):
            continue
        base_high = max(high_of(b) for b in base)
        base_low = min(low_of(b) for b in base)
        close = close_of(candles[d])
        if close - base_high >= needed:
            return (SupplyDemandSide.DEMAND, base_start, base_end, d)
        if base_low - close >= needed:
            return (SupplyDemandSide.SUPPLY, base_start, base_end, d)
    return None


def _assert_zone(candles, config, side, base_start, base_end):
    """Resolve the zone bounds for a found base.

    ``zone_mode`` is the tunable: the order-block reading takes just the last
    opposing candle inside the base, the base-extent reading takes the whole
    consolidation range.
    """
    base = candles[base_start:base_end + 1]
    base_high = max(high_of(b) for b in base)
    base_low = min(low_of(b) for b in base)

    if config.zone_mode == "base_extent":
        zone_high, zone_low, anchor = base_high, base_low, candles[base_end]
    else:
        opposing = None
        for b in base:
            if side == SupplyDemandSide.DEMAND and close_of(b) < open_of(b):
                opposing = b
            elif side == SupplyDemandSide.SUPPLY and close_of(b) > open_of(b):
                opposing = b
        if opposing is None:
            zone_high, zone_low, anchor = base_high, base_low, candles[base_end]
        else:
            zone_high, zone_low, anchor = high_of(opposing), low_of(opposing), opposing

    if config.extend_zone:
        zone_high += config.pip_size
        zone_low -= config.pip_size

    return zone_high, zone_low, anchor


# ── engine ──────────────────────────────────────────────────────────────────


class SupplyDemandEngine:
    """Stateful supply/demand tracker, keyed by (symbol, timeframe)."""

    def __init__(self, config: SupplyDemandConfig = DEFAULT_SUPPLY_DEMAND_CONFIG):
        self.config = config
        self._state: dict[tuple[str, int], SupplyDemandZone] = {}
        self._events: list[dict] = []

    def analyze(self, candles, tf: int = 0, symbol: str = "") -> list[dict]:
        cfg = self.config
        if len(candles) < 2:
            return []

        atr = compute_atr(candles, cfg.atr_period)
        found = find_zone(candles, cfg, atr)
        used_fallback = False
        if found is None and cfg.fallback_ignore_departure:
            relaxed = replace(cfg, impulse_min_atr_mult=cfg.fallback_atr_mult)
            found = find_zone(candles, relaxed, atr)
            used_fallback = found is not None
        if found is None:
            return []

        side, base_start, base_end, departure_index = found

        if not tf:
            tf = int(_val(candles[-1], "timeframe", default=0) or 0)

        base = candles[base_start:base_end + 1]
        zone_high, zone_low, _anchor = _assert_zone(
            candles, cfg, side, base_start, base_end
        )
        if zone_high <= zone_low:
            return []

        touched = False
        touch_time = None
        broken = False
        break_time = None
        touch_index = None
        penetration = 0.0
        break_tolerance = cfg.break_tolerance_ticks * cfg.pip_size

        for offset, bar in enumerate(candles[departure_index + 1:]):
            j = departure_index + 1 + offset
            if side == SupplyDemandSide.DEMAND:
                through = close_of(bar) < zone_low - break_tolerance
            else:
                through = close_of(bar) > zone_high + break_tolerance

            if through:
                # A close beyond the far edge ends the zone, and can never
                # count as a clean touch on the same bar.
                broken = True
                break_time = _time_key(bar)
                break

            if low_of(bar) <= zone_high and high_of(bar) >= zone_low:
                if side == SupplyDemandSide.DEMAND:
                    penetration = max(penetration, zone_high - low_of(bar))
                else:
                    penetration = max(penetration, high_of(bar) - zone_low)
                if not touched:
                    touched = True
                    touch_time = _time_key(bar)
                    touch_index = j

        departure = candles[departure_index]
        base_high = max(high_of(c) for c in base)
        base_low = min(low_of(c) for c in base)
        if side == SupplyDemandSide.DEMAND:
            departure_move = close_of(departure) - base_high
        else:
            departure_move = base_low - close_of(departure)
        departure_move = abs(departure_move)

        if broken:
            state = SupplyDemandState.BROKEN
        elif touched:
            state = SupplyDemandState.TOUCHED
        else:
            state = SupplyDemandState.FRESH

        swept_before_touch = False
        if touch_index is not None:
            window = candles[max(0, base_start - cfg.sweep_lookback_bars):base_start]
            if window:
                scan = range(base_start + 1, touch_index + 1)
                if side == SupplyDemandSide.DEMAND:
                    prior_low = min(low_of(c) for c in window)
                    swept_before_touch = any(low_of(candles[j]) < prior_low for j in scan)
                else:
                    prior_high = max(high_of(c) for c in window)
                    swept_before_touch = any(high_of(candles[j]) > prior_high for j in scan)

        zone_span = zone_high - zone_low
        strength = 0.0
        if atr > 0:
            target = max(cfg.impulse_min_atr_mult, 0.1)
            strength += 0.35 * min((departure_move / atr) / target, 1.0)
        if touched and zone_span > 0:
            strength += 0.30 * (1.0 - min(penetration / zone_span, 1.0))
        if swept_before_touch:
            strength += 0.20
        history = max(len(candles) - 1, 1)
        strength += 0.15 * (1.0 - min((len(candles) - 1 - departure_index) / history, 1.0))
        strength = round(min(max(strength, 0.0), 1.0), 4)

        record = SupplyDemandZone(
            symbol=symbol,
            timeframe=tf,
            side=side.value,
            zone_mode=cfg.zone_mode,
            zone_high=zone_high,
            zone_low=zone_low,
            zone_span_pips=(zone_high - zone_low) / cfg.pip_size,
            base_bars=base_end - base_start + 1,
            base_start_time=_time_key(candles[base_start]),
            base_end_time=_time_key(candles[base_end]),
            departure_time=_time_key(departure),
            departure_move_pips=departure_move / cfg.pip_size,
            atr_pips=atr / cfg.pip_size,
            atr_mult=departure_move / atr if atr else 0.0,
            touched=touched,
            touch_time=touch_time,
            broken=broken,
            break_time=break_time,
            used_fallback=used_fallback,
            swept_before_touch=swept_before_touch,
            strength=strength,
            state=state.value,
            current_price=close_of(candles[-1]),
            created_at=_time_key(candles[-1]),
        )

        events = self._diff(record)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        self._events = self._events[-50:]
        return events

    def _diff(self, record: SupplyDemandZone) -> list[dict]:
        prev = self._state.get((record.symbol, record.timeframe))
        new_zone = prev is None or prev.departure_time != record.departure_time

        if new_zone:
            events = [self._event(SupplyDemandEvent.ZONE_FOUND, record)]
            if record.touched:
                events.append(self._event(SupplyDemandEvent.TOUCHED, record))
            if record.broken:
                events.append(self._event(SupplyDemandEvent.BROKEN, record))
            return events

        events = []
        if record.touched and not prev.touched:
            events.append(self._event(SupplyDemandEvent.TOUCHED, record))
        if record.broken and not prev.broken:
            events.append(self._event(SupplyDemandEvent.BROKEN, record))
        return events

    def _event(
        self, event: SupplyDemandEvent, record: SupplyDemandZone, **extra
    ) -> dict:
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
