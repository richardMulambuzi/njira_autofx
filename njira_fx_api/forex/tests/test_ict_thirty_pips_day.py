"""Tests for the standalone ICT thirty-pips-a-day detector (one fixed-target trade per day)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.thirty_pips_day import (
    DEFAULT_THIRTY_PIPS_DAY_CONFIG,
    ThirtyPipsDayConfig,
    ThirtyPipsDayEngine,
    ThirtyPipsDayEvent,
    ThirtyPipsDayState,
)

QUARTER = 900
DAY = 86400
# 07:00 UTC — inside the London kill zone (7–10), so the gate is open by default
BASE = int(datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc).timestamp())


def bar(i, o, h, l, c, base=BASE):
    return {"open_time": base + i * QUARTER, "open": o, "high": h, "low": l, "close": c}


def engine(**overrides):
    # 10-candle fixtures: lookback must stay small enough for swing_lookback *2 bars to fit
    return ThirtyPipsDayEngine(ThirtyPipsDayConfig(swing_lookback=2, **overrides))


def leg_candles():
    """Swing LOW 1.08000 at index 3, impulse to a confirmed swing HIGH 1.09000 at index 7."""
    return [
        bar(0, 1.08200, 1.08250, 1.08150, 1.08200),
        bar(1, 1.08200, 1.08250, 1.08100, 1.08150),
        bar(2, 1.08150, 1.08200, 1.08050, 1.08100),
        bar(3, 1.08100, 1.08120, 1.08000, 1.08050),   # swing LOW
        bar(4, 1.08050, 1.08300, 1.08030, 1.08280),
        bar(5, 1.08280, 1.08500, 1.08250, 1.08480),
        bar(6, 1.08480, 1.08700, 1.08450, 1.08680),
        bar(7, 1.08680, 1.09000, 1.08650, 1.08950),   # swing HIGH
        bar(8, 1.08950, 1.08980, 1.08600, 1.08650),   # confirmation only
        bar(9, 1.08650, 1.08900, 1.08550, 1.08800),   # confirmation only
    ]


# entry 1.08380 (62% of the 100-pip leg), stop 1.07980, target 1.08680 (30 pips)
FILL = bar(10, 1.08950, 1.08500, 1.08350, 1.08400)      # touches entry, high under target
NO_FILL = bar(10, 1.08950, 1.08550, 1.08450, 1.08500)   # stays above the entry
TARGET = bar(10, 1.08350, 1.08700, 1.08340, 1.08650)    # fills and runs 30 pips
INVALIDATE = bar(10, 1.08600, 1.08650, 1.07900, 1.07950)  # closes under the stop


# ── the leg and the fixed target ────────────────────────────────────────────

def test_prices_the_leg_into_a_fixed_thirty_pip_target():
    events = engine().analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [ThirtyPipsDayEvent.LEG_SET.value]

    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == ThirtyPipsDayState.WAITING.value
    assert rec["direction"] == "LONG"
    assert rec["leg_low"] == approx(1.08000)
    assert rec["leg_high"] == approx(1.09000)
    assert rec["leg_pips"] == approx(100.0)
    assert rec["entry_price"] == approx(1.08380)
    assert rec["stop_price"] == approx(1.07980)
    assert rec["target_price"] == approx(1.08680)
    assert rec["risk_pips"] == approx(40.0)
    assert rec["reward_pips"] == approx(30.0)
    assert rec["in_killzone"] is True


def test_the_target_is_configurable():
    e = engine(target_pips=50.0)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["target_price"] == approx(1.08880)
    assert e.get_active()[0]["reward_pips"] == approx(50.0)


def test_the_other_source_band_moves_the_entry():
    e = engine(band_low=0.50)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["entry_price"] == approx(1.08500)


def test_a_leg_smaller_than_the_minimum_is_ignored():
    e = engine(min_leg_pips=500.0)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.NO_LEG.value


def test_a_leg_longer_than_the_lookback_is_ignored():
    e = engine(leg_lookback=1)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.NO_LEG.value


def test_too_few_candles_to_confirm_a_swing_has_no_leg():
    e = engine()
    e.analyze(leg_candles()[:4], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.NO_LEG.value


# ── fill, then exit (exits may not count before the fill) ───────────────────

def test_price_must_fill_the_entry_before_anything_else_counts():
    e = engine()
    feed = leg_candles() + [NO_FILL]
    e.analyze(feed, tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == ThirtyPipsDayState.WAITING.value
    assert rec["in_band"] is False


def test_reaching_the_entry_reads_as_in_band():
    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")          # emits TPD_LEG_SET
    events = e.analyze(leg_candles() + [FILL], tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == ThirtyPipsDayState.IN_BAND.value
    assert rec["in_band"] is True
    assert [x["event"] for x in events] == [ThirtyPipsDayEvent.IN_BAND.value]


def test_running_thirty_pips_after_the_fill_is_the_target():
    e = engine()
    events = e.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.TARGET_HIT.value
    assert events[-1]["event"] == ThirtyPipsDayEvent.TARGET_HIT.value


def test_a_close_under_the_stop_invalidates_before_the_fill():
    e = engine()
    events = e.analyze(leg_candles() + [INVALIDATE], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.INVALIDATED.value
    assert events[-1]["event"] == ThirtyPipsDayEvent.INVALIDATED.value


# ── rule 2: one trade per day ───────────────────────────────────────────────

def test_the_day_is_spent_after_the_target_is_taken():
    e = engine()
    e.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.TARGET_HIT.value

    events = e.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.DONE_FOR_DAY.value
    assert events[-1]["event"] == ThirtyPipsDayEvent.DONE_FOR_DAY.value


def test_the_day_is_spent_after_a_loss_too():
    e = engine()
    e.analyze(leg_candles() + [INVALIDATE], tf=15, symbol="EURUSD")
    e.analyze(leg_candles() + [INVALIDATE], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.DONE_FOR_DAY.value


def test_the_next_day_is_a_clean_slate():
    e = engine()
    e.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    e.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.DONE_FOR_DAY.value

    tomorrow = [dict(c, open_time=c["open_time"] + DAY) for c in leg_candles()]
    tomorrow.append(dict(TARGET, open_time=TARGET["open_time"] + DAY))
    e.analyze(tomorrow, tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.TARGET_HIT.value


# ── gates ───────────────────────────────────────────────────────────────────

def test_outside_a_kill_zone_the_gate_blocks():
    outside = 16 * 3600          # 16:00 UTC — no ICT kill zone is open
    feed = [dict(c, open_time=c["open_time"] - 7 * 3600 + outside) for c in leg_candles()]
    e = engine()
    events = e.analyze(feed, tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == ThirtyPipsDayState.GATE_BLOCKED.value
    assert rec["in_killzone"] is False
    assert events[0]["event"] == ThirtyPipsDayEvent.GATE_BLOCKED.value


def test_the_kill_zone_gate_can_be_turned_off():
    outside = 16 * 3600
    feed = [dict(c, open_time=c["open_time"] - 7 * 3600 + outside) for c in leg_candles()]
    e = engine(require_killzone=False)
    e.analyze(feed, tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.WAITING.value


def test_a_contrary_bias_blocks_the_setup():
    e = engine(require_bias=True)
    events = e.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BEARISH")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.GATE_BLOCKED.value
    assert events[0]["event"] == ThirtyPipsDayEvent.GATE_BLOCKED.value


def test_an_agreeing_bias_lets_the_setup_through():
    e = engine(require_bias=True)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BULLISH")
    rec = e.get_active()[0]
    assert rec["state"] == ThirtyPipsDayState.WAITING.value
    assert rec["bias"] == "BULLISH"


def test_a_bias_dict_is_read_and_stored():
    e = engine(require_bias=True)
    e.analyze(leg_candles(), tf=15, symbol="EURUSD",
              bias={"allowed_direction": "LONG", "verdict": "BULLISH"})
    assert e.get_active()[0]["bias"] == "LONG"


def test_a_required_bias_that_is_missing_stays_silent():
    e = engine(require_bias=True)
    assert e.analyze(leg_candles(), tf=15, symbol="EURUSD") == []
    assert e.get_active() == []


def test_without_the_gate_a_contrary_bias_is_ignored():
    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD", bias="BEARISH")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.WAITING.value


# ── hygiene ─────────────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert engine().analyze([], tf=15, symbol="EURUSD") == []
    assert engine().analyze(leg_candles()[:1], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = engine(), engine()
    a.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    b.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    e = engine()
    e.analyze(leg_candles(), tf=15, symbol="EURUSD")
    e.analyze(leg_candles(), tf=15, symbol="XAUUSD")
    assert len(e.get_all()) == 2


def test_reset_clears_everything_including_the_day_lock():
    e = engine()
    e.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    e.reset()
    assert e.get_active() == []
    assert e.get_events() == []
    e.analyze(leg_candles() + [TARGET], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == ThirtyPipsDayState.TARGET_HIT.value


def test_default_config_is_the_module_default():
    assert ThirtyPipsDayEngine().config is DEFAULT_THIRTY_PIPS_DAY_CONFIG
