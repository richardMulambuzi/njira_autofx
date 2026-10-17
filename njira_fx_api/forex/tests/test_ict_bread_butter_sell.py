"""Tests for the standalone ICT bread-and-butter SELL model (retracement short)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.bread_butter_sell import (
    DEFAULT_BREAD_BUTTER_SELL_CONFIG,
    BreadButterSellConfig,
    BreadButterSellEngine,
    BreadButterSellEvent,
    BreadButterSellState,
)

QUARTER = 900
BASE = int(datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc).timestamp())


def bar(i, o, h, l, c):
    return {"open_time": BASE + i * QUARTER, "open": o, "high": h, "low": l, "close": c}


def leg_candles():
    """Swing HIGH 1.09000 at index 3, impulse DOWN to swing LOW 1.08000 at index 7,
    then two quieter bars so both swings are confirmed by swing_lookback=2."""
    return [
        bar(0, 1.08500, 1.08600, 1.08450, 1.08550),
        bar(1, 1.08550, 1.08700, 1.08500, 1.08680),
        bar(2, 1.08680, 1.08850, 1.08650, 1.08800),
        bar(3, 1.08800, 1.09000, 1.08750, 1.08900),   # swing high
        bar(4, 1.08900, 1.08920, 1.08600, 1.08650),
        bar(5, 1.08650, 1.08680, 1.08350, 1.08400),
        bar(6, 1.08400, 1.08450, 1.08200, 1.08250),
        bar(7, 1.08250, 1.08300, 1.08000, 1.08050),   # swing low
        bar(8, 1.08050, 1.08150, 1.08020, 1.08100),
        bar(9, 1.08100, 1.08180, 1.08100, 1.08150),
    ]


RETRACE = bar(10, 1.08050, 1.08700, 1.08030, 1.08650)      # into the 62% edge
SHALLOW = bar(10, 1.08050, 1.08500, 1.08030, 1.08480)      # stops short of the band
INVALIDATE = bar(10, 1.08050, 1.09100, 1.08030, 1.09060)   # closes above the stop
TARGET = bar(11, 1.08650, 1.08660, 1.07950, 1.07980)       # takes the leg low
BOTH = bar(10, 1.08050, 1.09100, 1.07950, 1.09060)         # prints the stop AND the target


def engine(**overrides):
    # 10-candle fixtures: lookback must stay small enough for swing_lookback *2 bars to fit
    return BreadButterSellEngine(BreadButterSellConfig(swing_lookback=2, **overrides))


# ── leg discovery and geometry ──────────────────────────────────────────────

def test_finds_the_bear_leg_and_prices_the_ote_band():
    rec = engine().get_active()
    events = engine().analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert events[0]["event"] == BreadButterSellEvent.LEG_SET.value

    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    r = e.get_active()[0]
    assert r["state"] == BreadButterSellState.WAITING.value
    assert r["direction"] == "SHORT"
    assert r["leg_high"] == approx(1.09000)
    assert r["leg_low"] == approx(1.08000)
    assert r["leg_pips"] == approx(100.0)
    assert r["band_low_price"] == approx(1.08620)      # 62% edge, the lower price
    assert r["band_high_price"] == approx(1.08790)     # 79% edge, the higher price
    assert r["entry_price"] == approx(1.08620)
    assert r["stop_price"] == approx(1.09020)
    assert r["target_price"] == approx(1.08000)
    assert r["risk_pips"] == approx(40.0)
    assert r["reward_pips"] == approx(62.0)
    assert r["rr"] == approx(1.55)
    assert rec == []


def test_the_other_source_band_is_configurable():
    e = engine(band_low=0.50, band_high=0.618)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    r = e.get_active()[0]
    assert r["band_low_price"] == approx(1.08500)
    assert r["band_high_price"] == approx(1.08618)


def test_a_leg_smaller_than_min_leg_pips_is_ignored():
    e = engine(min_leg_pips=500.0)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == BreadButterSellState.NO_LEG.value


def test_too_few_candles_to_confirm_a_swing_is_no_leg():
    e = engine()
    e.analyze(leg_candles()[:4], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == BreadButterSellState.NO_LEG.value


def test_the_extension_target_sits_below_the_leg_low():
    e = engine(target_mode="leg_extension", target_extension=1.0)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["target_price"] == approx(1.07000)


# ── state machine ───────────────────────────────────────────────────────────

def test_a_retrace_into_the_band_reads_as_in_band():
    e = engine()
    events = e.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    r = e.get_active()[0]
    assert r["state"] == BreadButterSellState.IN_BAND.value
    assert r["in_band"] is True
    assert events[0]["event"] == BreadButterSellEvent.IN_BAND.value


def test_a_retrace_short_of_the_band_stays_waiting():
    e = engine()
    e.analyze(leg_candles() + [SHALLOW], tf=15, symbol="EURUSD")
    r = e.get_active()[0]
    assert r["state"] == BreadButterSellState.WAITING.value
    assert r["in_band"] is False


def test_a_close_above_the_stop_invalidates_the_setup():
    e = engine()
    events = e.analyze(leg_candles() + [INVALIDATE], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == BreadButterSellState.INVALIDATED.value
    assert events[0]["event"] == BreadButterSellEvent.INVALIDATED.value


def test_taking_the_leg_low_is_the_target():
    e = engine()
    events = e.analyze(leg_candles() + [RETRACE, TARGET], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == BreadButterSellState.TARGET_HIT.value
    assert events[0]["event"] == BreadButterSellEvent.TARGET_HIT.value


def test_the_loss_wins_when_one_bar_prints_both_levels():
    e = engine()
    e.analyze(leg_candles() + [BOTH], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == BreadButterSellState.INVALIDATED.value


def test_the_setup_walks_leg_then_band_then_target():
    e = engine()
    first = e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    second = e.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    third = e.analyze(leg_candles() + [RETRACE, TARGET], tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in first] == [BreadButterSellEvent.LEG_SET.value]
    assert [ev["event"] for ev in second] == [BreadButterSellEvent.IN_BAND.value]
    assert [ev["event"] for ev in third] == [BreadButterSellEvent.TARGET_HIT.value]


def test_an_unchanged_state_says_nothing_twice():
    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert e.analyze(leg_candles(), tf=15, symbol="EURUSD") == []


# ── bias gate ───────────────────────────────────────────────────────────────

def test_a_bullish_bias_blocks_the_short_when_the_gate_is_on():
    e = engine(require_bias=True)
    events = e.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BULLISH")
    assert e.get_active()[0]["state"] == BreadButterSellState.GATE_BLOCKED.value
    assert events[0]["event"] == BreadButterSellEvent.GATE_BLOCKED.value


def test_a_bearish_bias_lets_the_short_through():
    e = engine(require_bias=True)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BEARISH")
    assert e.get_active()[0]["state"] == BreadButterSellState.WAITING.value


def test_a_bias_dict_is_read_and_stored():
    e = engine(require_bias=True)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD",
              bias={"allowed_direction": "SHORT", "verdict": "BEARISH"})
    r = e.get_active()[0]
    assert r["bias"] == "SHORT"
    assert r["state"] == BreadButterSellState.WAITING.value


def test_the_gate_refuses_to_arm_without_a_bias():
    e = engine(require_bias=True)
    assert e.analyze(leg_candles(), tf=15, symbol="EURUSD") == []
    assert e.get_active() == []


def test_without_the_gate_a_bullish_bias_is_ignored():
    e = engine(require_bias=False)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BULLISH")
    assert e.get_active()[0]["state"] == BreadButterSellState.WAITING.value


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert engine().analyze([], tf=15, symbol="EURUSD") == []
    assert engine().analyze(leg_candles()[:1], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = engine(), engine()
    a.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    b.analyze(leg_candles() + [RETRACE], tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    e.analyze(leg_candles() + [TARGET], tf=15, symbol="XAUUSD")
    assert len(e.get_all()) == 2


def test_reset_clears_everything():
    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    e.reset()
    assert e.get_active() == []
    assert e.get_events() == []


def test_default_config_is_the_module_default():
    assert BreadButterSellEngine().config is DEFAULT_BREAD_BUTTER_SELL_CONFIG
