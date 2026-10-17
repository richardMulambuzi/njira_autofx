"""
Asian range + planner levels detector (ICT register item §22).

Answers one question only: *what is the Asian session's range, and has price
swept it?*  ICT treats the Asian extremes, plus the "planner" extensions off
them, as the levels London and New York are expected to run.

The sources do NOT agree on which UTC window the Asian session is.  Both windows
live in Config and BOTH ranges are always reported on every record — the
disagreement is never silently resolved and the two ranges are never merged:

    ict / roadmap (primary) : 20:00-00:00 New York (EST = UTC-5) -> 01:00-05:00 UTC
    litefinance / sessions  : 00:00-02:00 UTC  (== sessions.KILLZONES["asian_high"])

Standalone by design (project rule §1): imports nothing but stdlib.  Tuning this
module can never change any other detector's behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.asian_range")

CONCEPT_NAME = "asian_range"
CONCEPT_SOURCES = ("ict", "litefinance", "theinnercircletraders")

_SECONDS_PER_DAY = 86400


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class AsianRangeConfig:
    asian_start_hour_utc: float = 1.0      # primary; ICT 20:00-00:00 EST
    asian_end_hour_utc: float = 5.0
    alt_asian_start_hour_utc: float = 0.0  # alternate; litefinance/sessions, reported not merged
    alt_asian_end_hour_utc: float = 2.0
    planner_extensions: tuple = (0.5, 1.0, 1.5)   # range-span multiples for planner levels
    sweep_confirm: str = "close"           # "close" (body through) | "wick" (touch)
    pip_size: float = 0.0001


DEFAULT_ASIAN_RANGE_CONFIG = AsianRangeConfig()


# ── Result types ────────────────────────────────────────────────────────────

class AsianRangeState(Enum):
    BUILDING = "BUILDING"          # window still open
    LOCKED = "LOCKED"              # window closed, nothing swept
    SWEPT_HIGH = "SWEPT_HIGH"
    SWEPT_LOW = "SWEPT_LOW"
    SWEPT_BOTH = "SWEPT_BOTH"
    UNKNOWN = "UNKNOWN"


class AsianRangeEvent(Enum):
    ASIAN_RANGE_LOCKED = "AR_RANGE_LOCKED"
    ASIAN_HIGH_SWEPT = "AR_HIGH_SWEPT"
    ASIAN_LOW_SWEPT = "AR_LOW_SWEPT"


@dataclass
class AsianRange:
    symbol: str = ""
    timeframe: int = 0
    range_start_time: object = None
    range_end_time: object = None
    range_high: float = 0.0
    range_low: float = 0.0
    range_mid: float = 0.0
    span_pips: float = 0.0
    current_price: float = 0.0
    state: AsianRangeState = AsianRangeState.UNKNOWN
    building: bool = False
    swept_high: bool = False
    swept_low: bool = False
    sweep_high_time: object = None
    sweep_low_time: object = None
    planner_levels: dict = None       # {"0.5": {"upper": price, "lower": price}, ...}
    alt_range_high: float = 0.0
    alt_range_low: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "range_start_time": self.range_start_time,
            "range_end_time": self.range_end_time,
            "range_high": round(self.range_high, 6),
            "range_low": round(self.range_low, 6),
            "range_mid": round(self.range_mid, 6),
            "span_pips": round(self.span_pips, 2),
            "current_price": round(self.current_price, 6),
            "state": self.state.value,
            "building": self.building,
            "swept_high": self.swept_high,
            "swept_low": self.swept_low,
            "sweep_high_time": self.sweep_high_time,
            "sweep_low_time": self.sweep_low_time,
            "planner_levels": self.planner_levels or {},
            "alt_range_high": round(self.alt_range_high, 6),
            "alt_range_low": round(self.alt_range_low, 6),
        }


# ── Pure helpers (no state, no I/O) ─────────────────────────────────────────

def _val(candle: dict, *keys, default=None):
    if not isinstance(candle, dict):
        return default
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    return _val(candle, "open_time", "time", "timestamp", "date")


def _hour_of_day(ts) -> float:
    return (float(ts) % _SECONDS_PER_DAY) / 3600.0


def in_window(ts, start: float, end: float) -> bool:
    """True inside the half-open UTC window [start, end); wraps past midnight."""
    if start == end:
        return False
    hour = _hour_of_day(ts)
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def window_indices(candles: list[dict], start: float, end: float) -> Optional[tuple]:
    """(first, last) of the MOST RECENT contiguous run of bars inside the window."""
    runs: list[list[int]] = []
    run: list[int] = []
    for i, candle in enumerate(candles):
        ts = _time_key(candle)
        if ts is not None and in_window(ts, start, end):
            run.append(i)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    if not runs:
        return None
    return runs[-1][0], runs[-1][-1]


def window_extremes(candles: list[dict], first: int, last: int) -> tuple:
    highs = [float(_val(candles[i], "high", "h", default=0.0)) for i in range(first, last + 1)]
    lows = [float(_val(candles[i], "low", "l", default=0.0)) for i in range(first, last + 1)]
    return max(highs), min(lows)


def planner_levels(range_low: float, range_high: float, extensions) -> dict:
    """Projection levels off both range extremes, keyed by the span multiple."""
    span = range_high - range_low
    out = {}
    for mult in extensions:
        out[f"{float(mult):g}"] = {
            "upper": round(range_high + float(mult) * span, 6),
            "lower": round(range_low - float(mult) * span, 6),
        }
    return out


# ── Engine ──────────────────────────────────────────────────────────────────

class AsianRangeEngine:
    """Per-symbol Asian-range state.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: AsianRangeConfig = DEFAULT_ASIAN_RANGE_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], AsianRange] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        cfg = self.config
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        found = window_indices(candles, cfg.asian_start_hour_utc, cfg.asian_end_hour_utc)
        if found is None:
            return []
        first, last = found
        high, low = window_extremes(candles, first, last)
        if high <= low:
            return []
        building = last == len(candles) - 1

        swept_high = swept_low = False
        sweep_high_time = sweep_low_time = None
        for i in range(last + 1, len(candles)):
            bar = candles[i]
            if cfg.sweep_confirm == "wick":
                over_high = float(_val(bar, "high", "h", default=0.0)) > high
                over_low = float(_val(bar, "low", "l", default=0.0)) < low
            else:
                close = float(_val(bar, "close", "c", default=0.0))
                over_high = close > high
                over_low = close < low
            if over_high and not swept_high:
                swept_high, sweep_high_time = True, _time_key(bar)
            if over_low and not swept_low:
                swept_low, sweep_low_time = True, _time_key(bar)

        if building:
            state = AsianRangeState.BUILDING
        elif swept_high and swept_low:
            state = AsianRangeState.SWEPT_BOTH
        elif swept_high:
            state = AsianRangeState.SWEPT_HIGH
        elif swept_low:
            state = AsianRangeState.SWEPT_LOW
        else:
            state = AsianRangeState.LOCKED

        alt = window_indices(candles, cfg.alt_asian_start_hour_utc, cfg.alt_asian_end_hour_utc)
        alt_high = alt_low = 0.0
        if alt is not None:
            alt_high, alt_low = window_extremes(candles, alt[0], alt[1])

        record = AsianRange(
            symbol=symbol,
            timeframe=tf,
            range_start_time=_time_key(candles[first]),
            range_end_time=_time_key(candles[last]),
            range_high=high,
            range_low=low,
            range_mid=(high + low) / 2.0,
            span_pips=(high - low) / cfg.pip_size,
            current_price=float(_val(candles[-1], "close", "c", default=high)),
            state=state,
            building=building,
            swept_high=swept_high,
            swept_low=swept_low,
            sweep_high_time=sweep_high_time,
            sweep_low_time=sweep_low_time,
            planner_levels=planner_levels(low, high, cfg.planner_extensions),
            alt_range_high=alt_high,
            alt_range_low=alt_low,
            created_at=_time_key(candles[-1]),
        )

        events = self._diff(record)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _diff(self, record: AsianRange) -> list[dict]:
        key = (record.symbol, record.timeframe)
        prev: Optional[AsianRange] = self._state.get(key)
        new_range = prev is None or prev.range_start_time != record.range_start_time
        events: list[dict] = []
        if new_range or (prev is not None and prev.building and not record.building):
            events.append(self._event(AsianRangeEvent.ASIAN_RANGE_LOCKED, record))
        if prev is not None and not new_range:
            if record.swept_high and not prev.swept_high:
                events.append(self._event(AsianRangeEvent.ASIAN_HIGH_SWEPT, record))
            if record.swept_low and not prev.swept_low:
                events.append(self._event(AsianRangeEvent.ASIAN_LOW_SWEPT, record))
        return events

    def _event(self, event: AsianRangeEvent, record: AsianRange, **extra) -> dict:
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
