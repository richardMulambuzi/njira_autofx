"""
Bread-and-Butter BUY setup (ICT) — buy the discount of a bullish leg.

The model, as ICT describes it, is the plainest repeatable trade in the toolkit:

    1. a swing LOW prints, then an impulse leg drives UP to a swing HIGH
    2. the move is not over — price retraces back into the OTE band
       (62%–79% of that leg, i.e. the *discount* half of it)
    3. buy the retracement; protective stop just under the swing LOW
    4. first objective is the leg's own HIGH, which is where the resting
       buy-side liquidity sits

The entry band is measured FROM the swing low toward the swing high.  A 79%
retracement is the deeper, cheaper edge of the band; 62% is the shallower one.
This module reports the shallower edge as ``entry_price`` because that is the
first price the retracement reaches.

Two-source honesty: the 62–79% band is ICT's own teaching, and the 50–61.8%
band taught elsewhere is a different number — that conflict lives in
``ote.py``/``premium_discount.py`` and is NOT resolved here.  This module ships
one band and it is fully configurable (``band_low`` / ``band_high``).

Standalone by design (project rule §1): imports stdlib only — no siblings, no
``sessions``.  The directional bias, if the caller wants it enforced, arrives
INJECTED via ``analyze(..., bias=...)``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.bread_butter_buy")

CONCEPT_NAME = "bread_butter_buy"
CONCEPT_SOURCES = ("ict", "tradingfinder")

_AGREE = ("BULLISH", "LONG", "UP", "BUY")
_DISAGREE = ("BEARISH", "SHORT", "DOWN", "SELL")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class BreadButterBuyConfig:
    swing_lookback: int = 5          # bars either side that must not exceed the swing
    leg_lookback: int = 100          # how far forward from the swing low to look for the high
    band_low: float = 0.62           # shallower edge of the OTE band (fraction of the leg)
    band_high: float = 0.79          # deeper edge of the OTE band
    require_bias: bool = False       # gate on an injected bullish bias
    min_leg_pips: float = 10.0       # a leg shorter than this is noise
    stop_buffer_pips: float = 2.0    # stop sits this far under the swing low
    target_mode: str = "leg_extreme"  # "leg_extreme" | "leg_extension"
    target_extension: float = 1.0    # leg heights beyond the high, for "leg_extension"
    pip_size: float = 0.0001


DEFAULT_BREAD_BUTTER_BUY_CONFIG = BreadButterBuyConfig()


# ── Result types ────────────────────────────────────────────────────────────

class BreadButterBuyState(Enum):
    NO_LEG = "NO_LEG"
    WAITING = "WAITING"          # leg found, price still above the band
    IN_BAND = "IN_BAND"          # price is inside the OTE band — the setup is live
    GATE_BLOCKED = "GATE_BLOCKED"
    INVALIDATED = "INVALIDATED"  # closed under the stop: the leg failed
    TARGET_HIT = "TARGET_HIT"
    UNKNOWN = "UNKNOWN"


class BreadButterBuyEvent(Enum):
    LEG_SET = "BB_LEG_SET"
    IN_BAND = "BB_IN_BAND"
    GATE_BLOCKED = "BB_GATE_BLOCKED"
    INVALIDATED = "BB_INVALIDATED"
    TARGET_HIT = "BB_TARGET_HIT"


@dataclass
class BreadButterBuyRecord:
    symbol: str = ""
    timeframe: int = 0
    state: BreadButterBuyState = BreadButterBuyState.UNKNOWN
    direction: str = "LONG"
    leg_low: float = 0.0
    leg_high: float = 0.0
    leg_low_time: object = None
    leg_high_time: object = None
    leg_pips: float = 0.0
    band_low_price: float = 0.0   # deeper edge (79% retracement)
    band_high_price: float = 0.0  # shallower edge (62% retracement)
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    rr: float = 0.0
    in_band: bool = False
    bias: str = ""
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state.value,
            "direction": self.direction,
            "leg_low": round(self.leg_low, 6),
            "leg_high": round(self.leg_high, 6),
            "leg_low_time": self.leg_low_time,
            "leg_high_time": self.leg_high_time,
            "leg_pips": round(self.leg_pips, 2),
            "band_low_price": round(self.band_low_price, 6),
            "band_high_price": round(self.band_high_price, 6),
            "entry_price": round(self.entry_price, 6),
            "stop_price": round(self.stop_price, 6),
            "target_price": round(self.target_price, 6),
            "risk_pips": round(self.risk_pips, 2),
            "reward_pips": round(self.reward_pips, 2),
            "rr": round(self.rr, 2),
            "in_band": self.in_band,
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
    """Confirmed swings: bars needing ``lookback`` bars either side, none beyond them."""
    found = []
    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback:i + lookback + 1]
        if low:
            if _low(candles[i]) <= min(_low(c) for c in window):
                found.append(i)
        elif _high(candles[i]) >= max(_high(c) for c in window):
            found.append(i)
    return found


def _bull_leg(candles: list[dict], lookback: int,
              leg_lookback: int) -> Optional[tuple]:
    """(low_index, high_index, leg_low, leg_high) for the most recent bullish leg.

    The high is the first CONFIRMED swing high after the low, never the running
    maximum — a running maximum would move the target up with every new high and
    the target could never be reached.
    """
    highs = _swing_indices(candles, lookback, low=False)
    for i in reversed(_swing_indices(candles, lookback, low=True)):
        leg_low = _low(candles[i])
        for j in highs:
            if j <= i:
                continue
            if j - i > leg_lookback:
                break
            if _high(candles[j]) > leg_low:
                return i, j, leg_low, _high(candles[j])
    return None


def _bias_direction(bias) -> Optional[str]:
    if bias is None:
        return None
    if isinstance(bias, str):
        return bias.upper() or None
    if isinstance(bias, dict):
        for key in ("allowed_direction", "bias", "direction", "state", "verdict"):
            value = bias.get(key)
            if value:
                return str(value).upper()
        return None
    return str(getattr(bias, "allowed_direction", "") or "").upper() or None


# ── Engine ──────────────────────────────────────────────────────────────────

class BreadButterBuyEngine:
    """Per-symbol bullish leg + OTE retracement state.  Pure: no clock, no I/O."""

    def __init__(self, config: BreadButterBuyConfig = DEFAULT_BREAD_BUTTER_BUY_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], BreadButterBuyRecord] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "",
                bias=None) -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        direction = _bias_direction(bias)
        record = BreadButterBuyRecord(
            symbol=symbol, timeframe=tf, bias=direction or "", created_at=last_ts)

        if self.config.require_bias:
            if direction is None:
                return []
            if direction in _DISAGREE or direction not in _AGREE:
                record.state = BreadButterBuyState.GATE_BLOCKED
                return self._commit(record, tf, symbol)

        leg = _bull_leg(candles, self.config.swing_lookback, self.config.leg_lookback)
        if leg is None:
            record.state = BreadButterBuyState.NO_LEG
            return self._commit(record, tf, symbol)

        low_i, high_i, leg_low, leg_high = leg
        record.leg_low = leg_low
        record.leg_high = leg_high
        record.leg_low_time = _time_key(candles[low_i])
        record.leg_high_time = _time_key(candles[high_i])
        record.leg_pips = (leg_high - leg_low) / self.config.pip_size

        if record.leg_pips < self.config.min_leg_pips:
            record.state = BreadButterBuyState.NO_LEG
            return self._commit(record, tf, symbol)

        cfg = self.config
        span = leg_high - leg_low
        record.band_high_price = leg_high - cfg.band_low * span   # 62% retracement
        record.band_low_price = leg_high - cfg.band_high * span   # 79% retracement
        record.entry_price = record.band_high_price
        record.stop_price = leg_low - cfg.stop_buffer_pips * cfg.pip_size
        if cfg.target_mode == "leg_extension":
            record.target_price = leg_high + cfg.target_extension * span
        else:
            record.target_price = leg_high
        record.risk_pips = (record.entry_price - record.stop_price) / cfg.pip_size
        record.reward_pips = (record.target_price - record.entry_price) / cfg.pip_size
        record.rr = (record.reward_pips / record.risk_pips) if record.risk_pips > 0 else 0.0

        # Walk forward from the high: whichever of stop / target / band arrives first.
        state = BreadButterBuyState.WAITING
        for candle in candles[high_i + 1:]:
            if _close(candle) < record.stop_price:
                state = BreadButterBuyState.INVALIDATED
                break
            if _high(candle) >= record.target_price:
                state = BreadButterBuyState.TARGET_HIT
                break
            if _high(candle) >= record.band_low_price and _low(candle) <= record.band_high_price:
                record.in_band = True
                state = BreadButterBuyState.IN_BAND
        record.state = state
        return self._commit(record, tf, symbol)

    # -- plumbing -----------------------------------------------------------

    def _commit(self, record: BreadButterBuyRecord, tf: int, symbol: str) -> list[dict]:
        prev = self._state.get((symbol, tf))
        events = self._diff(record, prev)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _diff(self, record: BreadButterBuyRecord,
              prev: Optional[BreadButterBuyRecord]) -> list[dict]:
        if prev is not None and prev.state == record.state and prev.leg_low == record.leg_low:
            return []
        mapping = {
            BreadButterBuyState.NO_LEG: None,
            BreadButterBuyState.WAITING: BreadButterBuyEvent.LEG_SET,
            BreadButterBuyState.IN_BAND: BreadButterBuyEvent.IN_BAND,
            BreadButterBuyState.GATE_BLOCKED: BreadButterBuyEvent.GATE_BLOCKED,
            BreadButterBuyState.INVALIDATED: BreadButterBuyEvent.INVALIDATED,
            BreadButterBuyState.TARGET_HIT: BreadButterBuyEvent.TARGET_HIT,
        }
        event = mapping.get(record.state)
        if event is None:
            return []
        return [self._event(event, record)]

    def _event(self, event: BreadButterBuyEvent, record: BreadButterBuyRecord,
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
