"""
BOS Continuation detector (ICT premium combo #9).

Answers one question only: *has price broken structure in the trend direction
and is it continuing, giving a continuation entry?*

A bullish BOS is a close above the prior swing high; a bearish BOS is a close
below the prior swing low.  Either confirms continuation in that direction.

Standalone by design (project rule 1): imports stdlib only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.bos_continuation")

CONCEPT_NAME = "bos_continuation"
CONCEPT_SOURCES = ("ict", "tradingfinder")


@dataclass(frozen=True)
class BOSContinuationConfig:
    lookback: int = 5
    pip_size: float = 0.0001


DEFAULT_BOS_CONTINUATION_CONFIG = BOSContinuationConfig()


class BOSContinuationDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class BOSContinuationState(Enum):
    BULLISH_BOS = "BULLISH_BOS"
    BEARISH_BOS = "BEARISH_BOS"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class BOSContinuationEvent(Enum):
    BOS_CONTINUATION_SETUP = "BC_BOS_CONTINUATION_SETUP"


@dataclass
class BOSContinuationRecord:
    symbol: str = ""
    timeframe: int = 0
    direction: str = BOSContinuationDirection.NONE.value
    state: str = BOSContinuationState.UNKNOWN.value
    broken_level: float = 0.0
    close_price: float = 0.0
    created_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "state": self.state,
            "broken_level": round(self.broken_level, 6),
            "close_price": round(self.close_price, 6),
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


class BOSContinuationEngine:
    def __init__(self, config=DEFAULT_BOS_CONTINUATION_CONFIG):
        self.config = config
        self._state = {}
        self._events = []

    def analyze(self, candles, tf=0, symbol=""):
        if len(candles) < 3:
            return []
        cfg = self.config
        if not tf:
            tf = int(_val(candles[-1], "timeframe", default=0) or 0)
        window = candles[-(cfg.lookback + 1):-1] if len(candles) > cfg.lookback + 1 else candles[:-1]
        if not window:
            return []
        prior_high = max(_h(c) for c in window)
        prior_low = min(_l(c) for c in window)
        last = candles[-1]
        close = _cl(last)

        if close > prior_high:
            direction = BOSContinuationDirection.LONG.value
            state = BOSContinuationState.BULLISH_BOS.value
            level = prior_high
        elif close < prior_low:
            direction = BOSContinuationDirection.SHORT.value
            state = BOSContinuationState.BEARISH_BOS.value
            level = prior_low
        else:
            direction = BOSContinuationDirection.NONE.value
            state = BOSContinuationState.NONE.value
            level = prior_high

        record = BOSContinuationRecord(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            state=state,
            broken_level=level,
            close_price=close,
            created_at=_time_key(candles[-1]),
        )

        prev = self._state.get((symbol, tf))
        events = []
        if prev is None or prev.state != record.state:
            events.append(self._event(BOSContinuationEvent.BOS_CONTINUATION_SETUP, record))
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
