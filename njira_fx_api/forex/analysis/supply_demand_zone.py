"""
Supply & Demand Zone Entry detector (ICT premium combo #6).

Answers one question only: *is price at a fresh supply/demand zone and holding,
giving an entry in the zone's direction?*

Wraps the same base+departure logic as the supply_demand detector but emits an
entry setup (rather than a zone lifecycle) when price retests and holds.

Standalone by design (project rule 1): imports stdlib only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.supply_demand_zone")

CONCEPT_NAME = "supply_demand_zone"
CONCEPT_SOURCES = ("ict", "tradingfinder")


@dataclass(frozen=True)
class SupplyDemandZoneConfig:
    base_max_bars: int = 3
    impulse_min_atr_mult: float = 1.5
    atr_period: int = 14
    pip_size: float = 0.0001


DEFAULT_SUPPLY_DEMAND_ZONE_CONFIG = SupplyDemandZoneConfig()


class SupplyDemandZoneDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class SupplyDemandZoneState(Enum):
    WAITING = "WAITING"
    TRIGGERED = "TRIGGERED"
    MISSED = "MISSED"
    UNKNOWN = "UNKNOWN"


class SupplyDemandZoneEvent(Enum):
    SD_ZONE_ENTRY_SETUP = "SDZ_SD_ZONE_ENTRY_SETUP"


@dataclass
class SupplyDemandZoneRecord:
    symbol: str = ""
    timeframe: int = 0
    direction: str = SupplyDemandZoneDirection.NONE.value
    state: str = SupplyDemandZoneState.UNKNOWN.value
    zone_high: float = 0.0
    zone_low: float = 0.0
    zone_span_pips: float = 0.0
    retest_price: float = 0.0
    retest_time: Optional[float] = None
    held: bool = False
    created_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "state": self.state,
            "zone_high": round(self.zone_high, 6),
            "zone_low": round(self.zone_low, 6),
            "zone_span_pips": round(self.zone_span_pips, 2),
            "retest_price": round(self.retest_price, 6),
            "retest_time": self.retest_time,
            "held": self.held,
        }


def _val(candle, *keys, default=None):
    if not isinstance(candle, dict):
        return default
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle):
    v = _val(candle, "open_time", "time", "timestamp", "date")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num(candle, key):
    try:
        return float(_val(candle, key, default=0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _h(c): return _num(c, "high")
def _l(c): return _num(c, "low")
def _o(c): return _num(c, "open")
def _cl(c): return _num(c, "close")


def compute_atr(candles, period):
    if period <= 0 or len(candles) < 2:
        return 0.0
    window = candles[-(period + 1):]
    ranges = []
    for i in range(1, len(window)):
        pc = _cl(window[i - 1])
        h, l = _h(window[i]), _l(window[i])
        ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not ranges:
        return 0.0
    return sum(ranges) / len(ranges)


def _find_zone(candles, cfg, atr):
    if atr <= 0:
        return None
    needed = cfg.impulse_min_atr_mult * atr
    for d in range(len(candles) - 1, 1, -1):
        base_end = d - 1
        base_start = max(0, d - cfg.base_max_bars)
        if base_start >= d:
            continue
        base = candles[base_start:base_end + 1]
        if any(_h(b) - _l(b) > atr for b in base):
            continue
        base_high = max(_h(b) for b in base)
        base_low = min(_l(b) for b in base)
        close = _cl(candles[d])
        if close - base_high >= needed:
            return ("DEMAND", base_start, base_end, d)
        if base_low - close >= needed:
            return ("SUPPLY", base_start, base_end, d)
    return None


class SupplyDemandZoneEngine:
    def __init__(self, config=DEFAULT_SUPPLY_DEMAND_ZONE_CONFIG):
        self.config = config
        self._state = {}
        self._events = []

    def analyze(self, candles, tf=0, symbol=""):
        if len(candles) < 4:
            return []
        cfg = self.config
        atr = compute_atr(candles, cfg.atr_period)
        found = _find_zone(candles, cfg, atr)
        if found is None:
            return []
        side, bs, be, di = found
        if not tf:
            tf = int(_val(candles[-1], "timeframe", default=0) or 0)
        base = candles[bs:be + 1]
        base_high = max(_h(c) for c in base)
        base_low = min(_l(c) for c in base)
        opposing = None
        for b in base:
            if side == "DEMAND" and _cl(b) < _o(b):
                opposing = b
            elif side == "SUPPLY" and _cl(b) > _o(b):
                opposing = b
        if opposing is None:
            zone_high, zone_low = base_high, base_low
        else:
            zone_high, zone_low = _h(opposing), _l(opposing)
        if zone_high <= zone_low:
            return []

        last = candles[-1]
        tol = cfg.pip_size
        entered = _l(last) <= zone_high + tol and _h(last) >= zone_low - tol
        if side == "DEMAND":
            held = _cl(last) >= zone_low - tol
            direction = SupplyDemandZoneDirection.LONG.value
        else:
            held = _cl(last) <= zone_high + tol
            direction = SupplyDemandZoneDirection.SHORT.value
        triggered = entered and held
        state = SupplyDemandZoneState.TRIGGERED.value if triggered else SupplyDemandZoneState.MISSED.value

        record = SupplyDemandZoneRecord(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            state=state,
            zone_high=zone_high,
            zone_low=zone_low,
            zone_span_pips=(zone_high - zone_low) / cfg.pip_size,
            retest_price=_cl(last),
            retest_time=_time_key(last),
            held=held,
            created_at=_time_key(candles[-1]),
        )

        if not triggered:
            self._state[(symbol, tf)] = record
            return []
        prev = self._state.get((symbol, tf))
        events = []
        if prev is None or prev.retest_time != record.retest_time:
            events.append(self._event(SupplyDemandZoneEvent.SD_ZONE_ENTRY_SETUP, record))
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        self._events = self._events[-50:]
        return events

    def _event(self, event, record, **extra):
        payload = {"event": event.value, "timestamp": record.created_at}
        payload.update(record.to_dict())
        payload.update(extra)
        return payload

    def get_active(self, limit=10):
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_all(self, limit=20):
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_events(self, n=5):
        return self._events[-n:]

    def reset(self):
        self._state.clear()
        self._events.clear()
