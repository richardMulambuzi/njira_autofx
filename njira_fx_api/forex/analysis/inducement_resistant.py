"""
Inducement-Resistant Entry detector (ICT premium combo #8).

Answers one question only: *is there a setup that survives an inducement
(sweep of a minor level) without being invalidated, giving an entry?*

A setup is "inducement-resistant" when the minor level is swept but price
reclaims and the structure (higher low / lower high) holds.  Direction follows
the reclaimed structure.

Standalone by design (project rule 1): imports stdlib only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.inducement_resistant")

CONCEPT_NAME = "inducement_resistant"
CONCEPT_SOURCES = ("ict", "tradingfinder")


@dataclass(frozen=True)
class InducementResistantConfig:
    pip_size: float = 0.0001
    min_reclaim_pips: float = 2.0


DEFAULT_INDUCEMENT_RESISTANT_CONFIG = InducementResistantConfig()


class InducementResistantDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class InducementResistantState(Enum):
    RESISTANT = "RESISTANT"
    BROKEN = "BROKEN"
    UNKNOWN = "UNKNOWN"


class InducementResistantEvent(Enum):
    INDUCEMENT_RESISTANT_SETUP = "IRE_INDUCEMENT_RESISTANT_SETUP"


@dataclass
class InducementResistantRecord:
    symbol: str = ""
    timeframe: int = 0
    direction: str = InducementResistantDirection.NONE.value
    state: str = InducementResistantState.UNKNOWN.value
    sweep_level: float = 0.0
    reclaim_price: float = 0.0
    created_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "state": self.state,
            "sweep_level": round(self.sweep_level, 6),
            "reclaim_price": round(self.reclaim_price, 6),
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
def _cl(c): return _num(c, "close")


class InducementResistantEngine:
    def __init__(self, config=DEFAULT_INDUCEMENT_RESISTANT_CONFIG):
        self.config = config
        self._state = {}
        self._events = []

    def analyze(self, candles, tf=0, symbol=""):
        if len(candles) < 3:
            return []
        cfg = self.config
        if not tf:
            tf = int(_val(candles[-1], "timeframe", default=0) or 0)
        # minor level = low of the penultimate bar (the inducement target)
        minor_low = _l(candles[-2])
        minor_high = _h(candles[-2])
        last = candles[-1]
        prev = candles[-3] if len(candles) >= 3 else candles[-2]
        swept_low = _l(last) < minor_low
        swept_high = _h(last) > minor_high
        reclaimed_up = _cl(last) > _cl(prev) + cfg.min_reclaim_pips * cfg.pip_size
        reclaimed_down = _cl(last) < _cl(prev) - cfg.min_reclaim_pips * cfg.pip_size

        if swept_low and reclaimed_up:
            direction = InducementResistantDirection.LONG.value
            state = InducementResistantState.RESISTANT.value
            sweep_level = minor_low
        elif swept_high and reclaimed_down:
            direction = InducementResistantDirection.SHORT.value
            state = InducementResistantState.RESISTANT.value
            sweep_level = minor_high
        else:
            direction = InducementResistantDirection.NONE.value
            state = InducementResistantState.BROKEN.value
            sweep_level = minor_low if swept_low else minor_high

        record = InducementResistantRecord(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            state=state,
            sweep_level=sweep_level,
            reclaim_price=_cl(last),
            created_at=_time_key(candles[-1]),
        )

        prev_rec = self._state.get((symbol, tf))
        events = []
        if prev_rec is None or prev_rec.state != record.state:
            events.append(self._event(InducementResistantEvent.INDUCEMENT_RESISTANT_SETUP, record))
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
