"""Tests for the breaker-block detector.

Synthetic candles only - no fixtures, no DB, no network. Every Config threshold is
asserted to actually gate behaviour, not merely exist.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.breaker_blocks import (
    DEFAULT_BREAKER_BLOCK_CONFIG,
    BreakerBlockConfig,
    BreakerBlockEngine,
    BreakerBlockEvent,
    BreakerBlockState,
    find_zone_indices,
)

HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())


def bar(hour, high, low, close=None, open_=None, day=0):
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = low if open_ is None else open_
    close = open_ if close is None else close
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def flat(hour, price, day=0):
    """A zero-range bar: never a block, and it never overlaps anything."""
    return bar(hour, price, price, close=price, open_=price, day=day)


def demand(day=0):
    """A 14-pip DOWN block at 01:00 followed by a 23-pip up impulse."""
    return [
        flat(0, 1.0848, day=day),
        bar(1, 1.0852, 1.0838, close=1.0840, open_=1.0850, day=day),
        bar(2, 1.0875, 1.0838, close=1.0870, open_=1.0850, day=day),
    ]


def supply(day=0):
    """A 14-pip UP block at 01:00 followed by a 23-pip down impulse."""
    return [
        flat(0, 1.0848, day=day),
        bar(1, 1.0850, 1.0836, close=1.0848, open_=1.0838, day=day),
        bar(2, 1.0850, 1.0813, close=1.0818, open_=1.0846, day=day),
    ]


# 03:00 closes below the demand block's low by 8 ticks -> breach
BREACH_DOWN = bar(3, 1.0838, 1.0828, close=1.0830, open_=1.0836)
# 04:00 trades back inside the block -> the breaker retest
RETEST = bar(4, 1.0846, 1.0836, close=1.0844, open_=1.0838)
# 03:00 closes above the supply block's high by 4 ticks -> breach
BREACH_UP = bar(3, 1.0862, 1.0850, close=1.0855, open_=1.0852)


def engine(**overrides):
    return BreakerBlockEngine(BreakerBlockConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


# ── pure helper ─────────────────────────────────────────────────────────────

def test_find_zone_indices_picks_the_most_recent_qualifying_block():
    assert find_zone_indices(demand(), DEFAULT_BREAKER_BLOCK_CONFIG) == 1
    assert find_zone_indices(supply(), DEFAULT_BREAKER_BLOCK_CONFIG) == 1
    assert find_zone_indices([flat(0, 1.08), flat(1, 1.08)], DEFAULT_BREAKER_BLOCK_CONFIG) is None


# ── happy path ──────────────────────────────────────────────────────────────

def test_fresh_demand_block_is_reported():
    eng = engine()
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["direction"] == "BULLISH"
    assert rec["role"] == "DEMAND"
    assert rec["state"] == BreakerBlockState.FRESH.value
    assert rec["block_high"] == approx(1.0852)
    assert rec["block_low"] == approx(1.0838)
    assert rec["block_span_pips"] == approx(14.0)
    assert rec["displacement_pips"] == approx(23.0)
    assert rec["age_bars"] == 1
    assert rec["breached"] is False
    assert rec["retested"] is False


def test_fresh_supply_block_is_reported():
    eng = engine()
    eng.analyze(supply(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["direction"] == "BEARISH"
    assert rec["role"] == "SUPPLY"
    assert rec["block_high"] == approx(1.0850)
    assert rec["block_low"] == approx(1.0836)


def test_close_through_flips_a_demand_block_into_supply():
    eng = engine()
    eng.analyze(demand() + [BREACH_DOWN], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["breached"] is True
    assert rec["role"] == "SUPPLY"
    assert rec["state"] == BreakerBlockState.BREACHED.value
    assert rec["breach_close"] == approx(1.0830)
    assert rec["breach_bars_after_block"] == 2


def test_close_through_flips_a_supply_block_into_demand():
    eng = engine()
    eng.analyze(supply() + [BREACH_UP], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["breached"] is True
    assert rec["role"] == "DEMAND"
    assert rec["state"] == BreakerBlockState.BREACHED.value


def test_retest_after_the_breach_marks_the_role_flip():
    eng = engine()
    eng.analyze(demand() + [BREACH_DOWN, RETEST], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["retested"] is True
    assert rec["state"] == BreakerBlockState.ROLE_FLIPPED.value


# ── Config thresholds actually gate behaviour ───────────────────────────────

def weak_impulse():
    """A 14-pip DOWN block whose impulse only travels 5 pips."""
    return [
        flat(0, 1.0848),
        bar(1, 1.0852, 1.0838, close=1.0840, open_=1.0850),
        bar(2, 1.0857, 1.0840, close=1.0855, open_=1.0854),
    ]


def test_require_displacement_gate():
    assert engine().analyze(weak_impulse(), tf=900, symbol="EURUSD") == []
    found = engine(require_displacement=False)
    found.analyze(weak_impulse(), tf=900, symbol="EURUSD")
    assert last(found)["block_span_pips"] == approx(14.0)


def test_displacement_mult_gate():
    assert engine(displacement_mult=0.1).analyze(weak_impulse(), tf=900, symbol="EURUSD") != []
    # 0.1 x a 14-pip block = 1.4 pips, so the 5-pip impulse now clears the bar
    eng = engine(displacement_mult=0.1)
    eng.analyze(weak_impulse(), tf=900, symbol="EURUSD")
    assert last(eng)["displacement_pips"] == approx(5.0)


def test_close_through_buffer_ticks_gate():
    # closes exactly one tick (0.0001) below the block low
    marginal = bar(3, 1.0838, 1.0830, close=1.0837, open_=1.0836)
    strict = engine()
    strict.analyze(demand() + [marginal], tf=900, symbol="EURUSD")
    assert last(strict)["breached"] is False
    assert last(strict)["state"] == BreakerBlockState.FRESH.value

    loose = engine(close_through_buffer_ticks=0.0)
    loose.analyze(demand() + [marginal], tf=900, symbol="EURUSD")
    assert last(loose)["breached"] is True


def test_max_bars_to_breach_gate():
    eng = engine(max_bars_to_breach=1)
    eng.analyze(demand() + [BREACH_DOWN], tf=900, symbol="EURUSD")
    assert last(eng)["breached"] is False
    assert last(eng)["state"] == BreakerBlockState.FRESH.value

    default = engine()
    default.analyze(demand() + [BREACH_DOWN], tf=900, symbol="EURUSD")
    assert last(default)["breached"] is True


def test_zone_expiry_bars_gate():
    eng = engine(zone_expiry_bars=0)
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    assert last(eng)["state"] == BreakerBlockState.EXPIRED.value

    fresh = engine()
    fresh.analyze(demand(), tf=900, symbol="EURUSD")
    assert last(fresh)["state"] == BreakerBlockState.FRESH.value


def test_impulse_bars_gate_widens_the_leg():
    assert engine(impulse_bars=2).analyze(demand(), tf=900, symbol="EURUSD") == []
    eng = engine(impulse_bars=2)
    eng.analyze(demand() + [flat(3, 1.0845)], tf=900, symbol="EURUSD")
    assert last(eng)["block_span_pips"] == approx(14.0)


# ── no-signal paths and boundaries ──────────────────────────────────────────

def test_no_zone_returns_empty():
    eng = engine()
    assert eng.analyze([flat(0, 1.08), flat(1, 1.08), flat(2, 1.08)], tf=900, symbol="EURUSD") == []


def test_single_candle_and_malformed_input():
    eng = engine()
    assert eng.analyze([flat(0, 1.08)], tf=900, symbol="EURUSD") == []
    assert eng.analyze([None, {}], tf=900, symbol="EURUSD") == []


# ── lifecycle events ────────────────────────────────────────────────────────

def test_events_fire_once_per_lifecycle_step():
    eng = engine()
    first = eng.analyze(demand(), tf=900, symbol="EURUSD")
    assert [e["event"] for e in first] == [BreakerBlockEvent.ZONE_FOUND.value]
    assert eng.analyze(demand(), tf=900, symbol="EURUSD") == []

    breached = eng.analyze(demand() + [BREACH_DOWN], tf=900, symbol="EURUSD")
    assert [e["event"] for e in breached] == [BreakerBlockEvent.BREACHED.value]
    assert eng.analyze(demand() + [BREACH_DOWN], tf=900, symbol="EURUSD") == []

    flipped = eng.analyze(demand() + [BREACH_DOWN, RETEST], tf=900, symbol="EURUSD")
    assert [e["event"] for e in flipped] == [BreakerBlockEvent.ROLE_FLIPPED.value]
    assert eng.analyze(demand() + [BREACH_DOWN, RETEST], tf=900, symbol="EURUSD") == []


def test_first_look_on_an_already_flipped_zone_emits_the_whole_path():
    eng = engine()
    events = eng.analyze(demand() + [BREACH_DOWN, RETEST], tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [
        BreakerBlockEvent.ZONE_FOUND.value,
        BreakerBlockEvent.BREACHED.value,
        BreakerBlockEvent.ROLE_FLIPPED.value,
    ]


def test_event_payload_carries_the_record_without_created_at():
    eng = engine()
    payload = eng.analyze(demand(), tf=900, symbol="EURUSD")[0]
    assert payload["event"] == "BB_ZONE_FOUND"
    assert payload["timestamp"] == MONDAY + 2 * HOUR
    assert payload["block_high"] == approx(1.0852)
    assert "created_at" not in payload


def test_a_new_block_re_emits_zone_found():
    eng = engine()
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    events = eng.analyze(demand() + demand(day=1), tf=900, symbol="EURUSD")
    assert BreakerBlockEvent.ZONE_FOUND.value in [e["event"] for e in events]
    assert last(eng)["block_time"] == MONDAY + DAY + 1 * HOUR


# ── engine hygiene ──────────────────────────────────────────────────────────

def test_analyze_is_deterministic():
    candles = demand() + [BREACH_DOWN, RETEST]
    a = engine()
    b = engine()
    a.analyze(candles, tf=900, symbol="EURUSD")
    b.analyze(candles, tf=900, symbol="EURUSD")
    assert a.get_all() == b.get_all()
    assert a.get_events() == b.get_events()


def test_state_is_isolated_per_symbol():
    eng = engine()
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    eng.analyze(supply(), tf=900, symbol="GBPUSD")
    assert len(eng.get_all()) == 2


def test_reset_clears_state_and_events():
    eng = engine()
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    assert eng.get_events() != []
    eng.reset()
    assert eng.get_all() == []
    assert eng.get_active() == []
    assert eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_BREAKER_BLOCK_CONFIG.require_displacement is True
    assert DEFAULT_BREAKER_BLOCK_CONFIG.displacement_mult == 1.5
    assert DEFAULT_BREAKER_BLOCK_CONFIG.pip_size == 0.0001
    assert engine().config == BreakerBlockConfig()
