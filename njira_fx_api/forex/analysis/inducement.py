"""
Inducement (IDM) detector (ICT register item).

Answers one question only: *is the pullback price is currently making the fake
move that traps early retail entries, and has it been taken yet?*

The smart-money move starts by consuming a MINOR pool that sits in front of the
MAJOR pool.  That minor pool is the inducement: it reads as a reversal (a lower
low inside an uptrend), entries pile in against it, and the supply those entries
create is what the real move eats on the way to the major pool.  Committing to
the major sweep without waiting for the inducement to be taken is the classic
early entry this module exists to flag.

Four states:

    identified  : a minor pool sits in front of a major pool and is untouched
    swept       : price traded through the minor pool, so the trap has sprung
    held        : price recovered away from the minor pool without taking it
    expired     : the minor pool was never taken within ``max_age_bars``

Direction names the REAL move implied by the fake one, matching ``judas_swing``:
    BULLISH  -> real move up, so the inducement is a minor LOW taken by a fake
                push down
    BEARISH  -> real move down, so the inducement is a minor HIGH taken by a
                fake push up

Fallbacks, so thin data still yields a usable read instead of silence:
    1. no swing confirmed at ``pivot_lookback`` retries once at
       ``fallback_pivot_lookback`` before giving up
    2. if ``require_major_pool`` is on but no major pool lies beyond the minor
       one, the record is still emitted with ``has_major_pool=False`` rather
       than dropped, leaving the judgement to the caller
    3. a swept minor pool is still reported when no major pool was ever found

The minor and major pools are read out of the candles handed in; this module
never imports another detector.

Standalone by design (project rule 1): imports nothing but stdlib.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.inducement")

CONCEPT_NAME = "inducement"
CONCEPT_SOURCES = ("ict", "theinnercircletraders")


@dataclass(frozen=True)
class InducementConfig:
    pivot_lookback: int = 3                   # bars either side needed to confirm a swing
    fallback_pivot_lookback: int = 1          # retried when no swing confirms at the primary lookback
    sweep_tolerance_ticks: float = 1.0        # how far past the minor pool still counts as untouched
    hold_recovery_pips: float = 10.0          # advance away from the minor pool that counts as held
    require_major_pool: bool = True           # only call a minor pool inducement if a major pool lies beyond
    min_gap_ticks: float = 5.0                # minimum minor-to-major pool separation
    max_age_bars: int = 40                    # a minor pool this old is expired
    pip_size: float = 0.0001


DEFAULT_INDUCEMENT_CONFIG = InducementConfig()


class InducementState(Enum):
    IDENTIFIED = "IDENTIFIED"
    SWEPT = "SWEPT"
    HELD = "HELD"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class InducementEvent(Enum):
    IDENTIFIED = "IDM_IDENTIFIED"
    SWEPT = "IDM_SWEPT"
    HELD = "IDM_HELD"
    EXPIRED = "IDM_EXPIRED"


@dataclass
class Inducement:
    symbol: str = ""
    timeframe: int = 0
    direction: str = ""                 # "BULLISH" | "BEARISH", the REAL move
    minor_pool: float = 0.0             # the inducing pool price
    major_pool: float = 0.0             # the pool the real move is aiming for
    has_major_pool: bool = False
    swing_index: int = 0
    swing_time: object = None
    minor_to_major_ticks: float = 0.0
    swept: bool = False
    sweep_time: object = None
    sweep_price: float = 0.0
    held: bool = False
    recovery_pips: float = 0.0
    age_bars: int = 0
    state: InducementState = InducementState.UNKNOWN
    current_price: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "minor_pool": round(self.minor_pool, 6),
            "major_pool": round(self.major_pool, 6),
            "has_major_pool": self.has_major_pool,
            "swing_index": self.swing_index,
            "swing_time": self.swing_time,
            "minor_to_major_ticks": round(self.minor_to_major_ticks, 2),
            "swept": self.swept,
            "sweep_time": self.sweep_time,
            "sweep_price": round(self.sweep_price, 6),
            "held": self.held,
            "recovery_pips": round(self.recovery_pips, 2),
            "age_bars": self.age_bars,
            "state": self.state.value,
            "current_price": round(self.current_price, 6),
        }


def _val(candle: dict, *keys, default=None):
    if not isinstance(candle, dict):
        return default
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    return _val(candle, "open_time", "time", "timestamp", "date")


def open_of(candle: dict) -> float:
    return float(_val(candle, "open", "o", default=0.0) or 0.0)


def high_of(candle: dict) -> float:
    return float(_val(candle, "high", "h", default=0.0) or 0.0)


def low_of(candle: dict) -> float:
    return float(_val(candle, "low", "l", default=0.0) or 0.0)


def close_of(candle: dict) -> float:
    return float(_val(candle, "close", "c", default=0.0) or 0.0)


def swing_lows(candles: list[dict], pivot: int) -> list[tuple[int, float]]:
    """Indices of confirmed swing lows: the lowest low within ``pivot`` bars either side."""
    out: list[tuple[int, float]] = []
    pivot = max(pivot, 1)
    for i in range(pivot, len(candles) - pivot):
        price = low_of(candles[i])
        window = candles[i - pivot:i + pivot + 1]
        if price <= min(low_of(c) for c in window):
            out.append((i, price))
    return out


def swing_highs(candles: list[dict], pivot: int) -> list[tuple[int, float]]:
    """Indices of confirmed swing highs: the highest high within ``pivot`` bars either side."""
    out: list[tuple[int, float]] = []
    pivot = max(pivot, 1)
    for i in range(pivot, len(candles) - pivot):
        price = high_of(candles[i])
        window = candles[i - pivot:i + pivot + 1]
        if price >= max(high_of(c) for c in window):
            out.append((i, price))
    return out


def latest_inducement(candles: list[dict], config: InducementConfig) -> Optional[dict]:
    """The minor pool closest to price that still has a major pool behind it.

    A bullish read takes swing lows: the most recent one is the minor pool and the
    cheapest low beneath it is the major pool.  A bearish read mirrors that on
    swing highs.  Returns ``None`` only when no swing exists at either lookback.
    """
    lows = swing_lows(candles, config.pivot_lookback)
    highs = swing_highs(candles, config.pivot_lookback)
    if not lows and not highs:
        lows = swing_lows(candles, config.fallback_pivot_lookback)
        highs = swing_highs(candles, config.fallback_pivot_lookback)
    if not lows and not highs:
        return None

    last_index = len(candles) - 1
    candidates: list[dict] = []

    if lows:
        swing_index, minor = lows[-1]
        beneath = [p for _, p in lows if p < minor - config.min_gap_ticks * config.pip_size]
        major = min(beneath) if beneath else 0.0
        candidates.append({
            "direction": "BULLISH",
            "minor_pool": minor,
            "major_pool": major,
            "has_major_pool": bool(beneath),
            "swing_index": swing_index,
            "swing_time": _time_key(candles[swing_index]),
            "distance": last_index - swing_index,
        })

    if highs:
        swing_index, minor = highs[-1]
        above = [p for _, p in highs if p > minor + config.min_gap_ticks * config.pip_size]
        major = max(above) if above else 0.0
        candidates.append({
            "direction": "BEARISH",
            "minor_pool": minor,
            "major_pool": major,
            "has_major_pool": bool(above),
            "swing_index": swing_index,
            "swing_time": _time_key(candles[swing_index]),
            "distance": last_index - swing_index,
        })

    usable = candidates
    if config.require_major_pool:
        with_major = [c for c in candidates if c["has_major_pool"]]
        if with_major:
            usable = with_major
    if not usable:
        return None
    usable.sort(key=lambda c: (c["distance"], -abs(c["minor_pool"])))
    return usable[0]


class InducementEngine:
    """Per-symbol inducement state.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: InducementConfig = DEFAULT_INDUCEMENT_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], Inducement] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 3:
            return []
        cfg = self.config
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        found = latest_inducement(candles, cfg)
        if found is None:
            return []

        bullish = found["direction"] == "BULLISH"
        minor = found["minor_pool"]
        tolerance = cfg.sweep_tolerance_ticks * cfg.pip_size
        swing_index = found["swing_index"]
        last_index = len(candles) - 1

        swept = False
        sweep_time = None
        sweep_price = 0.0
        best_recovery = 0.0

        for j in range(swing_index + 1, len(candles)):
            if j - swing_index > cfg.max_age_bars:
                break
            bar = candles[j]
            if bullish:
                if low_of(bar) < minor - tolerance:
                    swept = True
                    sweep_time = _time_key(bar)
                    sweep_price = low_of(bar)
                    break
                best_recovery = max(best_recovery, high_of(bar) - minor)
            else:
                if high_of(bar) > minor + tolerance:
                    swept = True
                    sweep_time = _time_key(bar)
                    sweep_price = high_of(bar)
                    break
                best_recovery = max(best_recovery, minor - low_of(bar))

        age_bars = last_index - swing_index
        held = (not swept) and best_recovery >= cfg.hold_recovery_pips * cfg.pip_size
        if swept:
            state = InducementState.SWEPT
        elif held:
            state = InducementState.HELD
        elif age_bars > cfg.max_age_bars:
            state = InducementState.EXPIRED
        else:
            state = InducementState.IDENTIFIED

        record = Inducement(
            symbol=symbol,
            timeframe=tf,
            direction=found["direction"],
            minor_pool=minor,
            major_pool=found["major_pool"],
            has_major_pool=found["has_major_pool"],
            swing_index=swing_index,
            swing_time=found["swing_time"],
            minor_to_major_ticks=(abs(minor - found["major_pool"]) / cfg.pip_size
                                  if found["has_major_pool"] else 0.0),
            swept=swept,
            sweep_time=sweep_time,
            sweep_price=sweep_price,
            held=held,
            recovery_pips=best_recovery / cfg.pip_size,
            age_bars=age_bars,
            state=state,
            current_price=close_of(candles[-1]),
            created_at=_time_key(candles[-1]),
        )

        events = self._diff(record)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _diff(self, record: Inducement) -> list[dict]:
        key = (record.symbol, record.timeframe)
        prev: Optional[Inducement] = self._state.get(key)
        new_pool = prev is None or prev.swing_index != record.swing_index

        events: list[dict] = []
        if new_pool:
            events.append(self._event(InducementEvent.IDENTIFIED, record))
            if record.swept:
                events.append(self._event(InducementEvent.SWEPT, record))
            elif record.held:
                events.append(self._event(InducementEvent.HELD, record))
            return events

        if record.swept and not prev.swept:
            events.append(self._event(InducementEvent.SWEPT, record))
        elif record.held and not prev.held:
            events.append(self._event(InducementEvent.HELD, record))
        if record.state is InducementState.EXPIRED and prev.state is not InducementState.EXPIRED:
            events.append(self._event(InducementEvent.EXPIRED, record))
        return events

    def _event(self, event: InducementEvent, record: Inducement, **extra) -> dict:
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
