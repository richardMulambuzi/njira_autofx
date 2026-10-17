"""Tests for the Power of Three (accumulation / manipulation / distribution) detector.

Synthetic candles only - no fixtures, no DB, no network. Every Config threshold
is asserted to actually gate behaviour, not merely exist.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.power_of_three import (
    DEFAULT_POWER_OF_THREE_CONFIG,
    PowerOfThreeConfig,
    PowerOfThreeEngine,
    PowerOfThreeEvent,
    PowerOfThreeState,
)

HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())


def bar(hour, high, low, close=None, open_=None, day=0):
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = low if open_ is None else open_
    close = open_ if close is None else close
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def accumulation(day=0):
    """Three London bars forming a 12-pip box: high 1.0852, low 1.0840."""
    return [
        bar(7, 1.0850, 1.0840, close=1.0845, day=day),
        bar(8, 1.0852, 1.0842, close=1.0848, day=day),
        bar(9, 1.0851, 1.0841, close=1.0846, day=day),
    ]


def engine(**overrides):
    return PowerOfThreeEngine(PowerOfThreeConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


# ── accumulation ─────────────────────────────────────────────────────────────

def test_accumulation_phase_and_box():
    eng = engine()
    eng.analyze(accumulation(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["phase"] == PowerOfThreeState.ACCUMULATION.value
    assert rec["box_high"] == approx(1.0852)
    assert rec["box_low"] == approx(1.0840)
    assert rec["box_span_pips"] == approx(12.0)
    assert rec["session"] == "london"
    assert rec["bars_in_session"] == 3
    assert rec["manipulation_side"] == ""
    assert rec["distribution_confirmed"] is False


def test_accumulation_event_emitted_once_per_session():
    eng = engine()
    first = eng.analyze(accumulation(), tf=900, symbol="EURUSD")
    assert [e["event"] for e in first] == [PowerOfThreeEvent.ACCUMULATION_SET.value]
    # same session, nothing changed -> quiet
    assert eng.analyze(accumulation(), tf=900, symbol="EURUSD") == []


def test_in_killzone_is_reported_not_gating():
    eng = engine()
    eng.analyze(accumulation(), tf=900, symbol="EURUSD")
    assert last(eng)["in_killzone"] is True

    eng2 = engine()
    outside = [bar(13, 1.0850, 1.0840, close=1.0845) for _ in range(1)]
    outside = [
        bar(13, 1.0850, 1.0840, close=1.0845),
        bar(14, 1.0852, 1.0842, close=1.0848),
        bar(15, 1.0851, 1.0841, close=1.0846),
    ]
    eng2.analyze(outside, tf=900, symbol="EURUSD")
    assert last(eng2)["in_killzone"] is False
    assert last(eng2)["phase"] == PowerOfThreeState.ACCUMULATION.value


# ── manipulation ─────────────────────────────────────────────────────────────

def test_manipulation_up_detected():
    eng = engine()
    eng.analyze(accumulation() + [bar(10, 1.0865, 1.0845, close=1.0860)], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["phase"] == PowerOfThreeState.MANIPULATION.value
    assert rec["manipulation_side"] == "UP"
    assert rec["manipulation_extreme"] == approx(1.0865)
    assert rec["manipulation_pips"] == approx(13.0)


def test_manipulation_down_detected():
    eng = engine()
    eng.analyze(accumulation() + [bar(10, 1.0848, 1.0827, close=1.0832)], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["phase"] == PowerOfThreeState.MANIPULATION.value
    assert rec["manipulation_side"] == "DOWN"
    assert rec["manipulation_extreme"] == approx(1.0827)
    assert rec["manipulation_pips"] == approx(13.0)


def test_manipulation_event_emitted():
    eng = engine()
    events = eng.analyze(
        accumulation() + [bar(10, 1.0865, 1.0845, close=1.0860)], tf=900, symbol="EURUSD"
    )
    assert PowerOfThreeEvent.MANIPULATION.value in [e["event"] for e in events]


def test_distribution_after_return_into_box():
    eng = engine()
    eng.analyze(
        accumulation() + [bar(10, 1.0865, 1.0845, close=1.0860)], tf=900, symbol="EURUSD"
    )
    events = eng.analyze(
        accumulation()
        + [bar(10, 1.0865, 1.0845, close=1.0860), bar(11, 1.0862, 1.0844, close=1.0846)],
        tf=900,
        symbol="EURUSD",
    )
    rec = last(eng)
    assert rec["phase"] == PowerOfThreeState.DISTRIBUTION.value
    assert rec["distribution_confirmed"] is True
    assert rec["distribution_direction"] == "DOWN"
    assert rec["distribution_target"] == approx(1.0840)
    assert PowerOfThreeEvent.DISTRIBUTION.value in [e["event"] for e in events]


def test_close_confirm_gate():
    bars = accumulation() + [
        bar(10, 1.0865, 1.0845, close=1.0860),
        bar(11, 1.0868, 1.0845, close=1.0862),
    ]
    strict = engine()
    strict.analyze(bars, tf=900, symbol="EURUSD")
    assert last(strict)["phase"] == PowerOfThreeState.MANIPULATION.value

    loose = engine(close_confirm=False)
    loose.analyze(bars, tf=900, symbol="EURUSD")
    assert last(loose)["phase"] == PowerOfThreeState.DISTRIBUTION.value


def test_sweep_depth_min_gates_manipulation():
    bars = accumulation() + [bar(10, 1.0865, 1.0845, close=1.0860)]
    eng = engine(sweep_depth_min=20.0)
    eng.analyze(bars, tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["phase"] == PowerOfThreeState.ACCUMULATION.value
    assert rec["manipulation_side"] == ""


def test_accumulation_ratio_gates_manipulation():
    # A 4-pip extend clears sweep_depth_min (3 pips) but not the 7.2-pip ratio limit.
    bars = accumulation() + [bar(10, 1.0856, 1.0845, close=1.0854)]
    eng = engine()
    eng.analyze(bars, tf=900, symbol="EURUSD")
    assert last(eng)["phase"] == PowerOfThreeState.ACCUMULATION.value

    loose = engine(accumulation_range_ratio=0.1)
    loose.analyze(bars, tf=900, symbol="EURUSD")
    assert last(loose)["phase"] == PowerOfThreeState.MANIPULATION.value


def test_min_phase_bars_gate():
    eng = engine(min_phase_bars=4)
    assert eng.analyze(accumulation(), tf=900, symbol="EURUSD") == []


def test_min_range_pips_gate():
    flat = [
        bar(7, 1.0842, 1.0840, close=1.0841),
        bar(8, 1.0842, 1.0840, close=1.0841),
        bar(9, 1.0842, 1.0840, close=1.0841),
    ]
    eng = engine()
    assert eng.analyze(flat, tf=900, symbol="EURUSD") == []


def test_no_session_bars_returns_empty():
    asia = [bar(1, 1.0850, 1.0840, close=1.0845), bar(2, 1.0850, 1.0840, close=1.0845)]
    eng = engine()
    assert eng.analyze(asia, tf=900, symbol="EURUSD") == []


def test_session_config_selects_window():
    eng = engine(session="tokyo")
    assert eng.analyze(accumulation(), tf=900, symbol="EURUSD") == []


def test_single_candle_and_malformed_input():
    eng = engine()
    assert eng.analyze([bar(7, 1.0850, 1.0840)], tf=900, symbol="EURUSD") == []
    assert eng.analyze([None, {}], tf=900, symbol="EURUSD") == []


def test_determinism():
    bars = accumulation() + [
        bar(10, 1.0865, 1.0845, close=1.0860),
        bar(11, 1.0862, 1.0844, close=1.0846),
    ]
    a = engine()
    b = engine()
    a.analyze(bars, tf=900, symbol="EURUSD")
    b.analyze(bars, tf=900, symbol="EURUSD")
    assert a.get_all() == b.get_all()
    assert a.get_events() == b.get_events()


def test_symbol_isolation():
    eng = engine()
    eng.analyze(accumulation(), tf=900, symbol="EURUSD")
    eng.analyze(accumulation(), tf=900, symbol="GBPUSD")
    assert len(eng.get_all()) == 2


def test_reset_clears_state_and_events():
    eng = engine()
    eng.analyze(accumulation(), tf=900, symbol="EURUSD")
    eng.reset()
    assert eng.get_all() == []
    assert eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_POWER_OF_THREE_CONFIG.session == "london"
    assert DEFAULT_POWER_OF_THREE_CONFIG.min_phase_bars == 3
    assert DEFAULT_POWER_OF_THREE_CONFIG.pip_size == 0.0001


def test_to_dict_excludes_created_at():
    eng = engine()
    eng.analyze(accumulation(), tf=900, symbol="EURUSD")
    assert "created_at" not in last(eng)
    assert last(eng)["phase"] == PowerOfThreeState.ACCUMULATION.value


def test_new_day_session_reemits_accumulation_event():
    eng = engine()
    eng.analyze(accumulation(), tf=900, symbol="EURUSD")
    # an overnight bar outside London breaks the run, so the next London stretch
    # is a genuinely new session and the accumulation event fires again
    events = eng.analyze(
        accumulation()
        + [bar(3, 1.0845, 1.0835, close=1.0840, day=1)]
        + accumulation(day=1),
        tf=900,
        symbol="EURUSD",
    )
    assert PowerOfThreeEvent.ACCUMULATION_SET.value in [e["event"] for e in events]
