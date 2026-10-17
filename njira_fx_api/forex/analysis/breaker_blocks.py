"""
Breaker-block detector (ICT register item).

Answers one question only: *has an order block been closed through, and if so has
price come back to retest it from the other side?*

An ICT breaker block is an order block that FAILED.  Price forms a demand block,
then closes decisively through it; the block that used to hold price now caps it,
so the same zone is read from the opposite side.  Tracking that flip is the whole
job of this module.

    fresh    : the block exists and price has not closed through it yet
    breached : a candle closed through the far edge by more than the buffer
    flipped  : after the breach, price traded back into the zone (the retest)

The buffer is measured in ticks (counted in ``pip_size`` units - this repo has no
separate tick size).  The impulse that created the block is only demanded when
``require_displacement`` is on, and its size is a multiple of the block's OWN
range, so the gate adapts to volatility instead of a fixed number of pips.

Standalone by design (project rule §1): imports nothing but stdlib.  The block is
read out of the candles it is handed, so tuning this module can never change any
other detector's behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.breaker_blocks")

CONCEPT_NAME = "breaker_blocks"
CONCEPT_SOURCES = ("ict", "theinnercircletraders", "litefinance")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class BreakerBlockConfig:
    close_through_buffer_ticks: float = 1.0   # how far past the far edge a close must land to count
    require_displacement: bool = True         # only blocks followed by a real impulse are considered
    displacement_mult: float = 1.5            # impulse size as a multiple of the block's own range
    impulse_bars: int = 1                     # bars that make up the impulse leg after the block
    max_bars_to_breach: int = 20              # a close-through older than this no longer counts
    zone_expiry_bars: int = 60                # a block that was never breached is dropped after this
    pip_size: float = 0.0001


DEFAULT_BREAKER_BLOCK_CONFIG = BreakerBlockConfig()


# ── Result types ────────────────────────────────────────────────────────────

class BreakerBlockState(Enum):
    FRESH = "FRESH"
    BREACHED = "BREACHED"
    ROLE_FLIPPED = "ROLE_FLIPPED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class BreakerBlockEvent(Enum):
    ZONE_FOUND = "BB_ZONE_FOUND"
    BREACHED = "BB_BREACHED"
    ROLE_FLIPPED = "BB_ROLE_FLIPPED"


@dataclass
class BreakerBlock:
    symbol: str = ""
    timeframe: int = 0
    direction: str = ""                  # direction of the ORIGINAL block: "BULLISH" (demand) | "BEARISH" (supply)
    role: str = ""                       # "DEMAND" while fresh, "SUPPLY" once flipped (and vice versa)
    block_time: object = None
    block_high: float = 0.0
    block_low: float = 0.0
    block_span_pips: float = 0.0
    displacement_pips: float = 0.0
    age_bars: int = 0
    breached: bool = False
    breach_time: object = None
    breach_close: float = 0.0
    breach_bars_after_block: int = 0
    retested: bool = False
    state: BreakerBlockState = BreakerBlockState.UNKNOWN
    current_price: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "role": self.role,
            "block_time": self.block_time,
            "block_high": round(self.block_high, 6),
            "block_low": round(self.block_low, 6),
            "block_span_pips": round(self.block_span_pips, 2),
            "displacement_pips": round(self.displacement_pips, 2),
            "age_bars": self.age_bars,
            "breached": self.breached,
            "breach_time": self.breach_time,
            "breach_close": round(self.breach_close, 6),
            "breach_bars_after_block": self.breach_bars_after_block,
            "retested": self.retested,
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


def find_zone_indices(candles: list[dict], config: BreakerBlockConfig) -> Optional[int]:
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

class BreakerBlockEngine:
    """Per-symbol breaker-block state.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: BreakerBlockConfig = DEFAULT_BREAKER_BLOCK_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], BreakerBlock] = {}
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
        span = high_of(block) - low_of(block)
        if span <= 0:
            return []

        bullish = close_of(block) < open_of(block)
        direction = "BULLISH" if bullish else "BEARISH"
        block_high = high_of(block)
        block_low = low_of(block)
        impulse = candles[index + 1:index + 1 + cfg.impulse_bars]
        if bullish:
            displacement = max(high_of(c) for c in impulse) - block_high
        else:
            displacement = block_low - min(low_of(c) for c in impulse)

        buffer = cfg.close_through_buffer_ticks * cfg.pip_size
        last_index = len(candles) - 1

        breached = False
        breach_time = None
        breach_close = 0.0
        breach_bars_after_block = 0
        breach_index = None
        retested = False

        for j in range(index + 1 + cfg.impulse_bars, len(candles)):
            bar = candles[j]
            bars_after = j - index
            if not breached and bars_after <= cfg.max_bars_to_breach:
                close = close_of(bar)
                if bullish and close < block_low - buffer:
                    breached = True
                elif not bullish and close > block_high + buffer:
                    breached = True
                if breached:
                    breach_time = _time_key(bar)
                    breach_close = close
                    breach_bars_after_block = bars_after
                    breach_index = j
            elif breached and breach_index is not None and j > breach_index:
                if high_of(bar) >= block_low and low_of(bar) <= block_high:
                    retested = True

        age_bars = last_index - index
        if breached and retested:
            state = BreakerBlockState.ROLE_FLIPPED
        elif breached:
            state = BreakerBlockState.BREACHED
        elif age_bars > cfg.zone_expiry_bars:
            state = BreakerBlockState.EXPIRED
        else:
            state = BreakerBlockState.FRESH

        original_role = "DEMAND" if bullish else "SUPPLY"
        role = original_role if not breached else ("SUPPLY" if bullish else "DEMAND")

        record = BreakerBlock(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            role=role,
            block_time=_time_key(block),
            block_high=block_high,
            block_low=block_low,
            block_span_pips=span / cfg.pip_size,
            displacement_pips=displacement / cfg.pip_size,
            age_bars=age_bars,
            breached=breached,
            breach_time=breach_time,
            breach_close=breach_close,
            breach_bars_after_block=breach_bars_after_block,
            retested=retested,
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

    def _diff(self, record: BreakerBlock) -> list[dict]:
        key = (record.symbol, record.timeframe)
        prev: Optional[BreakerBlock] = self._state.get(key)
        new_zone = prev is None or prev.block_time != record.block_time

        events: list[dict] = []
        if new_zone:
            events.append(self._event(BreakerBlockEvent.ZONE_FOUND, record))
            if record.breached:
                events.append(self._event(BreakerBlockEvent.BREACHED, record))
            if record.retested:
                events.append(self._event(BreakerBlockEvent.ROLE_FLIPPED, record))
            return events

        if record.breached and not prev.breached:
            events.append(self._event(BreakerBlockEvent.BREACHED, record))
        if record.retested and not prev.retested:
            events.append(self._event(BreakerBlockEvent.ROLE_FLIPPED, record))
        return events

    def _event(self, event: BreakerBlockEvent, record: BreakerBlock, **extra) -> dict:
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
