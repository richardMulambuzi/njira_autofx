"""Tests for the Asian-range detector (ICT register item §22).

Synthetic candles only: no fixtures, no DB, no network.  Every Config field is
exercised as a gate, and the module is checked for determinism.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.asian_range import (
    AsianRangeConfig,
    AsianRangeEngine,
    AsianRangeEvent,
    AsianRangeState,
    in_window,
    planner_levels,
    window_indices,
)

HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())


def bar(hour, high, low, close=None, open_=None, day=0):
    """One synthetic candle at `hour` UTC on the test Monday (+`day` days)."""
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = low if open_ is None else open_
    close = open_ if close is None else close
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def asian_bars():
    """Four bars inside the primary window (01:00-05:00 UTC)."""
    return [
        bar(1, 1.0850, 1.0840, close=1.0845),
        bar(2, 1.0855, 1.0845, close=1.0850),
        bar(3, 1.0860, 1.0848, close=1.0855),
        bar(4, 1.0858, 1.0850, close=1.0852),
    ]


def engine(**overrides):
    return AsianRangeEngine(AsianRangeConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


# ── pure helpers ────────────────────────────────────────────────────────────

def test_in_window_is_half_open_and_wraps_midnight():
    assert in_window(MONDAY + 1 * HOUR, 1.0, 5.0) is True
    assert in_window(MONDAY + 5 * HOUR, 1.0, 5.0) is False   # end is exclusive
    assert in_window(MONDAY + 0 * HOUR, 1.0, 5.0) is False
    # wrapping window 20:00-02:00 spans midnight
    assert in_window(MONDAY + 21 * HOUR, 20.0, 2.0) is True
    assert in_window(MONDAY + 1 * HOUR, 20.0, 2.0) is True
    assert in_window(MONDAY + 12 * HOUR, 20.0, 2.0) is False
    # a zero-width window never matches
    assert in_window(MONDAY + 1 * HOUR, 2.0, 2.0) is False


def test_window_indices_picks_the_most_recent_contiguous_run():
    candles = [bar(1, 1.1, 1.0), bar(2, 1.1, 1.0), bar(6, 1.2, 1.1), bar(7, 1.2, 1.1)]
    assert window_indices(candles, 1.0, 5.0) == (0, 1)
    assert window_indices(candles, 6.0, 9.0) == (2, 3)
    assert window_indices(candles, 10.0, 12.0) is None


def test_planner_levels_project_both_directions_off_both_extremes():
    levels = planner_levels(1.0840, 1.0860, (0.5, 1.0, 1.5))
    assert set(levels) == {"0.5", "1", "1.5"}
    assert levels["0.5"]["upper"] == approx(1.0870)
    assert levels["1"]["upper"] == approx(1.0880)
    assert levels["1.5"]["upper"] == approx(1.0890)
    assert levels["0.5"]["lower"] == approx(1.0830)
    assert levels["1"]["lower"] == approx(1.0820)
    assert levels["1.5"]["lower"] == approx(1.0810)


# ── happy path ──────────────────────────────────────────────────────────────

def test_range_is_measured_over_the_window_bars():
    eng = engine()
    eng.analyze(asian_bars(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["range_high"] == approx(1.0860)
    assert rec["range_low"] == approx(1.0840)
    assert rec["range_mid"] == approx(1.0850)
    assert rec["span_pips"] == approx(20.0)
    assert rec["symbol"] == "EURUSD"
    assert rec["timeframe"] == 900


def test_building_while_price_is_still_inside_the_window():
    eng = engine()
    eng.analyze(asian_bars(), tf=900)
    rec = last(eng)
    assert rec["building"] is True
    assert rec["state"] == AsianRangeState.BUILDING.value


def test_locked_once_the_window_closes():
    eng = engine()
    candles = asian_bars() + [bar(6, 1.0856, 1.0850, close=1.0854)]
    eng.analyze(candles, tf=900)
    rec = last(eng)
    assert rec["building"] is False
    assert rec["state"] == AsianRangeState.LOCKED.value
    assert rec["swept_high"] is False
    assert rec["swept_low"] is False


def test_planner_levels_are_reported_on_the_record():
    eng = engine()
    eng.analyze(asian_bars(), tf=900)
    levels = last(eng)["planner_levels"]
    assert levels["1"]["upper"] == approx(1.0880)
    assert levels["1"]["lower"] == approx(1.0820)


# ── Config thresholds actually gate behaviour ───────────────────────────────

def test_planner_extensions_config_gates_the_report():
    eng = engine(planner_extensions=(1.0,))
    eng.analyze(asian_bars(), tf=900)
    assert set(last(eng)["planner_levels"]) == {"1"}


def test_hour_window_config_gates_which_bars_count():
    eng = engine(asian_start_hour_utc=6.0, asian_end_hour_utc=7.0)
    assert eng.analyze(asian_bars(), tf=900) == []


def test_alt_window_config_gates_the_alt_range():
    default = engine()
    default.analyze(asian_bars(), tf=900)
    assert last(default)["alt_range_high"] == approx(1.0850)
    assert last(default)["alt_range_low"] == approx(1.0840)

    moved = engine(alt_asian_start_hour_utc=2.0, alt_asian_end_hour_utc=4.0)
    moved.analyze(asian_bars(), tf=900)
    assert last(moved)["alt_range_high"] == approx(1.0860)
    assert last(moved)["alt_range_low"] == approx(1.0845)


def test_sweep_confirm_gates_whether_a_wick_counts():
    wick_only = asian_bars() + [bar(6, 1.0870, 1.0850, close=1.0855)]

    close_mode = engine()
    close_mode.analyze(wick_only, tf=900)
    assert last(close_mode)["swept_high"] is False
    assert last(close_mode)["state"] == AsianRangeState.LOCKED.value

    wick_mode = engine(sweep_confirm="wick")
    wick_mode.analyze(wick_only, tf=900)
    assert last(wick_mode)["swept_high"] is True
    assert last(wick_mode)["state"] == AsianRangeState.SWEPT_HIGH.value


def test_pip_size_config_gates_span_pips():
    eng = engine(pip_size=0.01)
    eng.analyze(asian_bars(), tf=900)
    assert last(eng)["span_pips"] == approx(0.2)


# ── sweeps ──────────────────────────────────────────────────────────────────

def test_swept_high_when_a_close_takes_the_high():
    eng = engine()
    eng.analyze(asian_bars() + [bar(6, 1.0870, 1.0855, close=1.0865)], tf=900)
    rec = last(eng)
    assert rec["swept_high"] is True
    assert rec["swept_low"] is False
    assert rec["state"] == AsianRangeState.SWEPT_HIGH.value
    assert rec["sweep_high_time"] == MONDAY + 6 * HOUR


def test_swept_low_when_a_close_takes_the_low():
    eng = engine()
    eng.analyze(asian_bars() + [bar(6, 1.0855, 1.0830, close=1.0835)], tf=900)
    rec = last(eng)
    assert rec["swept_low"] is True
    assert rec["swept_high"] is False
    assert rec["state"] == AsianRangeState.SWEPT_LOW.value


def test_swept_both_needs_two_bars_because_a_close_cannot_do_both():
    eng = engine()
    candles = asian_bars() + [
        bar(6, 1.0870, 1.0855, close=1.0865),
        bar(7, 1.0865, 1.0830, close=1.0835),
    ]
    eng.analyze(candles, tf=900)
    rec = last(eng)
    assert rec["swept_high"] is True
    assert rec["swept_low"] is True
    assert rec["state"] == AsianRangeState.SWEPT_BOTH.value


def test_a_bar_inside_the_window_never_counts_as_a_sweep():
    eng = engine()
    candles = asian_bars() + [bar(4, 1.0900, 1.0800, close=1.0890)]
    eng.analyze(candles, tf=900)
    rec = last(eng)
    # hour 4 is still inside the window, so it extends the range instead
    assert rec["range_high"] == approx(1.0900)
    assert rec["swept_high"] is False


# ── no-signal paths and boundaries ──────────────────────────────────────────

def test_no_bar_in_the_window_returns_empty():
    eng = engine()
    assert eng.analyze([bar(10, 1.1, 1.0), bar(11, 1.1, 1.0)], tf=900) == []


def test_single_candle_returns_empty():
    eng = engine()
    assert eng.analyze([bar(1, 1.1, 1.0)], tf=900) == []


def test_flat_range_returns_empty():
    eng = engine()
    flat = [bar(1, 1.0800, 1.0800), bar(2, 1.0800, 1.0800)]
    assert eng.analyze(flat, tf=900) == []


def test_missing_or_malformed_candles_do_not_crash():
    eng = engine()
    assert eng.analyze([None, {}], tf=900) == []


# ── lifecycle events ────────────────────────────────────────────────────────

def test_first_look_emits_range_locked_then_stays_quiet():
    eng = engine()
    first = eng.analyze(asian_bars(), tf=900)
    assert [e["event"] for e in first] == [AsianRangeEvent.ASIAN_RANGE_LOCKED.value]
    assert eng.analyze(asian_bars(), tf=900) == []


def test_event_payload_carries_the_whole_record():
    eng = engine()
    first = eng.analyze(asian_bars(), tf=900)
    payload = first[0]
    assert payload["event"] == "AR_RANGE_LOCKED"
    assert payload["timestamp"] == MONDAY + 4 * HOUR
    assert payload["range_high"] == approx(1.0860)
    assert "created_at" not in payload


def test_closing_the_window_re_emits_range_locked():
    eng = engine()
    eng.analyze(asian_bars(), tf=900)
    events = eng.analyze(asian_bars() + [bar(6, 1.0856, 1.0850, close=1.0854)], tf=900)
    assert [e["event"] for e in events] == [AsianRangeEvent.ASIAN_RANGE_LOCKED.value]


def test_sweep_event_is_emitted_exactly_once():
    eng = engine()
    eng.analyze(asian_bars() + [bar(6, 1.0856, 1.0850, close=1.0854)], tf=900)
    swept = asian_bars() + [bar(6, 1.0870, 1.0855, close=1.0865)]
    first = eng.analyze(swept, tf=900)
    assert [e["event"] for e in first] == [AsianRangeEvent.ASIAN_HIGH_SWEPT.value]
    assert eng.analyze(swept, tf=900) == []


def test_a_new_session_day_re_emits_range_locked():
    eng = engine()
    eng.analyze(asian_bars(), tf=900)
    next_day = asian_bars() + [
        bar(6, 1.0856, 1.0850, close=1.0854),
        bar(1, 1.0900, 1.0880, close=1.0890, day=1),
        bar(2, 1.0905, 1.0885, close=1.0900, day=1),
    ]
    events = eng.analyze(next_day, tf=900)
    assert AsianRangeEvent.ASIAN_RANGE_LOCKED.value in [e["event"] for e in events]
    assert last(eng)["range_high"] == approx(1.0905)


# ── engine hygiene ──────────────────────────────────────────────────────────

def test_analyze_is_deterministic():
    candles = asian_bars() + [bar(6, 1.0870, 1.0855, close=1.0865)]
    a, b = engine(), engine()
    a.analyze(candles, tf=900, symbol="EURUSD")
    b.analyze(candles, tf=900, symbol="EURUSD")
    assert a.get_all() == b.get_all()
    assert a.get_events() == b.get_events()


def test_state_is_isolated_per_symbol_and_timeframe():
    eng = engine()
    eng.analyze(asian_bars(), tf=900, symbol="EURUSD")
    eng.analyze(asian_bars(), tf=3600, symbol="XAUUSD")
    assert len(eng.get_all()) == 2
    assert {r["symbol"] for r in eng.get_all()} == {"EURUSD", "XAUUSD"}


def test_reset_clears_state_and_events():
    eng = engine()
    eng.analyze(asian_bars(), tf=900, symbol="EURUSD")
    assert eng.get_events() != []
    eng.reset()
    assert eng.get_all() == []
    assert eng.get_active() == []
    assert eng.get_events() == []


def test_default_config_is_the_frozen_dataclass_default():
    assert engine().config == AsianRangeConfig()
    assert engine().config.asian_start_hour_utc == 1.0
