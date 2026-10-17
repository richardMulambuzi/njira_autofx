"""
Friday Seek & Destroy detector (ICT — the weekly-liquidity raid that reverses).

Answers one question only: *on Friday, did price raid the week's extreme and then
get rejected back inside the week's range?*

ICT's claim: Friday is the last real session of the week.  Positions have to be
closed, so price is pushed at the week's high or low to "seek" the stops resting
there and then "destroy" them — it snaps back inside the week's range, and the
move after that runs to the other end of the week.  The raid is the seek; the
snap back is the destroy.

    seek ABOVE the week high, then close back INSIDE  -> DESTROYED, direction SHORT
    seek BELOW the week low,  then close back INSIDE  -> DESTROYED, direction LONG

The week's range is built from the days BEFORE today (Monday up to yesterday), and
only today's bars are allowed to do the raiding.  On a Monday there is no range
yet, so the detector stays quiet — that is `NO_WEEK_RANGE`, not a bug.

Weekday comes from epoch seconds with integer maths only (epoch day 0 was a
Thursday), so nothing here depends on the machine clock or locale.

The stop sits just beyond the seek extreme, which is the price the raid actually
printed; the target is the other end of the week's range.  `target_mode="week_extension"`
extends that target by a multiple of the week's height for traders who want the
run beyond the range instead of to its far edge.

Standalone by design (project rule §1): imports stdlib only — bias and zones are
not needed here, and nothing is injected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.friday_seek_destroy")

CONCEPT_NAME = "friday_seek_destroy"
CONCEPT_SOURCES = ("ict", "tradingfinder")

_SECONDS_PER_DAY = 86400
_FRIDAY = 4          # 0 = Monday, so Friday is 4
_AGREE_SHORT = ("BEARISH", "SHORT", "DOWN", "SELL")
_AGREE_LONG = ("BULLISH", "LONG", "UP", "BUY")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class FridaySeekDestroyConfig:
    pip_size: float = 0.0001
    min_seek_pips: float = 5.0        # how far past the week extreme counts as a raid
    stop_buffer_pips: float = 2.0     # stop sits this far beyond the seek extreme
    destroy_window_bars: int = 20     # bars after the raid in which the snap-back must come
    destroy_confirm: str = "close"    # "close" (body back inside) | "wick" (touch back inside)
    require_friday: bool = True       # False = allow any day of the week
    target_mode: str = "opposite_extreme"   # "opposite_extreme" | "week_extension"
    target_extension: float = 1.0     # multiples of the week height, for "week_extension"


DEFAULT_FRIDAY_SEEK_DESTROY_CONFIG = FridaySeekDestroyConfig()


# ── Result types ────────────────────────────────────────────────────────────

class FridaySeekDestroyState(Enum):
    NOT_FRIDAY = "NOT_FRIDAY"
    NO_WEEK_RANGE = "NO_WEEK_RANGE"
    WAITING = "WAITING"               # Friday, range known, nothing raided yet
    SEEK_UP = "SEEK_UP"               # raided the week high, waiting for rejection
    SEEK_DOWN = "SEEK_DOWN"           # raided the week low, waiting for rejection
    DESTROYED = "DESTROYED"           # rejected back inside — this is the signal
    INVALIDATED = "INVALIDATED"       # no rejection inside the window; the break was real
    UNKNOWN = "UNKNOWN"


class FridaySeekDestroyEvent(Enum):
    SEEK_UP = "FSD_SEEK_UP"
    SEEK_DOWN = "FSD_SEEK_DOWN"
    DESTROYED = "FSD_DESTROYED"
    INVALIDATED = "FSD_INVALIDATED"


@dataclass
class FridaySeekDestroyRecord:
    symbol: str = ""
    timeframe: int = 0
    state: FridaySeekDestroyState = FridaySeekDestroyState.UNKNOWN
    weekday: int = 0                  # 0 = Monday ... 4 = Friday
    week_high: float = 0.0
    week_low: float = 0.0
    week_high_time: object = None
    week_low_time: object = None
    seek_side: str = ""               # "UP" | "DOWN" — which extreme was raided
    seek_price: float = 0.0           # the extreme the raid printed
    seek_time: object = None
    seek_pips: float = 0.0
    direction: str = ""               # the REVERSAL being traded: "SHORT" | "LONG"
    destroy_price: float = 0.0
    destroy_time: object = None
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    rr: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state.value,
            "weekday": self.weekday,
            "week_high": round(self.week_high, 6),
            "week_low": round(self.week_low, 6),
            "week_high_time": self.week_high_time,
            "week_low_time": self.week_low_time,
            "seek_side": self.seek_side,
            "seek_price": round(self.seek_price, 6),
            "seek_time": self.seek_time,
            "seek_pips": round(self.seek_pips, 2),
            "direction": self.direction,
            "destroy_price": round(self.destroy_price, 6),
            "destroy_time": self.destroy_time,
            "stop_price": round(self.stop_price, 6),
            "target_price": round(self.target_price, 6),
            "risk_pips": round(self.risk_pips, 2),
            "reward_pips": round(self.reward_pips, 2),
            "rr": round(self.rr, 2),
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


def _open(candle: dict) -> float:
    return float(_val(candle, "open", "o", default=0.0))


def _high(candle: dict) -> float:
    return float(_val(candle, "high", "h", default=0.0))


def _low(candle: dict) -> float:
    return float(_val(candle, "low", "l", default=0.0))


def _close(candle: dict) -> float:
    return float(_val(candle, "close", "c", default=0.0))


def _day_index(ts: float) -> int:
    """Whole days since the epoch, UTC."""
    return int(ts // _SECONDS_PER_DAY)


def _weekday(ts: float) -> int:
    """0 = Monday ... 6 = Sunday.  Epoch day 0 was a Thursday, hence the +3."""
    return (_day_index(ts) + 3) % 7


def _monday_of(day: int) -> int:
    """The day index of the Monday of the week `day` falls in."""
    return day - ((day + 3) % 7)


# ── Engine ──────────────────────────────────────────────────────────────────

class FridaySeekDestroyEngine:
    """Per-symbol Friday raid state.  Pure: no clock, no I/O."""

    def __init__(self,
                 config: FridaySeekDestroyConfig = DEFAULT_FRIDAY_SEEK_DESTROY_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], FridaySeekDestroyRecord] = {}
        self._events: list[dict] = []

    # -- main entry point ---------------------------------------------------

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        day = _day_index(last_ts)
        record = FridaySeekDestroyRecord(
            symbol=symbol,
            timeframe=tf,
            weekday=_weekday(last_ts),
            created_at=last_ts,
        )

        if self.config.require_friday and record.weekday != _FRIDAY:
            record.state = FridaySeekDestroyState.NOT_FRIDAY
            return self._commit(record, tf, symbol)

        record = self._build(candles, record, day)
        return self._commit(record, tf, symbol)

    # -- computation --------------------------------------------------------

    def _build(self, candles: list[dict], record: FridaySeekDestroyRecord,
               day: int) -> FridaySeekDestroyRecord:
        cfg = self.config
        today: list[dict] = []
        earlier: list[dict] = []
        for candle in candles:
            ts = _time_key(candle)
            if ts is None:
                continue
            (today if _day_index(ts) == day else earlier).append(candle)

        monday = _monday_of(day)
        week = [c for c in earlier if _day_index(_time_key(c)) >= monday]
        if not week:
            record.state = FridaySeekDestroyState.NO_WEEK_RANGE
            return record

        high_candle = max(week, key=_high)
        low_candle = min(week, key=_low)
        record.week_high = _high(high_candle)
        record.week_high_time = _time_key(high_candle)
        record.week_low = _low(low_candle)
        record.week_low_time = _time_key(low_candle)

        if not today:
            record.state = FridaySeekDestroyState.WAITING
            return record

        reach = cfg.min_seek_pips * cfg.pip_size
        seek_i = None
        for i, candle in enumerate(today):
            # the high side is checked first, so a bar raiding both reads as UP
            if _high(candle) >= record.week_high + reach:
                record.seek_side = "UP"
                record.seek_price = _high(candle)
                record.seek_pips = (_high(candle) - record.week_high) / cfg.pip_size
                seek_i = i
                break
            if _low(candle) <= record.week_low - reach:
                record.seek_side = "DOWN"
                record.seek_price = _low(candle)
                record.seek_pips = (record.week_low - _low(candle)) / cfg.pip_size
                seek_i = i
                break

        if seek_i is None:
            record.state = FridaySeekDestroyState.WAITING
            return record

        record.seek_time = _time_key(today[seek_i])
        self._trade_levels(record)
        record.state = (FridaySeekDestroyState.SEEK_UP if record.seek_side == "UP"
                        else FridaySeekDestroyState.SEEK_DOWN)

        up = record.seek_side == "UP"
        for offset, candle in enumerate(today[seek_i + 1:]):
            if offset >= cfg.destroy_window_bars:
                record.state = FridaySeekDestroyState.INVALIDATED
                return record
            if cfg.destroy_confirm == "wick":
                back = _low(candle) < record.week_high if up else _high(candle) > record.week_low
            else:
                back = _close(candle) < record.week_high if up else _close(candle) > record.week_low
            if back:
                record.state = FridaySeekDestroyState.DESTROYED
                record.destroy_price = _close(candle)
                record.destroy_time = _time_key(candle)
                record.risk_pips = (abs(record.destroy_price - record.stop_price)
                                    / cfg.pip_size)
                record.reward_pips = (abs(record.target_price - record.destroy_price)
                                      / cfg.pip_size)
                record.rr = (record.reward_pips / record.risk_pips
                             if record.risk_pips > 0 else 0.0)
                return record
        return record

    def _trade_levels(self, record: FridaySeekDestroyRecord) -> None:
        """Stop beyond the seek extreme; target the far end of the week's range."""
        cfg = self.config
        buffer = cfg.stop_buffer_pips * cfg.pip_size
        height = record.week_high - record.week_low
        if record.seek_side == "UP":
            record.direction = "SHORT"
            record.stop_price = record.seek_price + buffer
            record.target_price = (record.week_low
                                   if cfg.target_mode != "week_extension"
                                   else record.week_low - height * cfg.target_extension)
        else:
            record.direction = "LONG"
            record.stop_price = record.seek_price - buffer
            record.target_price = (record.week_high
                                   if cfg.target_mode != "week_extension"
                                   else record.week_high + height * cfg.target_extension)

    # -- events -------------------------------------------------------------

    def _commit(self, record: FridaySeekDestroyRecord, tf: int, symbol: str) -> list[dict]:
        prev = self._state.get((symbol, tf))
        events = self._diff(record, prev)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _diff(self, record: FridaySeekDestroyRecord,
              prev: Optional[FridaySeekDestroyRecord]) -> list[dict]:
        if prev is not None and prev.state is record.state and prev.seek_price == record.seek_price:
            return []
        table = {
            FridaySeekDestroyState.SEEK_UP: FridaySeekDestroyEvent.SEEK_UP,
            FridaySeekDestroyState.SEEK_DOWN: FridaySeekDestroyEvent.SEEK_DOWN,
            FridaySeekDestroyState.DESTROYED: FridaySeekDestroyEvent.DESTROYED,
            FridaySeekDestroyState.INVALIDATED: FridaySeekDestroyEvent.INVALIDATED,
        }
        event = table.get(record.state)
        if event is None:
            return []
        return [self._event(event, record)]

    def _event(self, event: FridaySeekDestroyEvent,
               record: FridaySeekDestroyRecord, **extra) -> dict:
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
