"""
Sweep → MSS → OB/FVG trade model (ICT flagship, the assembly).

This is the whole recipe in one place, and it does no detection of its own:

    ① a stop-stacked level gets SWEPT          (liquidity module's output)
    ② price immediately reverses — an MSS      (market structure module's output)
    ③ entry on the pullback into the zone that caused the move
                                              (order-block / FVG module's output)
    ④ target the next pool of orders           (dol module's output)

Everything it needs arrives INJECTED as plain dicts, because the project rule is
that a composite never imports its own parts — that is what keeps every brick
independently tunable.  The one permitted import is ``forex.analysis.sessions``,
used solely to ask whether the moment sits inside an ICT kill zone.

Injected contracts (each optional; a missing input is reported, never inferred):

    bias              {"tradeable": bool, "allowed_direction": "LONG"|"SHORT"|"",
                       "reference_prices": {...}}          — daily_bias.to_dict()
    sweeps            [{"kind","price","timestamp","direction"}]
                       direction is SELL_SIDE (stops below taken) or BUY_SIDE
    structure_events  [{"event": "MSS_BULLISH"|"MSS_BEARISH", "price", "timestamp"}]
    zones             [{"direction": "BULLISH"|"BEARISH", "high","low","timestamp"}]
                       upper_boundary / lower_boundary are accepted aliases
    dol               {"direction": "UP"|"DOWN", "target_price", "target_kind"}

Deliberate, flagged disagreements rather than hidden choices:

    C3  the zone edge differs per broker feed — ``entry_mode`` picks edge or mid
    C5  the stop anchor differs between sources — ``stop_anchor`` picks the
        sweep EXTREME or the sweep candle's CLOSE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from forex.analysis import sessions

logger = logging.getLogger("forex.analysis.sweep_mss_model")

CONCEPT_NAME = "sweep_mss_model"
CONCEPT_SOURCES = ("ict", "litefinance", "tradingfinder")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class SweepMSSConfig:
    sweep_window_bars: int = 20        # how recent the sweep must be
    mss_window_bars: int = 20          # how long the reversal may take
    zone_lookback_bars: int = 20       # how recent the entry zone must be
    require_bias: bool = True          # refuse to trade without a locked daily bias
    require_killzone: bool = True      # only inside London / New York kill zones
    min_rr: float = 2.0                # reward:risk floor before it is actionable
    entry_mode: str = "zone_edge"      # "zone_edge" | "zone_mid"
    stop_anchor: str = "sweep_extreme"  # "sweep_extreme" | "sweep_close"  (C5)
    stop_buffer_pips: float = 2.0      # slipped past the anchor so it is not tapped
    pip_size: float = 0.0001


DEFAULT_SWEEP_MSS_CONFIG = SweepMSSConfig()


# ── Result types ────────────────────────────────────────────────────────────

class SetupDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class SetupStatus(Enum):
    ARMED = "ARMED"
    GATE_BLOCKED = "GATE_BLOCKED"
    NO_SWEEP = "NO_SWEEP"
    NO_MSS = "NO_MSS"
    NO_ZONE = "NO_ZONE"
    NO_TARGET = "NO_TARGET"
    OUTSIDE_KILLZONE = "OUTSIDE_KILLZONE"
    RR_TOO_LOW = "RR_TOO_LOW"


class SweepMSSEvent(Enum):
    SETUP_ARMED = "SM_SETUP_ARMED"
    SETUP_INVALIDATED = "SM_SETUP_INVALIDATED"
    GATE_BLOCKED = "SM_GATE_BLOCKED"


@dataclass
class SweepMSSSetup:
    symbol: str = ""
    timeframe: int = 0
    direction: SetupDirection = SetupDirection.NONE
    status: SetupStatus = SetupStatus.NO_SWEEP
    reason: str = ""
    sweep_kind: str = ""
    sweep_price: float = 0.0
    sweep_time: object = None
    mss_event: str = ""
    mss_price: float = 0.0
    mss_time: object = None
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    target_kind: str = ""
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    rr: float = 0.0
    in_killzone: bool = False
    bias: str = ""
    actionable: bool = False
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "status": self.status.value,
            "reason": self.reason,
            "sweep_kind": self.sweep_kind,
            "sweep_price": round(self.sweep_price, 6),
            "sweep_time": self.sweep_time,
            "mss_event": self.mss_event,
            "mss_price": round(self.mss_price, 6),
            "mss_time": self.mss_time,
            "entry": round(self.entry, 6),
            "stop": round(self.stop, 6),
            "target": round(self.target, 6),
            "target_kind": self.target_kind,
            "risk_pips": round(self.risk_pips, 2),
            "reward_pips": round(self.reward_pips, 2),
            "rr": round(self.rr, 4),
            "in_killzone": self.in_killzone,
            "bias": self.bias,
            "actionable": self.actionable,
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


def _num(mapping: dict, key: str, default: float = 0.0) -> float:
    value = mapping.get(key)
    return float(value) if isinstance(value, (int, float)) else default


def _zone_bounds(zone: dict) -> tuple[float, float]:
    """A zone is (upper, lower) whichever naming the producer used."""
    upper = _val(zone, "high", "upper_boundary", "upper", default=None)
    lower = _val(zone, "low", "lower_boundary", "lower", default=None)
    if upper is None or lower is None:
        return 0.0, 0.0
    return float(upper), float(lower)


def _cutoff(candles: list[dict], window: int) -> float:
    """Timestamp of the oldest bar inside a ``window``-bar lookback."""
    index = max(0, len(candles) - max(1, window))
    return _time_key(candles[index]) or 0.0


# ── Engine ──────────────────────────────────────────────────────────────────

class SweepMSSEngine:
    """Sequences injected parts into one trade plan.  Pure: no clock, no I/O."""

    def __init__(self, config: SweepMSSConfig = DEFAULT_SWEEP_MSS_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], SweepMSSSetup] = {}
        self._events: list[dict] = []

    # -- main entry point ---------------------------------------------------

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "",
                bias: Optional[dict] = None,
                sweeps: Optional[list] = None,
                structure_events: Optional[list] = None,
                zones: Optional[list] = None,
                dol: Optional[dict] = None) -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        setup = self._build(candles, tf, symbol, last_ts, bias or {},
                            sweeps or [], structure_events or [], zones or [], dol or {})

        prev = self._state.get((symbol, tf))
        events = self._diff(setup, prev)
        self._state[(symbol, tf)] = setup
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    # -- the recipe ---------------------------------------------------------

    def _build(self, candles: list[dict], tf: int, symbol: str, last_ts: float,
               bias: dict, sweeps: list, structure_events: list, zones: list,
               dol: dict) -> SweepMSSSetup:
        cfg = self.config
        setup = SweepMSSSetup(symbol=symbol, timeframe=tf, created_at=last_ts)
        setup.in_killzone = bool(sessions.in_killzone(last_ts))
        setup.bias = str(bias.get("allowed_direction") or "")
        setup.reason = "no sweep in the last %d bars" % cfg.sweep_window_bars

        setup.status = SetupStatus.NO_SWEEP

        # Gate ① — the daily bias is a permission slip, not a suggestion.
        allowed = bias.get("allowed_direction") or ""
        if cfg.require_bias:
            if not bias:
                setup.status, setup.reason = SetupStatus.GATE_BLOCKED, "bias not provided"
                return setup
            if not bias.get("tradeable"):
                setup.status, setup.reason = SetupStatus.GATE_BLOCKED, "daily bias not tradeable"
                return setup

        # ① the sweep
        sweep_cutoff = _cutoff(candles, cfg.sweep_window_bars)
        recent = [s for s in sweeps if _num(s, "timestamp") >= sweep_cutoff]
        if not recent:
            return setup
        sweep = max(recent, key=lambda s: _num(s, "timestamp"))
        side = str(sweep.get("direction") or "").upper()
        direction = (SetupDirection.LONG if side == "SELL_SIDE"
                     else SetupDirection.SHORT if side == "BUY_SIDE" else SetupDirection.NONE)
        if direction is SetupDirection.NONE:
            setup.reason = "sweep has no recognised side"
            return setup
        setup.direction = direction
        setup.sweep_kind = str(sweep.get("kind") or "")
        setup.sweep_price = _num(sweep, "price")
        setup.sweep_time = sweep.get("timestamp")

        if allowed and allowed != direction.value:
            setup.status, setup.reason = (SetupStatus.GATE_BLOCKED,
                                          "bias says %s" % allowed)
            return setup

        # ② the reversal
        expected = ("MSS_BULLISH" if direction is SetupDirection.LONG else "MSS_BEARISH")
        mss_cutoff = _cutoff(candles, cfg.mss_window_bars)
        reversals = [e for e in structure_events
                     if str(e.get("event")) == expected
                     and _num(e, "timestamp") >= max(setup.sweep_time or 0, mss_cutoff)]
        if not reversals:
            setup.status = SetupStatus.NO_MSS
            setup.reason = "no %s within %d bars of the sweep" % (expected, cfg.mss_window_bars)
            return setup
        mss = min(reversals, key=lambda e: _num(e, "timestamp"))
        setup.mss_event = expected
        setup.mss_price = _num(mss, "price")
        setup.mss_time = mss.get("timestamp")

        # ③ the entry zone left behind by the move
        want = "BULLISH" if direction is SetupDirection.LONG else "BEARISH"
        zone_cutoff = _cutoff(candles, cfg.zone_lookback_bars)
        usable = [z for z in zones
                  if str(z.get("direction") or "").upper() == want
                  and _num(z, "timestamp") >= max(setup.sweep_time or 0, zone_cutoff)]
        if not usable:
            setup.status = SetupStatus.NO_ZONE
            setup.reason = "no %s entry zone after the sweep" % want.lower()
            return setup
        upper, lower = _zone_bounds(max(usable, key=lambda z: _num(z, "timestamp")))
        if upper <= 0 or lower <= 0:
            setup.status, setup.reason = SetupStatus.NO_ZONE, "zone has no usable bounds"
            return setup
        if cfg.entry_mode == "zone_mid":
            entry = (upper + lower) / 2.0
        else:
            entry = upper if direction is SetupDirection.LONG else lower
        setup.entry = entry

        # stop — C5, both anchors kept as a config pair
        anchor = setup.sweep_price
        if cfg.stop_anchor == "sweep_close":
            anchor = _num(sweep, "close", setup.sweep_price)
        buffer = cfg.stop_buffer_pips * cfg.pip_size
        setup.stop = (anchor - buffer if direction is SetupDirection.LONG
                      else anchor + buffer)

        # ④ the target
        target = _num(dol, "target_price")
        target_kind = str(dol.get("target_kind") or "")
        if target <= 0:
            refs = bias.get("reference_prices") or {}
            key = "pdh" if direction is SetupDirection.LONG else "pdl"
            target = _num(refs, key)
            target_kind = key.upper() if target > 0 else ""
        if target <= 0:
            setup.status, setup.reason = SetupStatus.NO_TARGET, "no draw-on-liquidity target"
            return setup
        if (direction is SetupDirection.LONG and target <= entry) or \
           (direction is SetupDirection.SHORT and target >= entry):
            setup.status = SetupStatus.NO_TARGET
            setup.reason = "target is on the wrong side of the entry"
            return setup
        setup.target = target
        setup.target_kind = target_kind

        risk = abs(entry - setup.stop)
        reward = abs(target - entry)
        setup.risk_pips = risk / cfg.pip_size
        setup.reward_pips = reward / cfg.pip_size
        setup.rr = (reward / risk) if risk > 0 else 0.0

        if cfg.require_killzone and not setup.in_killzone:
            setup.status = SetupStatus.OUTSIDE_KILLZONE
            setup.reason = "setup is outside every ICT kill zone"
            return setup
        if setup.rr < cfg.min_rr:
            setup.status = SetupStatus.RR_TOO_LOW
            setup.reason = "rr %.2f below the %.2f floor" % (setup.rr, cfg.min_rr)
            return setup

        setup.status = SetupStatus.ARMED
        setup.reason = "sweep -> %s -> %s entry" % (expected, want.lower())
        setup.actionable = True
        return setup

    # -- events -------------------------------------------------------------

    def _diff(self, setup: SweepMSSSetup,
              prev: Optional[SweepMSSSetup]) -> list[dict]:
        armed = setup.status is SetupStatus.ARMED
        was_armed = prev is not None and prev.status is SetupStatus.ARMED

        if armed:
            return [self._event(SweepMSSEvent.SETUP_ARMED, setup)]
        if was_armed:
            return [self._event(SweepMSSEvent.SETUP_INVALIDATED, setup,
                                previous_status=prev.status.value)]
        if setup.status is SetupStatus.GATE_BLOCKED:
            return [self._event(SweepMSSEvent.GATE_BLOCKED, setup)]
        return []

    def _event(self, event: SweepMSSEvent, setup: SweepMSSSetup, **extra) -> dict:
        payload = {"event": event.value, "timestamp": setup.created_at}
        payload.update(setup.to_dict())
        payload.update(extra)
        return payload

    # -- read access --------------------------------------------------------

    def get_active(self, limit: int = 10) -> list[dict]:
        return [s.to_dict() for s in list(self._state.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list[dict]:
        return [s.to_dict() for s in list(self._state.values())[-limit:]]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    def reset(self) -> None:
        self._state.clear()
        self._events.clear()
