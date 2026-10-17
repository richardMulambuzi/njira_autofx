"""
Mitigation-block detector (ICT register item).

Answers one question only: *did price come back to an untested order block and
respect it, or did it close straight through?*

This is the mirror image of `breaker_blocks`.  Breaker blocks track the FAILED
block; mitigation blocks track the block that HELD.  Price leaves a demand block,
later returns to it, and the question is whether that return is absorbed:

    fresh        : the block exists and price has not come back to it
    mitigated    : price reached the block's near edge WITHOUT closing through it
    invalidated  : a candle closed through the block's far edge instead
    expired      : the block was never touched within ``max_age_bars``

Orientation, so the near/far edges are unambiguous:
    bullish (demand) block -> near edge = block_high (price returns from above),
                              far edge  = block_low
    bearish (supply) block -> near edge = block_low  (price returns from below),
                              far edge  = block_high

``mitigation_touch_tolerance_ticks`` widens the near edge so a near-miss still
counts, and is measured in ticks counted in ``pip_size`` units (this repo has no
separate tick size).  Because touching and invalidating are separate checks, a
return that both reaches the zone and closes through it invalidates rather than
mitigates - the conservative reading.

Standalone by design (project rule §1): imports nothing but stdlib.  The block is
read out of the candles it is handed, so tuning this module can never change any
other detector's behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.mitigation_blocks")

CONCEPT_NAME = "mitigation_blocks"
CONCEPT_SOURCES = ("ict", "theinnercircletraders", "litefinance")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class MitigationBlockConfig:
    require_displacement: bool = True         # only blocks followed by a real impulse are considered
    displacement_mult: float = 1.5            # impulse size as a multiple of the block's own range
    impulse_bars: int = 1                     # bars that make up the impulse leg after the block
    mitigation_touch_tolerance_ticks: float = 1.0   # how close to the near edge still counts as a touch
    max_age_bars: int = 30                    # a block untouched this long is expired
    pip_size: float = 0.0001


DEFAULT_MITIGATION_BLOCK_CONFIG = MitigationBlockConfig()


# ── Result types ────────────────────────────────────────────────────────────

class MitigationBlockState(Enum):
    FRESH = "FRESH"
    MITIGATED = "MITIGATED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class MitigationBlockEvent(Enum):
    ZONE_FOUND = "MB_ZONE_FOUND"
    MITIGATED = "MB_MITIGATED"
    INVALIDATED = "MB_INVALIDATED"


@dataclass
class MitigationBlock:
    symbol: str = ""
    timeframe: int = 0
    direction: str = ""              # "BULLISH" (demand) | "BEARISH" (supply)
    block_time: object = None
    block_high: float = 0.0
    block_low: float = 0.0
    block_span_pips: float = 0.0
    displacement_pips: float = 0.0
    near_edge: float = 0.0           # the edge price must come back to
    far_edge: float = 0.0            # a close beyond this invalidates the block
    age_bars: int = 0
    mitigated: bool = False
    mitigation_time: object = None
    mitigation_price: float = 0.0
    invalidated: bool = False
    state: MitigationBlockState = MitigationBlockState.UNKNOWN
    current_price: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "block_time": self.block_time,
            "block_high": round(self.block_high, 6),
            "block_low": round(self.block_low, 6),
            "block_span_pips": round(self.block_span_pips, 2),
            "displacement_pips": round(self.displacement_pips, 2),
            "near_edge": round(self.near_edge, 6),
            "far_edge": round(self.far_edge, 6),
            "age_bars": self.age_bars,
            "mitigated": self.mitigated,
            "mitigation_time": self.mitigation_time,
            "mitigation_price": round(self.mitigation_price, 6),
            "invalidated": self.invalidated,
            "state": self.state.value,
            "current_price": round(self.current_price, 6),
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


def open_of(candle: dict) -> float:
    return float(_val(candle, "open", "o", default=0.0) or 0.0)


def high_of(candle: dict) -> float:
    return float(_val(candle, "high", "h", default=0.0) or 0.0)


def low_of(candle: dict) -> float:
    return float(_val(candle, "low", "l", default=0.0) or 0.0)


def close_of(candle: dict) -> float:
    return float(_val(candle, "close", "c", default=0.0) or 0.0)


def find_zone_indices(candles: list[dict], config: MitigationBlockConfig) -> Optional[int]:
    """Index of the most recent bar that qualifies as an order block, else None.

    A bullish block is the last DOWN candle before an up impulse; a bearish block
    is the last UP candle before a down impulse.  When ``require_displacement`` is
    on, the impulse must travel at least ``displacement_mult`` x the block's own
    range beyond the block's far edge.
    """
    last_block = None
    limit = len(candles) - 1 - max(config.impulse_bars, 1)
    for i in range(1, limit + 1):
        block = candles[i]
        impulse = candles[i + 1:i + 1 + config.impulse_bars]
        if not impulse:
            continue
        span = high_of(block) - low_of(block)
        if span <= 0:
            continue
        down = close_of(block) < open_of(block)
        up = close_of(block) > open_of(block)
        if down:
            displacement = max(high_of(c) for c in impulse) - high_of(block)
        elif up:
            displacement = low_of(block) - min(low_of(c) for c in impulse)
        else:
            continue
        if config.require_displacement and displacement < config.displacement_mult * span:
            continue
        if displacement <= 0:
            continue
        last_block = i
    return last_block


# ── Engine ──────────────────────────────────────────────────────────────────

class MitigationBlockEngine:
    """Per-symbol mitigation state.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: MitigationBlockConfig = DEFAULT_MITIGATION_BLOCK_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], MitigationBlock] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        cfg = self.config
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        index = find_zone_indices(candles, cfg)
        if index is None:
            return []
        block = candles[index]
        block_high = high_of(block)
        block_low = low_of(block)
        span = block_high - block_low
        if span <= 0:
            return []

        bullish = close_of(block) < open_of(block)
        direction = "BULLISH" if bullish else "BEARISH"
        near_edge = block_high if bullish else block_low
        far_edge = block_low if bullish else block_high

        impulse = candles[index + 1:index + 1 + cfg.impulse_bars]
        if bullish:
            displacement = max(high_of(c) for c in impulse) - block_high
        else:
            displacement = block_low - min(low_of(c) for c in impulse)

        tolerance = cfg.mitigation_touch_tolerance_ticks * cfg.pip_size
        last_index = len(candles) - 1
        mitigated = False
        mitigation_time = None
        mitigation_price = 0.0
        invalidated = False

        for j in range(index + 1 + cfg.impulse_bars, len(candles)):
            if j - index > cfg.max_age_bars:
                break
            bar = candles[j]
            if bullish:
                through = close_of(bar) < far_edge
                touched = low_of(bar) <= near_edge + tolerance
            else:
                through = close_of(bar) > far_edge
                touched = high_of(bar) >= near_edge - tolerance
            if through:
                invalidated = True
                break
            if touched and not mitigated:
                mitigated = True
                mitigation_time = _time_key(bar)
                mitigation_price = close_of(bar)

        age_bars = last_index - index
        if invalidated:
            state = MitigationBlockState.INVALIDATED
        elif mitigated:
            state = MitigationBlockState.MITIGATED
        elif age_bars > cfg.max_age_bars:
            state = MitigationBlockState.EXPIRED
        else:
            state = MitigationBlockState.FRESH

        record = MitigationBlock(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            block_time=_time_key(block),
            block_high=block_high,
            block_low=block_low,
            block_span_pips=span / cfg.pip_size,
            displacement_pips=displacement / cfg.pip_size,
            near_edge=near_edge,
            far_edge=far_edge,
            age_bars=age_bars,
            mitigated=mitigated,
            mitigation_time=mitigation_time,
            mitigation_price=mitigation_price,
            invalidated=invalidated,
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

    def _diff(self, record: MitigationBlock) -> list[dict]:
        key = (record.symbol, record.timeframe)
        prev: Optional[MitigationBlock] = self._state.get(key)
        new_zone = prev is None or prev.block_time != record.block_time

        events: list[dict] = []
        if new_zone:
            events.append(self._event(MitigationBlockEvent.ZONE_FOUND, record))
            if record.mitigated:
                events.append(self._event(MitigationBlockEvent.MITIGATED, record))
            if record.invalidated:
                events.append(self._event(MitigationBlockEvent.INVALIDATED, record))
            return events

        if record.mitigated and not prev.mitigated:
            events.append(self._event(MitigationBlockEvent.MITIGATED, record))
        if record.invalidated and not prev.invalidated:
            events.append(self._event(MitigationBlockEvent.INVALIDATED, record))
        return events

    def _event(self, event: MitigationBlockEvent, record: MitigationBlock, **extra) -> dict:
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
