"""Tests for the standalone ICT Friday seek-&-destroy detector (weekly raid that reverses)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.friday_seek_destroy import (
    DEFAULT_FRIDAY_SEEK_DESTROY_CONFIG,
    FridaySeekDestroyConfig,
    FridaySeekDestroyEngine,
    FridaySeekDestroyEvent,
    FridaySeekDestroyState,
)

DAY = 86400
HOUR = 3600
QUARTER = 900

MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())   # a Monday
FRIDAY = MONDAY + 4 * DAY


def candle(ts, o, h, l, c):
    return {"open_time": ts, "open": o, "high": h, "low": l, "close": c}


def week_bar(offset, o, h, l, c):
    """A bar on Monday+offset at 08:00 UTC — the days that build the week's range."""
    return candle(MONDAY + offset * DAY + 8 * HOUR, o, h, l, c)


def friday_bar(i, o, h, l, c):
    """A bar on Friday at 07:00 + i*15m — the day that does the raiding."""
    return candle(FRIDAY + 7 * HOUR + i * QUARTER, o, h, l, c)


def engine(**overrides):
    return FridaySeekDestroyEngine(FridaySeekDestroyConfig(**overrides))


def week_candles():
    """Mon..Thu: week high 1.09000 on Wednesday, week low 1.08000 on Thursday."""
    return [
        week_bar(0, 1.08400, 1.08800, 1.08200, 1.08700),
        week_bar(1, 1.08700, 1.08900, 1.08100, 1.08300),
        week_bar(2, 1.08300, 1.09000, 1.08300, 1.08600),
        week_bar(3, 1.08600, 1.08800, 1.08000, 1.08200),
    ]


SEEK_UP = friday_bar(0, 1.08950, 1.09120, 1.08900, 1.09050)
DESTROY = friday_bar(1, 1.09050, 1.09060, 1.08900, 1.08920)
HOLD_UP = friday_bar(1, 1.09050, 1.09100, 1.09020, 1.09080)
HOLD_UP_2 = friday_bar(2, 1.09080, 1.09130, 1.09060, 1.09110)
DIP = friday_bar(1, 1.09080, 1.09100, 1.08950, 1.09060)
SEEK_DOWN = friday_bar(0, 1.08050, 1.08100, 1.07880, 1.07950)
DESTROY_UP = friday_bar(1, 1.07950, 1.08100, 1.07900, 1.08080)
QUIET = friday_bar(0, 1.08500, 1.08600, 1.08400, 1.08550)


# ── week range + day gating ─────────────────────────────────────────────────

def test_a_non_friday_is_reported_as_such():
    rec = engine().analyze(week_candles(), tf=15, symbol="EURUSD")
    assert rec == []
    assert engine().get_active() == []

    e = engine()
    e.analyze(week_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == FridaySeekDestroyState.NOT_FRIDAY.value
    assert e.get_active()[0]["weekday"] == 3          # Thursday
    assert e.get_events() == []


def test_no_earlier_days_means_no_week_range():
    e = engine()
    e.analyze([QUIET, friday_bar(1, 1.08550, 1.08600, 1.08500, 1.08550)],
              tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == FridaySeekDestroyState.NO_WEEK_RANGE.value


def test_the_week_range_comes_from_the_days_before_today():
    e = engine()
    e.analyze(week_candles() + [QUIET], tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == FridaySeekDestroyState.WAITING.value
    assert rec["weekday"] == 4
    assert rec["week_high"] == approx(1.09000)
    assert rec["week_low"] == approx(1.08000)


def test_the_friday_gate_can_be_switched_off():
    e = engine(require_friday=False)
    e.analyze(week_candles(), tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == FridaySeekDestroyState.SEEK_DOWN.value
    assert rec["week_low"] == approx(1.08100)      # Mon..Wed only when today is Thursday


# ── the seek ────────────────────────────────────────────────────────────────

def test_a_raid_above_the_week_high_is_a_seek_up():
    e = engine()
    events = e.analyze(week_candles() + [SEEK_UP], tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in events] == [FridaySeekDestroyEvent.SEEK_UP.value]
    rec = e.get_active()[0]
    assert rec["state"] == FridaySeekDestroyState.SEEK_UP.value
    assert rec["seek_side"] == "UP"
    assert rec["seek_price"] == approx(1.09120)
    assert rec["seek_pips"] == approx(12.0)
    assert rec["direction"] == "SHORT"
    assert rec["stop_price"] == approx(1.09140)
    assert rec["target_price"] == approx(1.08000)


def test_a_raid_below_the_week_low_is_a_seek_down():
    e = engine()
    events = e.analyze(week_candles() + [SEEK_DOWN], tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in events] == [FridaySeekDestroyEvent.SEEK_DOWN.value]
    rec = e.get_active()[0]
    assert rec["state"] == FridaySeekDestroyState.SEEK_DOWN.value
    assert rec["seek_side"] == "DOWN"
    assert rec["seek_pips"] == approx(12.0)
    assert rec["direction"] == "LONG"
    assert rec["stop_price"] == approx(1.07860)
    assert rec["target_price"] == approx(1.09000)


def test_a_raid_smaller_than_the_threshold_is_not_a_seek():
    e = engine(min_seek_pips=30.0)
    assert e.analyze(week_candles() + [SEEK_UP], tf=15, symbol="EURUSD") == []
    assert e.get_active()[0]["state"] == FridaySeekDestroyState.WAITING.value


# ── the destroy ─────────────────────────────────────────────────────────────

def test_a_close_back_inside_after_a_seek_up_is_destroyed():
    e = engine()
    events = e.analyze(week_candles() + [SEEK_UP, DESTROY], tf=15, symbol="EURUSD")
    # one call reports the FINAL state's diff, so the destroy is what it says
    assert [ev["event"] for ev in events] == [FridaySeekDestroyEvent.DESTROYED.value]
    rec = e.get_active()[0]
    assert rec["state"] == FridaySeekDestroyState.DESTROYED.value
    assert rec["destroy_price"] == approx(1.08920)
    assert rec["direction"] == "SHORT"
    assert rec["risk_pips"] == approx(22.0)
    assert rec["reward_pips"] == approx(92.0)
    assert rec["rr"] == approx(4.18)


def test_a_close_back_inside_after_a_seek_down_is_destroyed_long():
    e = engine()
    e.analyze(week_candles() + [SEEK_DOWN, DESTROY_UP], tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == FridaySeekDestroyState.DESTROYED.value
    assert rec["direction"] == "LONG"
    assert rec["stop_price"] == approx(1.07860)
    assert rec["risk_pips"] == approx(22.0)
    assert rec["reward_pips"] == approx(92.0)


def test_the_destroy_can_be_measured_on_the_wick():
    candles = week_candles() + [SEEK_UP, DIP]
    e = engine()
    e.analyze(candles, tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == FridaySeekDestroyState.SEEK_UP.value

    e2 = engine(destroy_confirm="wick")
    e2.analyze(candles, tf=15, symbol="EURUSD")
    assert e2.get_active()[0]["state"] == FridaySeekDestroyState.DESTROYED.value


def test_no_destroy_within_the_window_invalidates():
    candles = week_candles() + [SEEK_UP, HOLD_UP, HOLD_UP_2]
    e = engine(destroy_window_bars=1)
    events = e.analyze(candles, tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in events] == [FridaySeekDestroyEvent.INVALIDATED.value]
    assert e.get_active()[0]["state"] == FridaySeekDestroyState.INVALIDATED.value


def test_a_window_that_has_not_elapsed_yet_keeps_waiting():
    e = engine(destroy_window_bars=5)
    e.analyze(week_candles() + [SEEK_UP, HOLD_UP], tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == FridaySeekDestroyState.SEEK_UP.value


def test_the_extension_target_runs_beyond_the_week_range():
    e = engine(target_mode="week_extension", target_extension=1.0)
    e.analyze(week_candles() + [SEEK_UP, DESTROY], tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["target_price"] == approx(1.07000)      # one week-height below the week low
    assert rec["reward_pips"] == approx(192.0)


# ── lifecycle + events ──────────────────────────────────────────────────────

def test_the_setup_walks_seek_then_destroy():
    e = engine()
    first = e.analyze(week_candles() + [SEEK_UP], tf=15, symbol="EURUSD")
    second = e.analyze(week_candles() + [SEEK_UP, DESTROY], tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in first] == [FridaySeekDestroyEvent.SEEK_UP.value]
    assert [ev["event"] for ev in second] == [FridaySeekDestroyEvent.DESTROYED.value]


def test_an_unchanged_state_says_nothing_twice():
    e = engine()
    e.analyze(week_candles() + [SEEK_UP], tf=15, symbol="EURUSD")
    assert e.analyze(week_candles() + [SEEK_UP], tf=15, symbol="EURUSD") == []


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert engine().analyze([], tf=15, symbol="EURUSD") == []
    assert engine().analyze([QUIET], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = engine(), engine()
    a.analyze(week_candles() + [SEEK_UP, DESTROY], tf=15, symbol="EURUSD")
    b.analyze(week_candles() + [SEEK_UP, DESTROY], tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    e = engine()
    e.analyze(week_candles() + [SEEK_UP, DESTROY], tf=15, symbol="EURUSD")
    e.analyze(week_candles() + [SEEK_DOWN, DESTROY_UP], tf=15, symbol="XAUUSD")
    assert len(e.get_all()) == 2


def test_reset_clears_everything():
    e = engine()
    e.analyze(week_candles() + [SEEK_UP, DESTROY], tf=15, symbol="EURUSD")
    e.reset()
    assert e.get_active() == []
    assert e.get_events() == []


def test_default_config_is_the_module_default():
    assert FridaySeekDestroyEngine().config is DEFAULT_FRIDAY_SEEK_DESTROY_CONFIG
