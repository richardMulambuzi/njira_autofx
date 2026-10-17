"""
Bread-and-butter SELL detector (ICT) — the mirror image of the buy model.

The claim, in ICT's own terms: a swing HIGH prints, then an impulse leg drives
DOWN to a swing LOW.  Price then retraces back UP into the OTE band (62%–79% of
that fall — the *premium* half), which is the last good place to sell against the
resting sell-side liquidity sitting under the swing low.

    ① swing HIGH  ->  ② impulse DOWN to a swing LOW
    ③ sell the retracement into 62%–79% of the fall
    ④ stop above the swing HIGH, first target = the leg's own LOW

Where the buy module calls the 62% edge `entry_price` because a falling market
reaches it first, here a RISING market reaches the 62% edge first too — it is just
the lower of the two prices.  So `entry_price` is still the shallower (62%) edge;
`band_high_price` is always the higher-priced edge and `band_low_price` the lower,
which keeps one invariant across both modules and is the only way the two files
stay readable side by side.

The 62/79 choice is ICT's; other sources teach 50–61.8%.  That conflict is NOT
resolved here — it lives in `ote.py` and `premium_discount.py`.

Standalone by design (project rule §1): stdlib only, no sibling imports, no
`sessions`.  Daily bias arrives INJECTED.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.bread_butter_sell")

CONCEPT_NAME = "bread_butter_sell"
CONCEPT_SOURCES = ("ict", "tradingfinder")

_AGREE = ("BEARISH", "SHORT", "DOWN", "SELL")
_DISAGREE = ("BULLISH", "LONG", "UP", "BUY")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class BreadButterSellConfig:
    swing_lookback: int = 5            # bars either side needed to confirm a swing
    leg_lookback: int = 100            # max bars allowed between the high and the low
    band_low: float = 0.62             # shallower retracement edge
    band_high: float = 0.79            # deeper retracement edge
    require_bias: bool = False         # refuse to arm unless daily bias is bearish
    min_leg_pips: float = 10.0         # ignore legs too small to bother with
    stop_buffer_pips: float = 2.0      # clearance above the swing high
    target_mode: str = "leg_extreme"   # "leg_extreme" | "leg_extension"
    target_extension: float = 1.0      # span multiples when target_mode is extension
    pip_size: float = 0.0001


DEFAULT_BREAD_BUTTER_SELL_CONFIG = BreadButterSellConfig()


# ── Result types ────────────────────────────────────────────────────────────

class BreadButterSellState(Enum):
    NO_LEG = "NO_LEG"                  # no usable high->low leg found yet
    WAITING = "WAITING"                # leg found, price still below the band
    IN_BAND = "IN_BAND"                # price has retraced into the OTE band
    GATE_BLOCKED = "GATE_BLOCKED"      # daily bias refuses this direction
    INVALIDATED = "INVALIDATED"        # closed back above the stop
    TARGET_HIT = "TARGET_HIT"          # delivered to the leg low
    UNKNOWN = "UNKNOWN"


class BreadButterSellEvent(Enum):
    LEG_SET = "BBS_LEG_SET"
    IN_BAND = "BBS_IN_BAND"
    GATE_BLOCKED = "BBS_GATE_BLOCKED"
    INVALIDATED = "BBS_INVALIDATED"
    TARGET_HIT = "BBS_TARGET_HIT"


@dataclass
class BreadButterSellRecord:
    symbol: str = ""
    timeframe: int = 0
    state: BreadButterSellState = BreadButterSellState.UNKNOWN
    direction: str = "SHORT"
    leg_high: float = 0.0
    leg_low: float = 0.0
    leg_high_time: object = None
    leg_low_time: object = None
    leg_pips: float = 0.0
    band_high_price: float = 0.0       # the higher-priced edge (79% of the fall)
    band_low_price: float = 0.0        # the lower-priced edge (62% of the fall)
    entry_price: float = 0.0           # shallower edge — the first one reached
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
            "leg_high": round(self.leg_high, 6),
            "leg_low": round(self.leg_low, 6),
            "leg_high_time": self.leg_high_time,
            "leg_low_time": self.leg_low_time,
            "leg_pips": round(self.leg_pips, 2),
            "band_high_price": round(self.band_high_price, 6),
            "band_low_price": round(self.band_low_price, 6),
            "entry_price": round(self.entry_price, 6),
            "stop_price": round(self.stop_price, 6),
            "target_price": round(self.target_price, 6),
            "risk_pips": round(self.risk_pips, 2),
            "reward_pips": round(self.reward_pips, 2),
            "rr": round(self.rr, 2),
            "in_band": self.in_band,
            "bias": self.bias,
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


def _swing_indices(candles: list[dict], lookback: int, low: bool) -> list[int]:
    """Confirmed swing bar indices: the extreme of its own +/-lookback window."""
    found = []
    if lookback < 1:
        return found
    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback:i + lookback + 1]
        if low:
            if _low(candles[i]) <= min(_low(c) for c in window):
                found.append(i)
        elif _high(candles[i]) >= max(_high(c) for c in window):
            found.append(i)
    return found


def _bear_leg(candles: list[dict], lookback: int,
              leg_lookback: int) -> Optional[tuple]:
    """Most recent confirmed swing HIGH followed by a confirmed swing LOW.

    The low must be a CONFIRMED swing, never the running minimum: in the default
    "leg_extreme" mode the low is the target, and a running minimum is a target
    that keeps running away from the trade.
    """
    highs = _swing_indices(candles, lookback, low=False)
    lows = _swing_indices(candles, lookback, low=True)
    for i in reversed(highs):
        later = [j for j in lows if j > i and j - i <= leg_lookback]
        if later:
            j = later[0]
            return i, j, _high(candles[i]), _low(candles[j])
    return None


def _bias_direction(bias) -> str:
    """Normalise whatever the bias engine hands us into a plain upper-case string."""
    if bias is None:
        return ""
    if isinstance(bias, str):
        return bias.upper()
    if isinstance(bias, dict):
        for key in ("allowed_direction", "bias", "direction", "state", "verdict"):
            value = bias.get(key)
            if value:
                return str(getattr(value, "value", value)).upper()
        return ""
    for key in ("allowed_direction", "bias", "direction", "state", "verdict"):
        value = getattr(bias, key, None)
        if value:
            return str(getattr(value, "value", value)).upper()
    return ""


# ── Engine ──────────────────────────────────────────────────────────────────

class BreadButterSellEngine:
    """Per-symbol retracement-sell state.  Pure: no clock, no I/O."""

    def __init__(self, config: BreadButterSellConfig = DEFAULT_BREAD_BUTTER_SELL_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], BreadButterSellRecord] = {}
        self._events: list[dict] = []

    # -- main entry point ---------------------------------------------------

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "",
                bias=None) -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        cfg = self.config
        record = BreadButterSellRecord(
            symbol=symbol, timeframe=tf, created_at=last_ts,
            bias=_bias_direction(bias),
        )

        if cfg.require_bias and not record.bias:
            return []

        if cfg.require_bias and record.bias:
            if record.bias in _DISAGREE or record.bias not in _AGREE:
                record.state = BreadButterSellState.GATE_BLOCKED
                return self._commit(record, tf, symbol)

        leg = _bear_leg(candles, cfg.swing_lookback, cfg.leg_lookback)
        if leg is None:
            record.state = BreadButterSellState.NO_LEG
            return self._commit(record, tf, symbol)

        high_i, low_i, leg_high, leg_low = leg
        leg_pips = (leg_high - leg_low) / cfg.pip_size
        if leg_pips < cfg.min_leg_pips:
            record.state = BreadButterSellState.NO_LEG
            return self._commit(record, tf, symbol)

        span = leg_high - leg_low
        record.leg_high = leg_high
        record.leg_low = leg_low
        record.leg_high_time = _time_key(candles[high_i])
        record.leg_low_time = _time_key(candles[low_i])
        record.leg_pips = leg_pips
        # retracing UP off the low: the 62% edge is the lower price of the two
        record.band_low_price = leg_low + cfg.band_low * span
        record.band_high_price = leg_low + cfg.band_high * span
        record.entry_price = record.band_low_price
        record.stop_price = leg_high + cfg.stop_buffer_pips * cfg.pip_size
        if cfg.target_mode == "leg_extension":
            record.target_price = leg_low - cfg.target_extension * span
        else:
            record.target_price = leg_low
        record.risk_pips = (record.stop_price - record.entry_price) / cfg.pip_size
        record.reward_pips = (record.entry_price - record.target_price) / cfg.pip_size
        record.rr = (record.reward_pips / record.risk_pips) if record.risk_pips > 0 else 0.0
        record.state = BreadButterSellState.WAITING

        # Pessimistic order: the loss is read before the win on any single bar.
        for candle in candles[low_i + 1:]:
            if _close(candle) > record.stop_price:
                record.state = BreadButterSellState.INVALIDATED
                break
            if _low(candle) <= record.target_price:
                record.state = BreadButterSellState.TARGET_HIT
                break
            if _high(candle) >= record.band_low_price and _low(candle) <= record.band_high_price:
                record.in_band = True
                record.state = BreadButterSellState.IN_BAND

        return self._commit(record, tf, symbol)

    # -- bookkeeping --------------------------------------------------------

    def _commit(self, record: BreadButterSellRecord, tf: int, symbol: str) -> list[dict]:
        prev = self._state.get((symbol, tf))
        events = self._diff(record, prev)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _diff(self, record: BreadButterSellRecord,
              prev: Optional[BreadButterSellRecord]) -> list[dict]:
        if prev is not None and prev.state == record.state and prev.leg_low == record.leg_low:
            return []
        event = {
            BreadButterSellState.WAITING: BreadButterSellEvent.LEG_SET,
            BreadButterSellState.IN_BAND: BreadButterSellEvent.IN_BAND,
            BreadButterSellState.GATE_BLOCKED: BreadButterSellEvent.GATE_BLOCKED,
            BreadButterSellState.INVALIDATED: BreadButterSellEvent.INVALIDATED,
            BreadButterSellState.TARGET_HIT: BreadButterSellEvent.TARGET_HIT,
        }.get(record.state)
        if event is None:
            return []
        return [self._event(event, record)]

    def _event(self, event: BreadButterSellEvent, record: BreadButterSellRecord, **extra) -> dict:
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
