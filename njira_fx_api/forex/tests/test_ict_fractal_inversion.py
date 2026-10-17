"""Tests for the standalone ICT fractal-inversion detector (a broken HTF level that flips)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.fractal_inversion import (
    DEFAULT_FRACTAL_INVERSION_CONFIG,
    FractalInversionConfig,
    FractalInversionEngine,
    FractalInversionEvent,
    FractalInversionState,
)

HOUR = 3600
QUARTER = 900
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())   # a Monday


def candle(ts, o, h, l, c):
    return {"open_time": ts, "open": o, "high": h, "low": l, "close": c}


def block(hour, rows):
    """Four 15m bars starting at the given hour, from (open, high, low, close) rows."""
    base = MONDAY + hour * HOUR
    return [candle(base + i * QUARTER, o, h, l, c)
            for i, (o, h, l, c) in enumerate(rows)]


def engine(**overrides):
    # one-hour HTF bars out of 15m input, and one bar each side confirms a fractal
    return FractalInversionEngine(
        FractalInversionConfig(htf_minutes=60, fractal_lookback=1, **overrides))


# HTF extremes: 09:00 1.0840/1.0820 - 10:00 1.0850/1.0800 - 11:00 1.0900/1.0840
#               12:00 1.0890/1.0830 - 13:00 1.0860/1.0840 - 14:00 1.0930/1.0855
#               15:00 1.0930/1.0899
# Confirmed fractals: HIGH 1.0900 @11:00, LOWS 1.0800 @10:00 and 1.0830 @12:00.
BLOCK9 = block(9, [(1.0830, 1.0832, 1.0820, 1.0826),
                   (1.0826, 1.0840, 1.0824, 1.0835),
                   (1.0835, 1.0838, 1.0830, 1.0836),
                   (1.0836, 1.0838, 1.0832, 1.0835)])
BLOCK10 = block(10, [(1.0835, 1.0840, 1.0830, 1.0832),
                     (1.0832, 1.0850, 1.0800, 1.0845),
                     (1.0845, 1.0848, 1.0838, 1.0842),
                     (1.0842, 1.0844, 1.0838, 1.0840)])
BLOCK11 = block(11, [(1.0840, 1.0855, 1.0840, 1.0850),
                     (1.0850, 1.0900, 1.0848, 1.0870),
                     (1.0870, 1.0885, 1.0860, 1.0880),
                     (1.0880, 1.0885, 1.0870, 1.0875)])
BLOCK12 = block(12, [(1.0875, 1.0890, 1.0870, 1.0885),
                     (1.0885, 1.0888, 1.0860, 1.0865),
                     (1.0865, 1.0870, 1.0830, 1.0840),
                     (1.0840, 1.0845, 1.0835, 1.0838)])
BLOCK13 = block(13, [(1.0838, 1.0850, 1.0838, 1.0845),
                     (1.0845, 1.0860, 1.0842, 1.0855),
                     (1.0855, 1.0858, 1.0850, 1.0852),
                     (1.0852, 1.0856, 1.0840, 1.0845)])
BLOCK14 = block(14, [(1.0845, 1.0860, 1.0855, 1.0858),
                     (1.0858, 1.0930, 1.0856, 1.0930),      # closes above the level
                     (1.0930, 1.0930, 1.0902, 1.0925),      # comes back and touches it
                     (1.0925, 1.0928, 1.0920, 1.0920)])
BLOCK15 = block(15, [(1.0920, 1.0930, 1.0910, 1.0925),
                     (1.0925, 1.0930, 1.0900, 1.0910),
                     (1.0910, 1.0915, 1.0899, 1.0905),
                     (1.0905, 1.0912, 1.0900, 1.0910)])
# the same break, but price stays away from the level instead of retesting it
BLOCK14_BREAK_ONLY = block(14, [(1.0845, 1.0860, 1.0855, 1.0858),
                                (1.0858, 1.0930, 1.0856, 1.0930),
                                (1.0930, 1.0930, 1.0910, 1.0920),
                                (1.0920, 1.0925, 1.0908, 1.0915)])
BLOCK15_BAD = block(15, [(1.0915, 1.0930, 1.0880, 1.0880),      # closes back through
                         (1.0880, 1.0890, 1.0870, 1.0885),
                         (1.0885, 1.0892, 1.0875, 1.0880),
                         (1.0880, 1.0886, 1.0870, 1.0875)])


def waiting_candles():
    return BLOCK9 + BLOCK10 + BLOCK11 + BLOCK12 + BLOCK13


def no_retest_candles():
    return waiting_candles() + BLOCK14_BREAK_ONLY


def full_candles():
    return BLOCK9 + BLOCK10 + BLOCK11 + BLOCK12 + BLOCK13 + BLOCK14 + BLOCK15


def invalidated_candles():
    return waiting_candles() + BLOCK14_BREAK_ONLY + BLOCK15_BAD


# ── fractal discovery ───────────────────────────────────────────────────────

def test_not_enough_htf_history_means_no_fractal():
    e = engine()
    assert e.analyze(BLOCK9 + BLOCK10, tf=15, symbol="EURUSD") == []
    assert e.get_active()[0]["state"] == FractalInversionState.NO_FRACTAL.value


def test_the_higher_timeframe_is_configurable():
    # four-hour bars out of seven hourly blocks leaves only two bars — no fractal
    e = FractalInversionEngine(FractalInversionConfig(htf_minutes=240, fractal_lookback=1))
    e.analyze(full_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == FractalInversionState.NO_FRACTAL.value


def test_a_level_with_no_break_just_waits():
    e = engine()
    assert e.analyze(waiting_candles(), tf=15, symbol="EURUSD") == []
    assert e.get_active()[0]["state"] == FractalInversionState.WAITING.value


# ── the break ───────────────────────────────────────────────────────────────

def test_closing_above_the_fractal_high_is_a_broken_up():
    e = engine(retest_tolerance_pips=0.5)      # tolerance too tight to call it a retest
    events = e.analyze(no_retest_candles(), tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in events] == [FractalInversionEvent.BROKEN_UP.value]
    rec = e.get_active()[0]
    assert rec["state"] == FractalInversionState.BROKEN_UP.value
    assert rec["level_side"] == "HIGH"
    assert rec["level_price"] == approx(1.09000)
    assert rec["break_price"] == approx(1.09300)
    assert rec["direction"] == "LONG"


def test_a_break_smaller_than_the_buffer_does_not_count():
    e = engine(break_buffer_pips=200.0)
    assert e.analyze(no_retest_candles(), tf=15, symbol="EURUSD") == []
    assert e.get_active()[0]["state"] == FractalInversionState.WAITING.value


# ── the inversion ───────────────────────────────────────────────────────────

def test_the_retest_through_the_flipped_level_confirms_a_long():
    e = engine()
    events = e.analyze(full_candles(), tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in events] == [FractalInversionEvent.CONFIRMED_LONG.value]
    rec = e.get_active()[0]
    assert rec["state"] == FractalInversionState.CONFIRMED_LONG.value
    assert rec["level_side"] == "HIGH"
    assert rec["level_price"] == approx(1.09000)
    assert rec["retest_price"] == approx(1.09250)
    assert rec["retest_extreme"] == approx(1.09020)
    assert rec["entry_price"] == approx(1.09250)
    assert rec["stop_price"] == approx(1.09000)
    assert rec["risk_pips"] == approx(25.0)
    assert rec["target_price"] == approx(1.09700)      # the level's own swing height projected
    assert rec["reward_pips"] == approx(45.0)
    assert rec["rr"] == approx(1.8)


def test_a_close_back_through_the_level_invalidates():
    e = engine()
    events = e.analyze(invalidated_candles(), tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in events] == [FractalInversionEvent.INVALIDATED.value]
    rec = e.get_active()[0]
    assert rec["state"] == FractalInversionState.INVALIDATED.value
    assert rec["retest_price"] == approx(1.08800)
    assert rec["direction"] == "LONG"


def test_a_window_that_elapses_without_a_retest_falls_back_to_waiting():
    e = engine(retest_window_bars=0)
    e.analyze(full_candles(), tf=15, symbol="EURUSD")
    assert e.get_active()[0]["state"] == FractalInversionState.WAITING.value


def test_no_fractal_beyond_the_level_leaves_the_target_at_zero():
    e = engine(target_mode="next_fractal")
    e.analyze(full_candles(), tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["state"] == FractalInversionState.CONFIRMED_LONG.value
    assert rec["target_price"] == approx(0.0)
    assert rec["rr"] == approx(0.0)


# ── lifecycle + events ──────────────────────────────────────────────────────

def test_the_setup_walks_break_then_confirmation():
    e = engine()
    first = e.analyze(no_retest_candles(), tf=15, symbol="EURUSD")
    second = e.analyze(full_candles(), tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in first] == [FractalInversionEvent.BROKEN_UP.value]
    assert [ev["event"] for ev in second] == [FractalInversionEvent.CONFIRMED_LONG.value]


def test_an_unchanged_state_says_nothing_twice():
    e = engine()
    e.analyze(full_candles(), tf=15, symbol="EURUSD")
    assert e.analyze(full_candles(), tf=15, symbol="EURUSD") == []


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert engine().analyze([], tf=15, symbol="EURUSD") == []
    assert engine().analyze([BLOCK9[0]], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = engine(), engine()
    a.analyze(full_candles(), tf=15, symbol="EURUSD")
    b.analyze(full_candles(), tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    e = engine()
    e.analyze(full_candles(), tf=15, symbol="EURUSD")
    e.analyze(no_retest_candles(), tf=15, symbol="XAUUSD")
    assert len(e.get_all()) == 2


def test_reset_clears_everything():
    e = engine()
    e.analyze(full_candles(), tf=15, symbol="EURUSD")
    e.reset()
    assert e.get_active() == []
    assert e.get_events() == []


def test_default_config_is_the_module_default():
    assert FractalInversionEngine().config is DEFAULT_FRACTAL_INVERSION_CONFIG
