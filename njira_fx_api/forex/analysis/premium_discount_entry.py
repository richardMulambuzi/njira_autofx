"""
Premium / Discount (Equilibrium) Entry detector (ICT premium combo #7).

Answers one question only: *is price in the discount (buy) or premium (sell)
half of the current range, giving an entry aligned with the range?*

Equilibrium is the midpoint of the most recent swing high/low.  Below it is
discount (long bias); above it is premium (short bias).

Standalone by design (project rule 1): imports stdlib only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.premium_discount_entry")

CONCEPT_NAME = "premium_discount_entry"
CONCEPT_SOURCES = ("ict", "tradingfinder")


@dataclass(frozen=True)
class PremiumDiscountEntryConfig:
    lookback: int = 20
    pip_size: float = 0.0001


DEFAULT_PREMIUM_DISCOUNT_ENTRY_CONFIG = PremiumDiscountEntryConfig()


class PremiumDiscountEntryDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class PremiumDiscountEntryState(Enum):
    DISCOUNT = "DISCOUNT"
    PREMIUM = "PREMIUM"
    EQUILIBRIUM = "EQUILIBRIUM"
    UNKNOWN = "UNKNOWN"


class PremiumDiscountEntryEvent(Enum):
    PD_ENTRY_SETUP = "PDE_PD_ENTRY_SETUP"


@dataclass
class PremiumDiscountEntryRecord:
    symbol: str = ""
    timeframe: int = 0
    direction: str = PremiumDiscountEntryDirection.NONE.value
    state: str = PremiumDiscountEntryState.UNKNOWN.value
    range_high: float = 0.0
    range_low: float = 0.0
    equilibrium: float = 0.0
    current_price: float = 0.0
    created_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "state": self.state,
            "range_high": round(self.range_high, 6),
            "range_low": round(self.range_low, 6),
            "equilibrium": round(self.equilibrium, 6),
            "current_price": round(self.current_price, 6),
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


class PremiumDiscountEntryEngine:
    def __init__(self, config=DEFAULT_PREMIUM_DISCOUNT_ENTRY_CONFIG):
        self.config = config
        self._state = {}
        self._events = []

    def analyze(self, candles, tf=0, symbol=""):
        if len(candles) < 3:
            return []
        cfg = self.config
        window = candles[-cfg.lookback:] if len(candles) > cfg.lookback else candles
        rh = max(_h(c) for c in window)
        rl = min(_l(c) for c in window)
        eq = (rh + rl) / 2.0
        if not tf:
            tf = int(_val(candles[-1], "timeframe", default=0) or 0)
        price = _cl(candles[-1])
        if price < eq:
            state = PremiumDiscountEntryState.DISCOUNT.value
            direction = PremiumDiscountEntryDirection.LONG.value
        elif price > eq:
            state = PremiumDiscountEntryState.PREMIUM.value
            direction = PremiumDiscountEntryDirection.SHORT.value
        else:
            state = PremiumDiscountEntryState.EQUILIBRIUM.value
            direction = PremiumDiscountEntryDirection.NONE.value

        record = PremiumDiscountEntryRecord(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            state=state,
            range_high=rh,
            range_low=rl,
            equilibrium=eq,
            current_price=price,
            created_at=_time_key(candles[-1]),
        )

        prev = self._state.get((symbol, tf))
        events = []
        if prev is None or prev.state != record.state:
            events.append(self._event(PremiumDiscountEntryEvent.PD_ENTRY_SETUP, record))
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
