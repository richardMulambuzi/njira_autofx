"""
Venom model (ICT) — the strike on a range's liquidity.

The idea: a quiet window (the Asian range) builds two shelves of stops, one above
its high and one below its low.  When the market opens, price stabs one shelf —
that stab is the "venom" — and then delivers to the opposite shelf, which is the
objective.

    stab below the range low  -> STRIKE_DOWN -> expect delivery to the range HIGH
    stab above the range high -> STRIKE_UP   -> expect delivery to the range LOW

The stab is two-sided: if price does not just take the shelf but CLOSES decisively
through it, the range did not hold and the model is dead — that is INVALIDATED.
Whoever gets there first wins: target reached -> DELIVERED, close through the
stabbed side -> INVALIDATED.

This module owns its own range construction (it does not consume an injected
range), which is what keeps it independent of ``asian_range.py`` — swap either one
and the other is unaffected.

TIMEZONE NOTE — the window is in raw UTC hours.  ICT describes the Asian range as
20:00-00:00 EST; ``sessions.KILLZONES["asian_high"]`` uses (0, 2) UTC.  Those are
not the same window and neither is silently preferred here: the hours are Config
fields (conflict C2 in the build-order spec) and can be set to whichever the
operator wants.

Standalone by design (project rule §1): stdlib only, no sibling detector imports,
every number in ``VenomConfig``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.venom")

CONCEPT_NAME = "venom"
CONCEPT_SOURCES = ("ict", "tradingfinder")

_SECONDS_PER_DAY = 86400


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class VenomConfig:
    range_start_hour_utc: int = 0        # window that builds the range (Asian by default)
    range_end_hour_utc: int = 6          # exclusive; start > end wraps past midnight
    min_sweep_pips: float = 2.0          # how far past a shelf counts as a stab
    invalidate_buffer_pips: float = 3.0  # a close this far past the stabbed side kills it
    sweep_confirm: str = "wick"          # "wick" = the stab itself | "close" = needs a close out
    target_mode: str = "opposite_extreme"  # "opposite_extreme" | "range_extension"
    target_extension: float = 1.0        # used by "range_extension": multiples of range height
    pip_size: float = 0.0001
    max_bars_after_range: int = 500      # stop scanning this long after the window shut


DEFAULT_VENOM_CONFIG = VenomConfig()


# ── Result types ────────────────────────────────────────────────────────────

class VenomState(Enum):
    NO_RANGE = "NO_RANGE"
    RANGE_SET = "RANGE_SET"
    STRIKE_DOWN = "STRIKE_DOWN"
    STRIKE_UP = "STRIKE_UP"
    DELIVERED = "DELIVERED"
    INVALIDATED = "INVALIDATED"
    UNKNOWN = "UNKNOWN"


class VenomEvent(Enum):
    RANGE_SET = "VN_RANGE_SET"
    STRIKE_DOWN = "VN_STRIKE_DOWN"
    STRIKE_UP = "VN_STRIKE_UP"
    DELIVERED = "VN_DELIVERED"
    INVALIDATED = "VN_INVALIDATED"


@dataclass
class VenomRecord:
    symbol: str = ""
    timeframe: int = 0
    state: VenomState = VenomState.UNKNOWN
    range_day: int = 0
    range_high: float = 0.0
    range_low: float = 0.0
    range_bars: int = 0
    strike_side: str = ""               # "UP" | "DOWN" — which shelf was stabbed
    strike_price: float = 0.0
    strike_time: object = None
    sweep_depth_pips: float = 0.0
    target_price: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state.value,
            "range_day": self.range_day,
            "range_high": round(self.range_high, 6),
            "range_low": round(self.range_low, 6),
            "range_bars": self.range_bars,
            "strike_side": self.strike_side,
            "strike_price": round(self.strike_price, 6),
            "strike_time": self.strike_time,
            "sweep_depth_pips": round(self.sweep_depth_pips, 2),
            "target_price": round(self.target_price, 6),
        }


# ── Pure helpers (no state, no I/O) ─────────────────────────────────────────

def _val(candle: dict, *keys, default=None):
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    ts = _val(candle, "open_time", "time", "timestamp", "date")
    if ts is None:
        return None
    ts = float(ts)
    return ts / 1000.0 if ts > 1e11 else ts


def _high(candle: dict) -> float:
    return float(_val(candle, "high", "h", default=0.0))


def _low(candle: dict) -> float:
    return float(_val(candle, "low", "l", default=0.0))


def _close(candle: dict) -> float:
    return float(_val(candle, "close", "c", default=0.0))


def _hour_of(ts: float) -> int:
    return int((ts % _SECONDS_PER_DAY) // 3600)


def _in_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end          # wraps midnight


def _window_day(ts: float, start: int, end: int) -> Optional[int]:
    """Which window-date a bar belongs to, or None if it is outside the window."""
    hour = _hour_of(ts)
    if not _in_window(hour, start, end):
        return None
    day = int(ts // _SECONDS_PER_DAY)
    if start < end or hour >= start:
        return day
    return day - 1                               # the after-midnight half of a wrapping window


def _window_end_ts(day: int, start: int, end: int) -> float:
    base = day * _SECONDS_PER_DAY
    if end > start:
        return base + end * 3600
    return base + _SECONDS_PER_DAY + end * 3600


@dataclass
class _Range:
    day: int
    high: float
    low: float
    bars: int
    end_ts: float


def _build_range(candles: list[dict], start: int, end: int,
                 last_ts: float) -> Optional[_Range]:
    """Most recent window that has already closed, as high/low/bars."""
    buckets: dict[int, list[dict]] = {}
    for candle in candles:
        ts = _time_key(candle)
        if ts is None:
            continue
        day = _window_day(ts, start, end)
        if day is None:
            continue
        buckets.setdefault(day, []).append(candle)

    complete = [(day, bars) for day, bars in buckets.items()
                if _window_end_ts(day, start, end) <= last_ts and bars]
    if not complete:
        return None

    day, bars = max(complete, key=lambda pair: pair[0])
    return _Range(
        day=day,
        high=max(_high(c) for c in bars),
        low=min(_low(c) for c in bars),
        bars=len(bars),
        end_ts=_window_end_ts(day, start, end),
    )


# ── Engine ──────────────────────────────────────────────────────────────────

class VenomEngine:
    """Per-symbol range-strike state.  Pure: no clock, no I/O."""

    def __init__(self, config: VenomConfig = DEFAULT_VENOM_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], VenomRecord] = {}
        self._events: list[dict] = []

    # -- main entry point ---------------------------------------------------

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        cfg = self.config
        found = _build_range(candles, cfg.range_start_hour_utc,
                             cfg.range_end_hour_utc, last_ts)
        if found is None:
            return []                            # no window has closed yet

        record = self._build(candles, tf, symbol, found)
        if record is None:
            return []

        prev = self._state.get((symbol, tf))
        events = self._diff(record, prev)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    # -- computation --------------------------------------------------------

    def _build(self, candles: list[dict], tf: int, symbol: str,
               rng: _Range) -> VenomRecord:
        cfg = self.config
        record = VenomRecord(
            symbol=symbol,
            timeframe=tf,
            state=VenomState.RANGE_SET,
            range_day=rng.day,
            range_high=rng.high,
            range_low=rng.low,
            range_bars=rng.bars,
            created_at=_time_key(candles[-1]),
        )

        after = [c for c in candles if (_time_key(c) or 0) > rng.end_ts]
        after = after[:cfg.max_bars_after_range]
        if not after:
            return record

        strike_i: Optional[int] = None
        for i, candle in enumerate(after):
            if self._breached(candle, rng.high, above=True):
                record.strike_side = "UP"
                record.strike_price = _high(candle)
                record.strike_time = _time_key(candle)
                record.sweep_depth_pips = (_high(candle) - rng.high) / cfg.pip_size
                strike_i = i
                break
            if self._breached(candle, rng.low, above=False):
                record.strike_side = "DOWN"
                record.strike_price = _low(candle)
                record.strike_time = _time_key(candle)
                record.sweep_depth_pips = (rng.low - _low(candle)) / cfg.pip_size
                strike_i = i
                break

        if strike_i is None:
            return record                        # range intact, nothing stabbed yet

        record.state = (VenomState.STRIKE_UP if record.strike_side == "UP"
                        else VenomState.STRIKE_DOWN)
        record.target_price = self._target(rng, record.strike_side)

        # whichever happens first after the stab wins the record
        for candle in after[strike_i + 1:]:
            if record.strike_side == "UP":
                broken = _close(candle) > rng.high + cfg.invalidate_buffer_pips * cfg.pip_size
                delivered = _low(candle) <= record.target_price
            else:
                broken = _close(candle) < rng.low - cfg.invalidate_buffer_pips * cfg.pip_size
                delivered = _high(candle) >= record.target_price
            if broken:
                record.state = VenomState.INVALIDATED
                return record
            if delivered:
                record.state = VenomState.DELIVERED
                return record

        return record

    def _breached(self, candle: dict, level: float, above: bool) -> bool:
        cfg = self.config
        reach = cfg.min_sweep_pips * cfg.pip_size
        if cfg.sweep_confirm == "close":
            return _close(candle) > level + reach if above else _close(candle) < level - reach
        return _high(candle) > level + reach if above else _low(candle) < level - reach

    def _target(self, rng: _Range, side: str) -> float:
        cfg = self.config
        height = rng.high - rng.low
        if cfg.target_mode == "range_extension":
            if side == "UP":
                return rng.low - height * cfg.target_extension
            return rng.high + height * cfg.target_extension
        return rng.low if side == "UP" else rng.high   # the opposite extreme

    # -- events -------------------------------------------------------------

    def _diff(self, record: VenomRecord,
              prev: Optional[VenomRecord]) -> list[dict]:
        if prev is not None and prev.range_day == record.range_day \
                and prev.state is record.state:
            return []
        mapping = {
            VenomState.RANGE_SET: VenomEvent.RANGE_SET,
            VenomState.STRIKE_DOWN: VenomEvent.STRIKE_DOWN,
            VenomState.STRIKE_UP: VenomEvent.STRIKE_UP,
            VenomState.DELIVERED: VenomEvent.DELIVERED,
            VenomState.INVALIDATED: VenomEvent.INVALIDATED,
        }
        event = mapping.get(record.state)
        if event is None:
            return []
        return [self._event(event, record)]

    def _event(self, event: VenomEvent, record: VenomRecord, **extra) -> dict:
        payload = {"event": event.value, "timestamp": record.created_at}
        payload.update(record.to_dict())
        payload.update(extra)
        return payload

    # -- read access --------------------------------------------------------

    def get_active(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    def reset(self) -> None:
        self._state.clear()
        self._events.clear()
