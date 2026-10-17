"""
Power of Three / AMD detector (ICT register item).

Answers one question only: *is this session still accumulating, has it just
manipulated, or has it begun to distribute?*

    accumulation : the opening stretch of the session holds a tight range
    manipulation : price runs beyond that range by at least ``sweep_depth_min``
                   pips (the stop run / false move)
    distribution : price delivers back through the range on the OPPOSITE side
                   of the manipulation (fake up -> real down)

The two gates are deliberately separate so they can be tuned apart: a bar is a
manipulation CANDIDATE only when it leaves the box by more than
``accumulation_range_ratio`` of the box span, and is confirmed MANIPULATION only
when it also clears ``sweep_depth_min`` pips.

Standalone by design (project rule §1): imports stdlib plus ``sessions`` only,
and reads the box out of the candles it is handed.  Tuning this module can never
change any other detector's behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from forex.analysis import sessions

logger = logging.getLogger("forex.analysis.power_of_three")

CONCEPT_NAME = "power_of_three"
CONCEPT_SOURCES = ("ict", "theinnercircletraders", "litefinance")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class PowerOfThreeConfig:
    session: str = "london"                # which session the AMD cycle plays out in
    accumulation_range_ratio: float = 0.6   # max box overrun (as a fraction of box span) that still counts as accumulation
    sweep_depth_min: float = 3.0            # pips price must run beyond the box before it counts as the manipulation
    min_phase_bars: int = 3                 # bars that seed the accumulation box
    close_confirm: bool = True              # distribution needs a CLOSE back inside the box, not just a wick
    pip_size: float = 0.0001
    min_range_pips: float = 5.0


DEFAULT_POWER_OF_THREE_CONFIG = PowerOfThreeConfig()


# ── Result types ────────────────────────────────────────────────────────────

class PowerOfThreeState(Enum):
    ACCUMULATION = "ACCUMULATION"
    MANIPULATION = "MANIPULATION"
    DISTRIBUTION = "DISTRIBUTION"
    UNKNOWN = "UNKNOWN"


class PowerOfThreeEvent(Enum):
    ACCUMULATION_SET = "P3_ACCUMULATION_SET"
    MANIPULATION = "P3_MANIPULATION"
    DISTRIBUTION = "P3_DISTRIBUTION"


@dataclass
class PowerOfThree:
    symbol: str = ""
    timeframe: int = 0
    session: str = ""
    session_start_time: object = None
    phase: PowerOfThreeState = PowerOfThreeState.UNKNOWN
    bars_in_session: int = 0
    box_high: float = 0.0
    box_low: float = 0.0
    box_span_pips: float = 0.0
    manipulation_side: str = ""          # "UP" | "DOWN" | ""
    manipulation_extreme: float = 0.0
    manipulation_pips: float = 0.0
    manipulation_time: object = None
    distribution_direction: str = ""     # opposite of the manipulation side
    distribution_confirmed: bool = False
    distribution_target: float = 0.0
    current_price: float = 0.0
    in_killzone: bool = False            # confluence evidence only, never a gate
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "session": self.session,
            "session_start_time": self.session_start_time,
            "phase": self.phase.value,
            "bars_in_session": self.bars_in_session,
            "box_high": round(self.box_high, 6),
            "box_low": round(self.box_low, 6),
            "box_span_pips": round(self.box_span_pips, 2),
            "manipulation_side": self.manipulation_side,
            "manipulation_extreme": round(self.manipulation_extreme, 6),
            "manipulation_pips": round(self.manipulation_pips, 2),
            "manipulation_time": self.manipulation_time,
            "distribution_direction": self.distribution_direction,
            "distribution_confirmed": self.distribution_confirmed,
            "distribution_target": round(self.distribution_target, 6),
            "current_price": round(self.current_price, 6),
            "in_killzone": self.in_killzone,
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


def high_of(candle: dict) -> float:
    return float(_val(candle, "high", "h", default=0.0) or 0.0)


def low_of(candle: dict) -> float:
    return float(_val(candle, "low", "l", default=0.0) or 0.0)


def close_of(candle: dict) -> float:
    return float(_val(candle, "close", "c", default=0.0) or 0.0)


def session_indices(candles: list[dict], session: str) -> Optional[tuple]:
    """(first, last) of the MOST RECENT contiguous run of bars inside `session`."""
    runs: list[list[int]] = []
    run: list[int] = []
    for i, candle in enumerate(candles):
        ts = _time_key(candle)
        inside = ts is not None and session in (sessions.session_of(ts) or [])
        if inside:
            run.append(i)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    if not runs:
        return None
    return runs[-1][0], runs[-1][-1]


def box_extremes(candles: list[dict], first: int, count: int) -> tuple:
    """(high, low) over `count` bars starting at `first`."""
    window = candles[first:first + count]
    return max(high_of(c) for c in window), min(low_of(c) for c in window)


# ── Engine ──────────────────────────────────────────────────────────────────

class PowerOfThreeEngine:
    """Per-symbol AMD phase.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: PowerOfThreeConfig = DEFAULT_POWER_OF_THREE_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], PowerOfThree] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        cfg = self.config
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        found = session_indices(candles, cfg.session)
        if found is None:
            return []
        first, last = found
        bars_in_session = last - first + 1
        if bars_in_session < cfg.min_phase_bars:
            return []

        box_high, box_low = box_extremes(candles, first, cfg.min_phase_bars)
        span = box_high - box_low
        if span <= 0 or span / cfg.pip_size < cfg.min_range_pips:
            return []

        ratio_limit = cfg.accumulation_range_ratio * span
        depth_price = cfg.sweep_depth_min * cfg.pip_size

        phase = PowerOfThreeState.ACCUMULATION
        side = ""
        extreme = 0.0
        depth_pips = 0.0
        manipulation_time = None
        manipulation_index = None

        for i in range(first + cfg.min_phase_bars, last + 1):
            bar = candles[i]
            over_high = high_of(bar) - box_high
            over_low = box_low - low_of(bar)
            extend = max(over_high, over_low, 0.0)
            if extend <= ratio_limit or extend < depth_price:
                continue
            side = "UP" if over_high >= over_low else "DOWN"
            extreme = high_of(bar) if side == "UP" else low_of(bar)
            depth_pips = extend / cfg.pip_size
            manipulation_time = _time_key(bar)
            manipulation_index = i
            phase = PowerOfThreeState.MANIPULATION
            break

        direction = ""
        confirmed = False
        target = 0.0
        if manipulation_index is not None:
            for j in range(manipulation_index + 1, last + 1):
                bar = candles[j]
                if cfg.close_confirm:
                    back_inside = box_low <= close_of(bar) <= box_high
                else:
                    back_inside = high_of(bar) >= box_low and low_of(bar) <= box_high
                if back_inside:
                    confirmed = True
                    direction = "DOWN" if side == "UP" else "UP"
                    target = box_low if direction == "DOWN" else box_high
                    break
            if confirmed:
                phase = PowerOfThreeState.DISTRIBUTION

        last_time = _time_key(candles[-1])
        record = PowerOfThree(
            symbol=symbol,
            timeframe=tf,
            session=cfg.session,
            session_start_time=_time_key(candles[first]),
            phase=phase,
            bars_in_session=bars_in_session,
            box_high=box_high,
            box_low=box_low,
            box_span_pips=span / cfg.pip_size,
            manipulation_side=side,
            manipulation_extreme=extreme,
            manipulation_pips=depth_pips,
            manipulation_time=manipulation_time,
            distribution_direction=direction,
            distribution_confirmed=confirmed,
            distribution_target=target,
            current_price=close_of(candles[-1]),
            in_killzone=bool(sessions.in_killzone(last_time)) if last_time is not None else False,
            created_at=last_time,
        )

        events = self._diff(record)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _diff(self, record: PowerOfThree) -> list[dict]:
        key = (record.symbol, record.timeframe)
        prev: Optional[PowerOfThree] = self._state.get(key)
        new_session = prev is None or prev.session_start_time != record.session_start_time

        events: list[dict] = []
        if new_session:
            events.append(self._event(PowerOfThreeEvent.ACCUMULATION_SET, record))

        previous_side = "" if new_session else prev.manipulation_side
        if record.manipulation_side and record.manipulation_side != previous_side:
            events.append(self._event(PowerOfThreeEvent.MANIPULATION, record))

        if record.phase is PowerOfThreeState.DISTRIBUTION and (new_session or prev.phase is not PowerOfThreeState.DISTRIBUTION):
            events.append(self._event(PowerOfThreeEvent.DISTRIBUTION, record))
        return events

    def _event(self, event: PowerOfThreeEvent, record: PowerOfThree, **extra) -> dict:
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
