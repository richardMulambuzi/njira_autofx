"""
Multi-Timeframe Top-Down detector (ICT premium combo #10).

Answers one question only: *what is the higher-timeframe bias, and is the
lower-timeframe entry aligned with it?*

Top-down: read the bias from the higher-timeframe (HTF) net direction; the
lower-timeframe (LTF) entry is taken only when it agrees.

Standalone by design (project rule 1): imports stdlib only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.mtf_top_down")

CONCEPT_NAME = "mtf_top_down"
CONCEPT_SOURCES = ("ict", "tradingfinder")


@dataclass(frozen=True)
class MtfTopDownConfig:
    htf_ratio: float = 0.5
    pip_size: float = 0.0001


DEFAULT_MTF_TOP_DOWN_CONFIG = MtfTopDownConfig()


class MtfTopDownDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class MtfTopDownState(Enum):
    ALIGNED_LONG = "ALIGNED_LONG"
    ALIGNED_SHORT = "ALIGNED_SHORT"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class MtfTopDownEvent(Enum):
    MTF_TOP_DOWN_SETUP = "MTD_MTF_TOP_DOWN_SETUP"


@dataclass
class MtfTopDownRecord:
    symbol: str = ""
    timeframe: int = 0
    direction: str = MtfTopDownDirection.NONE.value
    state: str = MtfTopDownState.UNKNOWN.value
    htf_bias: str = ""
    ltf_direction: str = ""
    aligned: bool = False
    created_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "state": self.state,
            "htf_bias": self.htf_bias,
            "ltf_direction": self.ltf_direction,
            "aligned": self.aligned,
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


def _cl(c): return _num(c, "close")
def _op(c): return _num(c, "open")


class MtfTopDownEngine:
    def __init__(self, config=DEFAULT_MTF_TOP_DOWN_CONFIG):
        self.config = config
        self._state = {}
        self._events = []

    def analyze(self, candles, tf=0, symbol=""):
        if len(candles) < 4:
            return []
        cfg = self.config
        if not tf:
            tf = int(_val(candles[-1], "timeframe", default=0) or 0)
        split = max(2, int(len(candles) * cfg.htf_ratio))
        htf = candles[:split]
        ltf = candles[split:]
        if not ltf:
            ltf = candles[-2:]
        htf_change = _cl(htf[-1]) - _op(htf[0])
        htf_bias = "BULLISH" if htf_change >= 0 else "BEARISH"
        ltf_change = _cl(ltf[-1]) - _op(ltf[0])
        ltf_dir = "LONG" if ltf_change >= 0 else "SHORT"

        aligned = (htf_bias == "BULLISH" and ltf_dir == "LONG") or (htf_bias == "BEARISH" and ltf_dir == "SHORT")
        if aligned:
            direction = MtfTopDownDirection.LONG.value if htf_bias == "BULLISH" else MtfTopDownDirection.SHORT.value
            state = MtfTopDownState.ALIGNED_LONG.value if htf_bias == "BULLISH" else MtfTopDownState.ALIGNED_SHORT.value
        else:
            direction = MtfTopDownDirection.NONE.value
            state = MtfTopDownState.CONFLICT.value

        record = MtfTopDownRecord(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            state=state,
            htf_bias=htf_bias,
            ltf_direction=ltf_dir,
            aligned=aligned,
            created_at=_time_key(candles[-1]),
        )

        prev = self._state.get((symbol, tf))
        events = []
        if prev is None or prev.state != record.state:
            events.append(self._event(MtfTopDownEvent.MTF_TOP_DOWN_SETUP, record))
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
