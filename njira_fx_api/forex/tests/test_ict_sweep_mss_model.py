"""Tests for the ICT flagship assembly: sweep -> MSS -> zone -> target."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.sweep_mss_model import (
    DEFAULT_SWEEP_MSS_CONFIG,
    SetupDirection,
    SetupStatus,
    SweepMSSConfig,
    SweepMSSEngine,
    SweepMSSEvent,
)

HOUR = 3600
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())   # a Monday
BASE = MONDAY + 8 * HOUR          # 08:00 UTC — inside the London kill zone
BASE_OFF = MONDAY + 3 * HOUR      # 03:00 UTC — inside no kill zone at all


def candle(ts, o, h, l, c):
    return {"open_time": ts, "open": o, "high": h, "low": l, "close": c}


def _engine(**overrides):
    return SweepMSSEngine(SweepMSSConfig(**overrides))


def parts(base=BASE, **overrides):
    """A complete, valid flagship setup — sweep at 1.08280, MSS to 1.08460."""
    payload = {
        "candles": [candle(base, 1.08290, 1.08470, 1.08270, 1.08460),
                    candle(base + 900, 1.08460, 1.08470, 1.08430, 1.08450)],
        "bias": {
            "tradeable": True,
            "allowed_direction": "LONG",
            "reference_prices": {"pdh": 1.08620, "pdl": 1.08150},
        },
        "sweeps": [{"kind": "PDL", "price": 1.08280, "close": 1.08310,
                    "timestamp": base + 300, "direction": "SELL_SIDE"}],
        "structure_events": [{"event": "MSS_BULLISH", "price": 1.08460,
                              "timestamp": base + 600}],
        "zones": [{"direction": "BULLISH", "high": 1.08365, "low": 1.08340,
                   "timestamp": base + 600}],
        "dol": {"direction": "UP", "target_price": 1.08620, "target_kind": "PDH"},
    }
    payload.update(overrides)
    return payload


# ── the whole recipe ────────────────────────────────────────────────────────

def test_full_recipe_arms_a_long():
    engine = _engine()
    events = engine.analyze(**parts(), tf=15, symbol="EURUSD")
    assert len(events) == 1
    assert events[0]["event"] == SweepMSSEvent.SETUP_ARMED.value

    rec = engine.get_active()[0]
    assert rec["status"] == SetupStatus.ARMED.value
    assert rec["direction"] == SetupDirection.LONG.value
    assert rec["entry"] == approx(1.08365)      # zone edge, not the mid
    assert rec["stop"] == approx(1.08260)       # sweep extreme minus 2 pips
    assert rec["target"] == approx(1.08620)
    assert rec["risk_pips"] == approx(10.5)
    assert rec["reward_pips"] == approx(25.5)
    assert rec["rr"] == approx(2.4286, abs=1e-3)
    assert rec["in_killzone"] is True
    assert rec["actionable"] is True


def test_full_recipe_arms_a_short():
    payload = parts()
    payload["bias"] = {"tradeable": True, "allowed_direction": "SHORT",
                       "reference_prices": {"pdh": 1.08620, "pdl": 1.08150}}
    payload["sweeps"] = [{"kind": "PDH", "price": 1.08620, "close": 1.08590,
                          "timestamp": BASE + 300, "direction": "BUY_SIDE"}]
    payload["structure_events"] = [{"event": "MSS_BEARISH", "price": 1.08400,
                                    "timestamp": BASE + 600}]
    payload["zones"] = [{"direction": "BEARISH", "high": 1.08560, "low": 1.08530,
                         "timestamp": BASE + 600}]
    payload["dol"] = {"direction": "DOWN", "target_price": 1.08150, "target_kind": "PDL"}

    engine = _engine()
    engine.analyze(**payload, tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["status"] == SetupStatus.ARMED.value
    assert rec["direction"] == SetupDirection.SHORT.value
    assert rec["entry"] == approx(1.08530)      # bearish zone: entry at the LOWER edge
    assert rec["stop"] == approx(1.08640)       # sweep extreme plus the buffer
    assert rec["rr"] > 2.0


# ── the bias gate ───────────────────────────────────────────────────────────

def test_missing_bias_blocks_the_setup():
    engine = _engine()
    events = engine.analyze(**parts(bias={}), tf=15, symbol="EURUSD")
    assert events[0]["event"] == SweepMSSEvent.GATE_BLOCKED.value
    assert engine.get_active()[0]["status"] == SetupStatus.GATE_BLOCKED.value
    assert engine.get_active()[0]["reason"] == "bias not provided"


def test_untradeable_bias_blocks_the_setup():
    engine = _engine()
    engine.analyze(**parts(bias={"tradeable": False, "allowed_direction": "LONG"}),
                   tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["status"] == SetupStatus.GATE_BLOCKED.value
    assert rec["reason"] == "daily bias not tradeable"


def test_bias_disagreeing_with_the_sweep_side_blocks_the_setup():
    engine = _engine()
    engine.analyze(**parts(bias={"tradeable": True, "allowed_direction": "SHORT"}),
                   tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["reason"] == "bias says SHORT"


def test_bias_can_be_waived():
    engine = _engine(require_bias=False)
    engine.analyze(**parts(bias={}), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["status"] == SetupStatus.ARMED.value


# ── each step failing on its own ────────────────────────────────────────────

def test_no_sweep_in_the_window():
    engine = _engine()
    engine.analyze(**parts(sweeps=[]), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["status"] == SetupStatus.NO_SWEEP.value


def test_sweep_without_a_reversal():
    engine = _engine()
    engine.analyze(**parts(structure_events=[]), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["status"] == SetupStatus.NO_MSS.value
    assert rec["sweep_price"] == approx(1.08280)   # the sweep is still reported


def test_reversal_without_an_entry_zone():
    engine = _engine()
    engine.analyze(**parts(zones=[]), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["status"] == SetupStatus.NO_ZONE.value


def test_setup_without_a_target():
    engine = _engine()
    engine.analyze(**parts(dol={},
                           bias={"tradeable": True, "allowed_direction": "LONG"}),
                   tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["status"] == SetupStatus.NO_TARGET.value


def test_target_on_the_wrong_side_is_rejected():
    engine = _engine()
    engine.analyze(**parts(dol={"target_price": 1.08300, "target_kind": "X"}),
                   tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["reason"] == "target is on the wrong side of the entry"


def test_target_falls_back_to_the_bias_reference():
    engine = _engine()
    engine.analyze(**parts(dol={}), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["target"] == approx(1.08620)
    assert rec["target_kind"] == "PDH"


def test_setup_outside_every_kill_zone_is_held_back():
    engine = _engine()
    engine.analyze(**parts(base=BASE_OFF), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["in_killzone"] is False
    assert rec["status"] == SetupStatus.OUTSIDE_KILLZONE.value
    assert rec["actionable"] is False


def test_kill_zone_requirement_can_be_waived():
    engine = _engine(require_killzone=False)
    engine.analyze(**parts(base=BASE_OFF), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["status"] == SetupStatus.ARMED.value


def test_reward_risk_floor_holds_the_trigger():
    engine = _engine(min_rr=5.0)
    engine.analyze(**parts(), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["status"] == SetupStatus.RR_TOO_LOW.value
    assert rec["actionable"] is False


# ── conflict pairs stay switchable ──────────────────────────────────────────

def test_stop_can_anchor_on_the_sweep_close():
    engine = _engine(stop_anchor="sweep_close")
    engine.analyze(**parts(), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["stop"] == approx(1.08290)   # 1.08310 - 2 pips


def test_entry_can_sit_at_the_zone_midpoint():
    engine = _engine(entry_mode="zone_mid")
    engine.analyze(**parts(), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["entry"] == approx(1.083525)


def test_upper_boundary_aliases_are_accepted_for_zones():
    engine = _engine()
    engine.analyze(**parts(zones=[{"direction": "BULLISH",
                                   "upper_boundary": 1.08365,
                                   "lower_boundary": 1.08340,
                                   "timestamp": BASE + 600}]),
                   tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["entry"] == approx(1.08365)


# ── events ──────────────────────────────────────────────────────────────────

def test_a_lost_setup_is_reported_as_invalidated():
    engine = _engine()
    engine.analyze(**parts(), tf=15, symbol="EURUSD")
    events = engine.analyze(**parts(structure_events=[]), tf=15, symbol="EURUSD")
    assert len(events) == 1
    assert events[0]["event"] == SweepMSSEvent.SETUP_INVALIDATED.value
    assert events[0]["previous_status"] == SetupStatus.ARMED.value


def test_a_setup_that_is_still_broken_emits_nothing():
    engine = _engine()
    engine.analyze(**parts(sweeps=[]), tf=15, symbol="EURUSD")
    assert engine.analyze(**parts(sweeps=[]), tf=15, symbol="EURUSD") == []


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert _engine().analyze([candle(BASE, 1.0, 1.1, 0.9, 1.05)],
                             tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = _engine(), _engine()
    a.analyze(**parts(), tf=15, symbol="EURUSD")
    b.analyze(**parts(), tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    engine = _engine()
    engine.analyze(**parts(), tf=15, symbol="EURUSD")
    engine.analyze(**parts(sweeps=[]), tf=15, symbol="XAUUSD")
    assert len(engine.get_all()) == 2


def test_reset_clears_everything():
    engine = _engine()
    engine.analyze(**parts(), tf=15, symbol="EURUSD")
    engine.reset()
    assert engine.get_active() == []
    assert engine.get_events() == []


def test_default_config_is_the_module_default():
    assert SweepMSSEngine().config is DEFAULT_SWEEP_MSS_CONFIG
