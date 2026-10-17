"""Tests for the standalone ICT Bread-and-Butter BUY detector."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.bread_butter_buy import (
    DEFAULT_BREAD_BUTTER_BUY_CONFIG,
    BreadButterBuyConfig,
    BreadButterBuyEngine,
    BreadButterBuyEvent,
    BreadButterBuyState,
)

QUARTER = 900
BASE = int(datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc).timestamp())


def bar(i, o, h, l, c):
    return {"open_time": BASE + i * QUARTER, "open": o, "high": h, "low": l, "close": c}


def leg_candles():
    """Swing low 1.08000 at index 3, then a confirmed swing high 1.09000 at index 7."""
    return [
        bar(0, 1.08100, 1.08150, 1.08050, 1.08100),
        bar(1, 1.08100, 1.08130, 1.08060, 1.08080),
        bar(2, 1.08080, 1.08120, 1.08040, 1.08060),
        bar(3, 1.08060, 1.08100, 1.08000, 1.08040),   # swing low
        bar(4, 1.08040, 1.08200, 1.08020, 1.08180),
        bar(5, 1.08180, 1.08400, 1.08150, 1.08380),
        bar(6, 1.08380, 1.08700, 1.08360, 1.08680),
        bar(7, 1.08680, 1.09000, 1.08650, 1.08950),   # swing high (the leg)
        bar(8, 1.08950, 1.08980, 1.08800, 1.08850),   # confirms the high
        bar(9, 1.08850, 1.08900, 1.08750, 1.08800),   # confirms the high
    ]


RETRACE = bar(10, 1.08950, 1.08980, 1.08350, 1.08400)   # dips into the 62% edge
INVALIDATE = bar(10, 1.08950, 1.08980, 1.07800, 1.07900)  # closes under the stop
TARGET = bar(11, 1.08950, 1.09050, 1.08900, 1.09020)      # takes the leg high


# ── the leg and its geometry ────────────────────────────────────────────────

def test_leg_and_ote_geometry():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["state"] == BreadButterBuyState.WAITING.value
    assert rec["direction"] == "LONG"
    assert rec["leg_low"] == approx(1.08000)
    assert rec["leg_high"] == approx(1.09000)
    assert rec["leg_pips"] == approx(100.0)
    assert rec["band_high_price"] == approx(1.08380)   # 62% retracement
    assert rec["band_low_price"] == approx(1.08210)    # 79% retracement
    assert rec["entry_price"] == approx(1.08380)
    assert rec["stop_price"] == approx(1.07980)
    assert rec["target_price"] == approx(1.09000)
    assert rec["risk_pips"] == approx(40.0)
    assert rec["reward_pips"] == approx(62.0)
    assert rec["rr"] == approx(1.55)


def test_the_band_is_configurable():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2,
                                                       band_low=0.50, band_high=0.618))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["band_high_price"] == approx(1.08500)
    assert rec["band_low_price"] == approx(1.08382)


def test_a_short_leg_is_not_a_leg():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2, min_leg_pips=500.0))
    events = engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert events == []
    assert engine.get_active()[0]["state"] == BreadButterBuyState.NO_LEG.value


def test_without_a_confirmed_swing_low_there_is_no_leg():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles()[:4], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == BreadButterBuyState.NO_LEG.value


# ── the retracement ─────────────────────────────────────────────────────────

def test_retracing_into_the_band_arms_the_setup():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    events = engine.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [BreadButterBuyEvent.IN_BAND.value]
    rec = engine.get_active()[0]
    assert rec["state"] == BreadButterBuyState.IN_BAND.value
    assert rec["in_band"] is True


def test_a_bar_that_never_reaches_the_band_leaves_it_waiting():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    shallow = bar(10, 1.08950, 1.08980, 1.08600, 1.08700)   # stops above the 62% edge
    engine.analyze(leg_candles() + [shallow], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == BreadButterBuyState.WAITING.value


def test_closing_under_the_stop_invalidates_the_leg():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    events = engine.analyze(leg_candles() + [INVALIDATE], tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [BreadButterBuyEvent.INVALIDATED.value]


def test_taking_the_leg_high_is_the_target():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    events = engine.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [BreadButterBuyEvent.TARGET_HIT.value]


def test_the_invalidation_wins_when_both_print_on_one_bar():
    """Stop is checked before target — the pessimistic reading is the safe one."""
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    both = bar(10, 1.08950, 1.09050, 1.07800, 1.07900)
    engine.analyze(leg_candles() + [both], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == BreadButterBuyState.INVALIDATED.value


def test_the_target_can_be_extended_past_the_leg():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2,
                                                       target_mode="leg_extension",
                                                       target_extension=1.0))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["target_price"] == approx(1.10000)


# ── the injected bias gate ──────────────────────────────────────────────────

def test_a_bearish_bias_blocks_the_setup():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2, require_bias=True))
    events = engine.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BEARISH")
    assert [e["event"] for e in events] == [BreadButterBuyEvent.GATE_BLOCKED.value]
    assert engine.get_active()[0]["state"] == BreadButterBuyState.GATE_BLOCKED.value


def test_a_bullish_bias_lets_the_setup_through():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2, require_bias=True))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BULLISH")
    assert engine.get_active()[0]["state"] == BreadButterBuyState.WAITING.value


def test_a_bias_dict_is_read_too():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2, require_bias=True))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD",
                   bias={"allowed_direction": "LONG"})
    assert engine.get_active()[0]["state"] == BreadButterBuyState.WAITING.value
    assert engine.get_active()[0]["bias"] == "LONG"


def test_requiring_a_bias_and_getting_none_is_silence():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2, require_bias=True))
    assert engine.analyze(leg_candles(), tf=15, symbol="EURUSD") == []
    assert engine.get_active() == []


def test_an_unrequired_bias_is_ignored():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BEARISH")
    assert engine.get_active()[0]["state"] == BreadButterBuyState.WAITING.value


# ── lifecycle & hygiene ─────────────────────────────────────────────────────

def test_the_setup_walks_leg_then_band_then_target():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    first = engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    second = engine.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    third = engine.analyze(leg_candles() + [RETRACE, TARGET], tf=15, symbol="EURUSD")
    assert [e["event"] for e in first] == [BreadButterBuyEvent.LEG_SET.value]
    assert [e["event"] for e in second] == [BreadButterBuyEvent.IN_BAND.value]
    assert [e["event"] for e in third] == [BreadButterBuyEvent.TARGET_HIT.value]


def test_an_unchanged_state_says_nothing_twice():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert engine.analyze(leg_candles(), tf=15, symbol="EURUSD") == []


def test_insufficient_candles_returns_empty():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    assert engine.analyze([], tf=15, symbol="EURUSD") == []
    assert engine.analyze(leg_candles()[:1], tf=15, symbol="EURUSD") == []
    assert engine.get_active() == []


def test_same_input_gives_same_output():
    a = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    b = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    a.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    b.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    engine.analyze(leg_candles() + [TARGET], tf=15, symbol="XAUUSD")
    assert len(engine.get_all()) == 2


def test_reset_clears_everything():
    engine = BreadButterBuyEngine(BreadButterBuyConfig(swing_lookback=2))
    engine.analyze(leg_candles(), tf=15, symbol="EURUSD")
    engine.reset()
    assert engine.get_active() == []
    assert engine.get_events() == []


def test_default_config_is_the_module_default():
    assert BreadButterBuyEngine().config is DEFAULT_BREAD_BUTTER_BUY_CONFIG
