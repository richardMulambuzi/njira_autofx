"""Tests for the standalone ICT Venom model (strike on a range's liquidity)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.venom import (
    DEFAULT_VENOM_CONFIG,
    VenomConfig,
    VenomEngine,
    VenomEvent,
    VenomState,
)

HOUR = 3600
QUARTER = 900
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())
LONDON = MONDAY + 7 * HOUR


def candle(ts, o, h, l, c):
    return {"open_time": ts, "open": o, "high": h, "low": l, "close": c}


def _engine(**overrides):
    return VenomEngine(VenomConfig(**overrides))


def range_bars():
    """00:00-01:45 UTC window: low 1.08000, high 1.08500."""
    bars = [candle(MONDAY + i * QUARTER, 1.08250, 1.08480, 1.08020, 1.08300)
            for i in range(8)]
    bars[3] = candle(MONDAY + 3 * QUARTER, 1.08250, 1.08480, 1.08000, 1.08100)
    bars[7] = candle(MONDAY + 7 * QUARTER, 1.08250, 1.08500, 1.08020, 1.08400)
    return bars


QUIET = candle(LONDON, 1.08300, 1.08400, 1.08050, 1.08350)          # stabs nothing
STAB_DOWN = candle(LONDON, 1.08300, 1.08350, 1.07900, 1.08100)      # takes the low
DELIVER_UP = candle(LONDON + HOUR, 1.08100, 1.08550, 1.08080, 1.08400)
STAB_UP = candle(LONDON, 1.08300, 1.08600, 1.08280, 1.08400)        # takes the high
DELIVER_DOWN = candle(LONDON + HOUR, 1.08400, 1.08420, 1.07950, 1.08050)
BREAK_DOWN = candle(LONDON + HOUR, 1.08100, 1.08120, 1.07850, 1.07950)


# ── range construction ──────────────────────────────────────────────────────

def test_range_is_built_from_the_last_closed_window():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["state"] == VenomState.RANGE_SET.value
    assert rec["range_high"] == approx(1.08500)
    assert rec["range_low"] == approx(1.08000)
    assert rec["range_bars"] == 8


def test_no_closed_window_yet_produces_nothing():
    engine = _engine()
    assert engine.analyze(range_bars(), tf=15, symbol="EURUSD") == []
    assert engine.get_active() == []


def test_window_hours_are_configurable():
    engine = _engine(range_start_hour_utc=13, range_end_hour_utc=16)
    assert engine.analyze(range_bars() + [QUIET], tf=15, symbol="EURUSD") == []


# ── the strike ──────────────────────────────────────────────────────────────

def test_stab_below_the_low_is_a_down_strike_targeting_the_high():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_DOWN], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["state"] == VenomState.STRIKE_DOWN.value
    assert rec["strike_side"] == "DOWN"
    assert rec["strike_price"] == approx(1.07900)
    assert rec["sweep_depth_pips"] == approx(10.0)
    assert rec["target_price"] == approx(1.08500)


def test_stab_above_the_high_is_an_up_strike_targeting_the_low():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_UP], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["state"] == VenomState.STRIKE_UP.value
    assert rec["strike_side"] == "UP"
    assert rec["sweep_depth_pips"] == approx(10.0)
    assert rec["target_price"] == approx(1.08000)


def test_stab_below_the_threshold_is_not_a_strike():
    engine = _engine(min_sweep_pips=50.0)
    engine.analyze(range_bars() + [QUIET, STAB_DOWN], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.RANGE_SET.value


def test_close_confirmation_ignores_a_wick_only_stab():
    engine = _engine(sweep_confirm="close")
    engine.analyze(range_bars() + [QUIET, STAB_DOWN], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.RANGE_SET.value


def test_close_confirmation_accepts_a_close_through_the_shelf():
    engine = _engine(sweep_confirm="close")
    closed_through = candle(LONDON, 1.08300, 1.08350, 1.07900, 1.07950)
    engine.analyze(range_bars() + [QUIET, closed_through], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.STRIKE_DOWN.value


# ── delivery and death ──────────────────────────────────────────────────────

def test_reaching_the_opposite_extreme_is_delivery():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_DOWN, DELIVER_UP], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.DELIVERED.value


def test_reaching_the_low_after_an_up_strike_is_delivery():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_UP, DELIVER_DOWN], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.DELIVERED.value


def test_a_close_through_the_stabbed_side_kills_the_model():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_DOWN, BREAK_DOWN], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.INVALIDATED.value


def test_the_invalidation_buffer_is_configurable():
    engine = _engine(invalidate_buffer_pips=30.0)
    engine.analyze(range_bars() + [QUIET, STAB_DOWN, BREAK_DOWN], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.STRIKE_DOWN.value


def test_delivery_is_preferred_when_it_comes_first():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_DOWN, DELIVER_UP, BREAK_DOWN],
                   tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == VenomState.DELIVERED.value


def test_range_extension_target_is_configurable():
    engine = _engine(target_mode="range_extension", target_extension=1.0)
    engine.analyze(range_bars() + [QUIET, STAB_DOWN], tf=15, symbol="EURUSD")
    # one range-height (50 pips) beyond the far side, not the far side itself
    assert engine.get_active()[0]["target_price"] == approx(1.09000)


# ── events ──────────────────────────────────────────────────────────────────

def test_events_follow_the_lifecycle():
    engine = _engine()
    events = engine.analyze(range_bars() + [QUIET], tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [VenomEvent.RANGE_SET.value]

    events = engine.analyze(range_bars() + [QUIET, STAB_DOWN], tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [VenomEvent.STRIKE_DOWN.value]
    assert events[0]["target_price"] == approx(1.08500)

    events = engine.analyze(range_bars() + [QUIET, STAB_DOWN, DELIVER_UP],
                            tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [VenomEvent.DELIVERED.value]


def test_an_unchanged_state_emits_nothing_twice():
    engine = _engine()
    candles = range_bars() + [QUIET]
    engine.analyze(candles, tf=15, symbol="EURUSD")
    assert engine.analyze(candles, tf=15, symbol="EURUSD") == []


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert _engine().analyze([], tf=15, symbol="EURUSD") == []
    assert _engine().analyze(range_bars()[:1], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = _engine(), _engine()
    candles = range_bars() + [QUIET, STAB_DOWN, DELIVER_UP]
    a.analyze(candles, tf=15, symbol="EURUSD")
    b.analyze(candles, tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_DOWN], tf=15, symbol="EURUSD")
    engine.analyze(range_bars() + [QUIET, STAB_UP], tf=15, symbol="XAUUSD")
    assert len(engine.get_all()) == 2


def test_reset_clears_everything():
    engine = _engine()
    engine.analyze(range_bars() + [QUIET, STAB_DOWN], tf=15, symbol="EURUSD")
    engine.reset()
    assert engine.get_active() == []
    assert engine.get_events() == []


def test_default_config_is_the_module_default():
    assert VenomEngine().config is DEFAULT_VENOM_CONFIG
