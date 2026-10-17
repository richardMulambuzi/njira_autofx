"""
FVG / Imbalance Entry detector (ICT premium combo #3).

Answers one question only: *is there an unfilled fair value gap that price has
just returned to, offering an entry in the gap's direction?*

A fair value gap is the 3-candle imbalance: for a bullish FVG the low of
candle 3 sits above the high of candle 1, leaving a gap between them.  When
price later returns into that gap and closes back in the gap's direction, the
imbalance is being respected and the setup is live.

Standalone by design (project rule 1): imports stdlib only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.fvg_entry")

CONCEPT_NAME = "fvg_entry"
CONCEPT_SOURCES = ("ict", "tradingfinder")


@dataclass(frozen=True)
class FVGEntryConfig:
    min_gap_pips: float = 3.0
    pip_size: float = 0.0001
    max_gap_age_bars: int = 50


DEFAULT_FVG_ENTRY_CONFIG = FVGEntryConfig()


class FVGEntryDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class FVGEntryState(Enum):
    WAITING = "WAITING"
    FILLED = "FILLED"
    MISSED = "MISSED"
    UNKNOWN = "UNKNOWN"


class FVGEntryEvent(Enum):
    FVG_ENTRY_SETUP = "FVE_FVG_ENTRY_SETUP"


@dataclass
class FVGEntryRecord:
    symbol: str = ""
    timeframe: int = 0
    direction: str = FVGEntryDirection.NONE.value
    state: str = FVGEntryState.UNKNOWN.value
    gap_high: float = 0.0
    gap_low: float = 0.0
    gap_span_pips: float = 0.0
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
            "gap_high": round(self.gap_high, 6),
            "gap_low": round(self.gap_low, 6),
            "gap_span_pips": round(self.gap_span_pips, 2),
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
def _cl(c): return _num(c, "close")


def _find_fvg(candles, cfg):
    """Most recent 3-candle FVG: (side, index_of_candle3) or None."""
    min_gap = cfg.min_gap_pips * cfg.pip_size
    best = None
    for i in range(2, len(candles)):
        c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]
        # bullish FVG: gap between c1 high and c3 low
        if _l(c3) - _h(c1) >= min_gap:
            best = ("DEMAND", i)
        # bearish FVG: gap between c3 high and c1 low (inverted)
        if _l(c1) - _h(c3) >= min_gap:
            best = ("SUPPLY", i)
    return best


class FVGEntryEngine:
    def __init__(self, config=DEFAULT_FVG_ENTRY_CONFIG):
        self.config = config
        self._state = {}
        self._events = []

    def analyze(self, candles, tf=0, symbol=""):
        if len(candles) < 4:
            return []
        cfg = self.config
        found = _find_fvg(candles, cfg)
        if found is None:
            return []
        side, idx = found
        if not tf:
            tf = int(_val(candles[-1], "timeframe", default=0) or 0)
        c1, c3 = candles[idx - 2], candles[idx]
        if side == "DEMAND":
            gap_high = _l(c3)
            gap_low = _h(c1)
            direction = FVGEntryDirection.LONG.value
        else:
            gap_high = _l(c1)
            gap_low = _h(c3)
            direction = FVGEntryDirection.SHORT.value
        if gap_high <= gap_low:
            return []

        last = candles[-1]
        tol = cfg.pip_size
        entered = _l(last) <= gap_high + tol and _h(last) >= gap_low - tol
        if side == "DEMAND":
            held = _cl(last) >= gap_low - tol
        else:
            held = _cl(last) <= gap_high + tol
        retested = entered and held
        state = FVGEntryState.FILLED.value if retested else FVGEntryState.MISSED.value

        record = FVGEntryRecord(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            state=state,
            gap_high=gap_high,
            gap_low=gap_low,
            gap_span_pips=(gap_high - gap_low) / cfg.pip_size,
            retest_price=_cl(last),
            retest_time=_time_key(last),
            held=held,
            created_at=_time_key(candles[-1]),
        )

        if not retested:
            self._state[(symbol, tf)] = record
            return []
        prev = self._state.get((symbol, tf))
        events = []
        if prev is None or prev.retest_time != record.retest_time:
            events.append(self._event(FVGEntryEvent.FVG_ENTRY_SETUP, record))
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
