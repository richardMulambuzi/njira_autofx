"""
Thirty-pips-a-day detector (ICT's one-trade-a-day scalping discipline).

Two rules define this model and they are both about EXITS and RESTRAINT, not
about entries:

    1. the target is a FIXED number of pips (default 30) — not a market
       structure level — so the trade is finished early in the session;
    2. ONE setup per day.  The first one that resolves (win or loss) takes the
       day off the table and every later setup that day reads DONE_FOR_DAY.

The entry itself is the ordinary ICT pullback: after a swing leg, price comes
back into the OTE band (62%–79% of the leg) and that is where the trade is taken.
The band edges are Config, and the two-source conflict (ICT 62–79% vs the
50–61.8% others teach) is deliberately NOT resolved here — it owns itself in
``ote.py`` / ``premium_discount.py``.

Bias arrives INJECTED as a plain string or dict.  The kill zone is read from
``forex.analysis.sessions`` (the one permitted import); nothing here guesses a
timezone.

Standalone by design (project rule §1): imports stdlib plus ``sessions`` only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from forex.analysis import sessions

logger = logging.getLogger("forex.analysis.thirty_pips_day")

CONCEPT_NAME = "thirty_pips_day"
CONCEPT_SOURCES = ("ict", "tradingfinder")

_AGREE_LONG = ("BULLISH", "LONG", "UP", "BUY")
_AGREE_SHORT = ("BEARISH", "SHORT", "DOWN", "SELL")
_SECONDS_PER_DAY = 86400


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class ThirtyPipsDayConfig:
    pip_size: float = 0.0001
    target_pips: float = 30.0          # the whole point of the model: a fixed exit
    band_low: float = 0.62             # shallower OTE edge
    band_high: float = 0.79            # deeper OTE edge
    swing_lookback: int = 5            # bars either side that confirm a swing
    leg_lookback: int = 100            # max bars between the two leg extremes
    min_leg_pips: float = 20.0         # a leg smaller than this is noise
    stop_buffer_pips: float = 2.0      # stop sits this far beyond the leg extreme
    require_bias: bool = False
    require_killzone: bool = True      # ICT only trades these inside London / NY KZ


DEFAULT_THIRTY_PIPS_DAY_CONFIG = ThirtyPipsDayConfig()


# ── Result types ────────────────────────────────────────────────────────────

class ThirtyPipsDayState(Enum):
    NO_LEG = "NO_LEG"
    WAITING = "WAITING"                # leg priced, price not yet at the band
    IN_BAND = "IN_BAND"
    GATE_BLOCKED = "GATE_BLOCKED"
    INVALIDATED = "INVALIDATED"
    TARGET_HIT = "TARGET_HIT"
    DONE_FOR_DAY = "DONE_FOR_DAY"      # the day's one trade is already resolved
    UNKNOWN = "UNKNOWN"


class ThirtyPipsDayEvent(Enum):
    LEG_SET = "TPD_LEG_SET"
    IN_BAND = "TPD_IN_BAND"
    GATE_BLOCKED = "TPD_GATE_BLOCKED"
    INVALIDATED = "TPD_INVALIDATED"
    TARGET_HIT = "TPD_TARGET_HIT"
    DONE_FOR_DAY = "TPD_DONE_FOR_DAY"


@dataclass
class ThirtyPipsDayRecord:
    symbol: str = ""
    timeframe: int = 0
    state: ThirtyPipsDayState = ThirtyPipsDayState.UNKNOWN
    direction: str = ""                # "LONG" | "SHORT"
    day: int = 0
    leg_low: float = 0.0
    leg_high: float = 0.0
    leg_pips: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    in_band: bool = False
    in_killzone: bool = False
    bias: str = ""
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state.value,
            "direction": self.direction,
            "day": self.day,
            "leg_low": round(self.leg_low, 6),
            "leg_high": round(self.leg_high, 6),
            "leg_pips": round(self.leg_pips, 2),
            "entry_price": round(self.entry_price, 6),
            "stop_price": round(self.stop_price, 6),
            "target_price": round(self.target_price, 6),
            "risk_pips": round(self.risk_pips, 2),
            "reward_pips": round(self.reward_pips, 2),
            "in_band": self.in_band,
            "in_killzone": self.in_killzone,
            "bias": self.bias,
        }


# ── Pure helpers ────────────────────────────────────────────────────────────

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


def _swing_indices(candles: list[dict], lookback: int, low: bool) -> list[int]:
    """Confirmed swing indices: the bar needs ``lookback`` quiet bars either side."""
    out = []
    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback:i + lookback + 1]
        if low:
            if _low(candles[i]) <= min(_low(c) for c in window):
                out.append(i)
        else:
            if _high(candles[i]) >= max(_high(c) for c in window):
                out.append(i)
    return out


def _leg(candles: list[dict], lookback: int,
         leg_lookback: int) -> Optional[tuple]:
    """(start_i, end_i, leg_low, leg_high, direction) for the most recent leg.

    The newer of the last confirmed swing high / swing low ends the leg, so the
    two extremes alternate the way a real impulse does.
    """
    lows = _swing_indices(candles, lookback, low=True)
    highs = _swing_indices(candles, lookback, low=False)
    if not lows or not highs:
        return None
    last_low, last_high = lows[-1], highs[-1]
    if last_high > last_low:
        start, end, direction = last_low, last_high, "LONG"
    else:
        start, end, direction = last_high, last_low, "SHORT"
    if end - start > leg_lookback:
        return None
    return start, end, _low(candles[start]), _high(candles[end]), direction


def _bias_direction(bias) -> str:
    """Normalise an injected bias into an upper-case direction string."""
    if bias is None:
        return ""
    if isinstance(bias, str):
        return bias.upper()
    if isinstance(bias, dict):
        for key in ("allowed_direction", "bias", "direction", "state", "verdict"):
            if bias.get(key):
                value = bias[key]
                return str(getattr(value, "value", value)).upper()
        return ""
    for key in ("allowed_direction", "bias", "direction", "state", "verdict"):
        value = getattr(bias, key, None)
        if value:
            return str(getattr(value, "value", value)).upper()
    return ""


# ── Engine ──────────────────────────────────────────────────────────────────

class ThirtyPipsDayEngine:
    """One fixed-target setup per day.  Pure: no clock, no I/O."""

    def __init__(self, config: ThirtyPipsDayConfig = DEFAULT_THIRTY_PIPS_DAY_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], ThirtyPipsDayRecord] = {}
        self._events: list[dict] = []
        self._resolved_day: dict[tuple[str, int], int] = {}

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "",
                bias=None) -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        cfg = self.config
        day = int(last_ts // _SECONDS_PER_DAY)
        record = ThirtyPipsDayRecord(
            symbol=symbol, timeframe=tf, day=day,
            in_killzone=sessions.in_killzone(last_ts),
            bias=_bias_direction(bias),
            created_at=last_ts,
        )

        # rule 2: the day's one trade is already spent
        if self._resolved_day.get((symbol, tf)) == day:
            record.state = ThirtyPipsDayState.DONE_FOR_DAY
            return self._commit(record, tf, symbol)

        if cfg.require_killzone and not record.in_killzone:
            record.state = ThirtyPipsDayState.GATE_BLOCKED
            return self._commit(record, tf, symbol)

        if cfg.require_bias and not record.bias:
            return []

        leg = _leg(candles, cfg.swing_lookback, cfg.leg_lookback)
        if leg is None:
            record.state = ThirtyPipsDayState.NO_LEG
            return self._commit(record, tf, symbol)

        start, end, leg_low, leg_high, direction = leg
        record.direction = direction
        record.leg_low, record.leg_high = leg_low, leg_high
        span = leg_high - leg_low
        leg_pips = span / cfg.pip_size
        record.leg_pips = leg_pips
        if leg_pips < cfg.min_leg_pips:
            record.state = ThirtyPipsDayState.NO_LEG
            return self._commit(record, tf, symbol)

        if cfg.require_bias:
            agree = _AGREE_LONG if direction == "LONG" else _AGREE_SHORT
            if record.bias not in agree:
                record.state = ThirtyPipsDayState.GATE_BLOCKED
                return self._commit(record, tf, symbol)

        buffer = cfg.stop_buffer_pips * cfg.pip_size
        if direction == "LONG":
            record.entry_price = leg_high - cfg.band_low * span
            record.stop_price = leg_low - buffer
            record.target_price = record.entry_price + cfg.target_pips * cfg.pip_size
        else:
            record.entry_price = leg_low + cfg.band_low * span
            record.stop_price = leg_high + buffer
            record.target_price = record.entry_price - cfg.target_pips * cfg.pip_size
        record.risk_pips = abs(record.entry_price - record.stop_price) / cfg.pip_size
        record.reward_pips = cfg.target_pips

        # Two phases, in this order, on purpose: the trade cannot win or lose
        # before it is filled.  Phase 1 finds the fill (or a stop-out while we
        # wait); only phase 2, from the fill onwards, looks for the exit.
        entry_i = None
        record.state = ThirtyPipsDayState.WAITING
        for k in range(end + 1, len(candles)):
            candle = candles[k]
            if direction == "LONG":
                if _close(candle) < record.stop_price:
                    record.state = ThirtyPipsDayState.INVALIDATED
                    break
                if _low(candle) <= record.entry_price:
                    entry_i = k
                    break
            else:
                if _close(candle) > record.stop_price:
                    record.state = ThirtyPipsDayState.INVALIDATED
                    break
                if _high(candle) >= record.entry_price:
                    entry_i = k
                    break

        if entry_i is not None:
            record.in_band = True
            record.state = ThirtyPipsDayState.IN_BAND
            for candle in candles[entry_i:]:
                if direction == "LONG":
                    if _close(candle) < record.stop_price:
                        record.state = ThirtyPipsDayState.INVALIDATED
                        break
                    if _high(candle) >= record.target_price:
                        record.state = ThirtyPipsDayState.TARGET_HIT
                        break
                else:
                    if _close(candle) > record.stop_price:
                        record.state = ThirtyPipsDayState.INVALIDATED
                        break
                    if _low(candle) <= record.target_price:
                        record.state = ThirtyPipsDayState.TARGET_HIT
                        break

        if record.state in (ThirtyPipsDayState.TARGET_HIT, ThirtyPipsDayState.INVALIDATED):
            self._resolved_day[(symbol, tf)] = day

        return self._commit(record, tf, symbol)

    # -- events -------------------------------------------------------------

    def _commit(self, record: ThirtyPipsDayRecord, tf: int, symbol: str) -> list[dict]:
        prev = self._state.get((symbol, tf))
        events = self._diff(record, prev)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _diff(self, record: ThirtyPipsDayRecord,
              prev: Optional[ThirtyPipsDayRecord]) -> list[dict]:
        if prev is not None and prev.state == record.state and prev.day == record.day:
            return []
        mapping = {
            ThirtyPipsDayState.WAITING: ThirtyPipsDayEvent.LEG_SET,
            ThirtyPipsDayState.IN_BAND: ThirtyPipsDayEvent.IN_BAND,
            ThirtyPipsDayState.GATE_BLOCKED: ThirtyPipsDayEvent.GATE_BLOCKED,
            ThirtyPipsDayState.INVALIDATED: ThirtyPipsDayEvent.INVALIDATED,
            ThirtyPipsDayState.TARGET_HIT: ThirtyPipsDayEvent.TARGET_HIT,
            ThirtyPipsDayState.DONE_FOR_DAY: ThirtyPipsDayEvent.DONE_FOR_DAY,
        }
        event = mapping.get(record.state)
        return [self._event(event, record)] if event else []

    def _event(self, event: ThirtyPipsDayEvent, record: ThirtyPipsDayRecord,
               **extra) -> dict:
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
        self._resolved_day.clear()
