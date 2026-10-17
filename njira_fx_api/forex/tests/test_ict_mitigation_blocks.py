"""Tests for the mitigation-block detector.

Synthetic candles only - no fixtures, no DB, no network. Every Config threshold is
asserted to actually gate behaviour, not merely exist.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.mitigation_blocks import (
    DEFAULT_MITIGATION_BLOCK_CONFIG,
    MitigationBlockConfig,
    MitigationBlockEngine,
    MitigationBlockEvent,
    MitigationBlockState,
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


# price returns from above and dips into the demand block, but closes inside it
MITIGATES_DEMAND = bar(3, 1.0860, 1.0848, close=1.0855, open_=1.0858)
# price returns from below and wicks into the supply block, but closes inside it
MITIGATES_SUPPLY = bar(3, 1.0844, 1.0830, close=1.0835, open_=1.0832)
# a close beyond the far edge: the block did not hold
INVALIDATES_DEMAND = bar(3, 1.0840, 1.0828, close=1.0830, open_=1.0836)
INVALIDATES_SUPPLY = bar(3, 1.0862, 1.0852, close=1.0856, open_=1.0854)


def engine(**overrides):
    return MitigationBlockEngine(MitigationBlockConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


# ── pure helper ─────────────────────────────────────────────────────────────

def test_find_zone_indices_picks_the_most_recent_qualifying_block():
    assert find_zone_indices(demand(), DEFAULT_MITIGATION_BLOCK_CONFIG) == 1
    assert find_zone_indices(supply(), DEFAULT_MITIGATION_BLOCK_CONFIG) == 1
    assert find_zone_indices([flat(0, 1.08), flat(1, 1.08)], DEFAULT_MITIGATION_BLOCK_CONFIG) is None


# ── a fresh block is just a zone ────────────────────────────────────────────

def test_fresh_demand_block_is_a_zone():
    eng = engine()
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["direction"] == "BULLISH"
    assert rec["near_edge"] == approx(1.0852)
    assert rec["far_edge"] == approx(1.0838)
    assert rec["block_span_pips"] == approx(14.0)
    assert rec["displacement_pips"] == approx(23.0)
    assert rec["age_bars"] == 1
    assert rec["mitigated"] is False
    assert rec["invalidated"] is False
    assert rec["state"] == MitigationBlockState.FRESH.value


def test_fresh_supply_block_is_a_zone():
    eng = engine()
    eng.analyze(supply(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["direction"] == "BEARISH"
    assert rec["near_edge"] == approx(1.0836)
    assert rec["far_edge"] == approx(1.0850)


# ── mitigation: price came back and the block held ──────────────────────────

def test_demand_block_is_mitigated_when_price_returns():
    eng = engine()
    eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["mitigated"] is True
    assert rec["invalidated"] is False
    assert rec["state"] == MitigationBlockState.MITIGATED.value
    assert rec["mitigation_time"] == MONDAY + 3 * HOUR
    assert rec["mitigation_price"] == approx(1.0855)


def test_supply_block_is_mitigated_when_price_returns():
    eng = engine()
    eng.analyze(supply() + [MITIGATES_SUPPLY], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["mitigated"] is True
    assert rec["state"] == MitigationBlockState.MITIGATED.value


# ── invalidation: the block failed ──────────────────────────────────────────

def test_close_through_invalidates_a_demand_block():
    eng = engine()
    eng.analyze(demand() + [INVALIDATES_DEMAND], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["invalidated"] is True
    assert rec["mitigated"] is False
    assert rec["state"] == MitigationBlockState.INVALIDATED.value


def test_close_through_invalidates_a_supply_block():
    eng = engine()
    eng.analyze(supply() + [INVALIDATES_SUPPLY], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["invalidated"] is True
    assert rec["state"] == MitigationBlockState.INVALIDATED.value


def test_a_bar_that_touches_and_closes_through_invalidates():
    # opened back inside the zone but settled beyond the far edge: the block failed
    both = bar(3, 1.0846, 1.0828, close=1.0830, open_=1.0844)
    eng = engine()
    eng.analyze(demand() + [both], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["invalidated"] is True
    assert rec["mitigated"] is False
    assert rec["state"] == MitigationBlockState.INVALIDATED.value


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
    # 0.1 x a 14-pip block = 1.4 pips, so the 5-pip impulse now clears the bar
    eng = engine(displacement_mult=0.1)
    eng.analyze(weak_impulse(), tf=900, symbol="EURUSD")
    assert last(eng)["displacement_pips"] == approx(5.0)


def test_impulse_bars_gate_widens_the_leg():
    assert engine(impulse_bars=2).analyze(demand(), tf=900, symbol="EURUSD") == []
    eng = engine(impulse_bars=2)
    eng.analyze(demand() + [flat(3, 1.0845)], tf=900, symbol="EURUSD")
    assert last(eng)["block_span_pips"] == approx(14.0)


def test_mitigation_touch_tolerance_ticks_gate():
    # stops exactly one tick (0.0001) above the near edge
    near_miss = bar(3, 1.0860, 1.0853, close=1.0858, open_=1.0856)
    strict = engine(mitigation_touch_tolerance_ticks=0.0)
    strict.analyze(demand() + [near_miss], tf=900, symbol="EURUSD")
    assert last(strict)["mitigated"] is False

    loose = engine()
    loose.analyze(demand() + [near_miss], tf=900, symbol="EURUSD")
    assert last(loose)["mitigated"] is True


def test_max_age_bars_gate():
    eng = engine(max_age_bars=1)
    eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    # the touch bar sits 2 bars after the block, so the scan never reaches it
    assert last(eng)["mitigated"] is False
    assert last(eng)["state"] == MitigationBlockState.EXPIRED.value

    default = engine()
    default.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    assert last(default)["mitigated"] is True


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
    assert [e["event"] for e in first] == [MitigationBlockEvent.ZONE_FOUND.value]
    assert eng.analyze(demand(), tf=900, symbol="EURUSD") == []

    hit = eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    assert [e["event"] for e in hit] == [MitigationBlockEvent.MITIGATED.value]
    assert eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD") == []


def test_invalidation_event_fires_after_a_mitigation():
    eng = engine()
    eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    later_through = bar(4, 1.0840, 1.0828, close=1.0830, open_=1.0836)
    events = eng.analyze(
        demand() + [MITIGATES_DEMAND, later_through],
        tf=900,
        symbol="EURUSD",
    )
    assert [e["event"] for e in events] == [MitigationBlockEvent.INVALIDATED.value]


def test_first_look_on_an_already_mitigated_zone_emits_the_whole_path():
    eng = engine()
    events = eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [
        MitigationBlockEvent.ZONE_FOUND.value,
        MitigationBlockEvent.MITIGATED.value,
    ]


def test_first_look_on_an_already_invalidated_zone_emits_the_whole_path():
    eng = engine()
    events = eng.analyze(demand() + [INVALIDATES_DEMAND], tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [
        MitigationBlockEvent.ZONE_FOUND.value,
        MitigationBlockEvent.INVALIDATED.value,
    ]


def test_event_payload_carries_the_record_without_created_at():
    eng = engine()
    payload = eng.analyze(demand(), tf=900, symbol="EURUSD")[0]
    assert payload["event"] == "MB_ZONE_FOUND"
    assert payload["timestamp"] == MONDAY + 2 * HOUR
    assert payload["near_edge"] == approx(1.0852)
    assert "created_at" not in payload


def test_a_new_block_re_emits_zone_found():
    eng = engine()
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    events = eng.analyze(demand() + demand(day=1), tf=900, symbol="EURUSD")
    assert MitigationBlockEvent.ZONE_FOUND.value in [e["event"] for e in events]
    assert last(eng)["block_time"] == MONDAY + DAY + 1 * HOUR


# ── engine hygiene ──────────────────────────────────────────────────────────

def test_analyze_is_deterministic():
    candles = demand() + [MITIGATES_DEMAND]
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
    assert DEFAULT_MITIGATION_BLOCK_CONFIG.require_displacement is True
    assert DEFAULT_MITIGATION_BLOCK_CONFIG.mitigation_touch_tolerance_ticks == 1.0
    assert DEFAULT_MITIGATION_BLOCK_CONFIG.pip_size == 0.0001
    assert engine().config == MitigationBlockConfig()


def weak_demand(day=0):
    """The 14-pip down block again, but the up impulse is only 10 pips."""
    return [
        flat(0, 1.0848, day=day),
        bar(1, 1.0852, 1.0838, close=1.0840, open_=1.0850, day=day),
        bar(2, 1.0862, 1.0838, close=1.0860, open_=1.0840, day=day),
    ]


def fronted(price, day=0):
    """demand() with a return to the block, and the pre-block low moved to price."""
    return [
        flat(0, price, day=day),
        bar(1, 1.0852, 1.0838, close=1.0840, open_=1.0850, day=day),
        bar(2, 1.0875, 1.0838, close=1.0870, open_=1.0850, day=day),
        MITIGATES_DEMAND,
    ]


def deep_mitigation(day=0):
    """demand() with a return that buries almost the whole span inside the block."""
    return demand(day=day) + [bar(3, 1.0860, 1.0840, close=1.0855, open_=1.0858, day=day)]


NEAR_MISS_DEMAND = bar(3, 1.0840, 1.0830, close=1.0837, open_=1.0838)


def test_enhancement_config_defaults_are_inert():
    cfg = MitigationBlockConfig()
    assert cfg.fallback_ignore_displacement is False
    assert cfg.invalidation_tolerance_ticks == 0.0
    assert cfg.sweep_lookback_bars == 10
    assert cfg == DEFAULT_MITIGATION_BLOCK_CONFIG


def test_find_zone_indices_rejects_a_weak_impulse_by_default():
    assert find_zone_indices(weak_demand(), DEFAULT_MITIGATION_BLOCK_CONFIG) is None


def test_find_zone_indices_accepts_a_weak_impulse_once_the_gate_is_dropped():
    assert find_zone_indices(weak_demand(), MitigationBlockConfig(require_displacement=False)) == 1


def test_weak_impulse_yields_no_record_by_default():
    assert engine().analyze(weak_demand(), tf=900, symbol="EURUSD") == []
    assert engine().get_all() == []


def test_fallback_reopens_the_weak_impulse_when_enabled():
    eng = engine(fallback_ignore_displacement=True)
    eng.analyze(weak_demand(), tf=900, symbol="EURUSD")
    assert len(eng.get_all()) == 1


def test_fallback_record_is_flagged_and_still_fresh():
    eng = engine(fallback_ignore_displacement=True)
    eng.analyze(weak_demand(), tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["used_fallback"] is True
    assert record["state"] == "FRESH"
    assert record["mitigated"] is False
    assert record["invalidated"] is False
    assert record["direction"] == "BULLISH"


def test_fallback_flag_stays_false_on_a_qualifying_block():
    eng = engine(fallback_ignore_displacement=True)
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    assert last(eng)["used_fallback"] is False


def test_fallback_flag_can_be_switched_off_explicitly():
    eng = engine(fallback_ignore_displacement=True, require_displacement=False)
    eng.analyze(weak_demand(), tf=900, symbol="EURUSD")
    assert last(eng)["used_fallback"] is False


def test_strength_never_leaves_the_unit_interval():
    fixtures = [
        demand() + [MITIGATES_DEMAND],
        supply() + [MITIGATES_SUPPLY],
        weak_demand(),
        fronted(1.0800),
        fronted(1.0848),
        deep_mitigation(),
    ]
    for candles in fixtures:
        eng = engine(fallback_ignore_displacement=True)
        eng.analyze(candles, tf=900, symbol="EURUSD")
        record = last(eng)
        assert record != {}, candles
        assert 0.0 <= record["strength"] <= 1.0


def test_swept_before_mitigation_is_false_when_the_prior_low_holds():
    eng = engine()
    eng.analyze(fronted(1.0800), tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["state"] == "MITIGATED"
    assert record["swept_before_mitigation"] is False


def test_swept_before_mitigation_is_true_when_the_prior_low_is_taken():
    eng = engine()
    eng.analyze(fronted(1.0848), tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["state"] == "MITIGATED"
    assert record["swept_before_mitigation"] is True


def test_sweep_bonus_is_worth_twenty_points_of_strength():
    clean = engine()
    clean.analyze(fronted(1.0800), tf=900, symbol="EURUSD")
    swept = engine()
    swept.analyze(fronted(1.0848), tf=900, symbol="EURUSD")
    assert last(swept)["strength"] - last(clean)["strength"] == approx(0.20, abs=1e-4)


def test_swept_before_mitigation_is_false_when_the_block_is_never_revisited():
    eng = engine()
    eng.analyze(fronted(1.0848)[:3], tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["state"] == "FRESH"
    assert record["swept_before_mitigation"] is False


def test_a_close_one_tick_beyond_the_far_edge_invalidates_at_zero_tolerance():
    eng = engine()
    eng.analyze(demand() + [NEAR_MISS_DEMAND], tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["invalidated"] is True
    assert record["state"] == "INVALIDATED"


def test_widening_the_tolerance_turns_the_near_miss_into_a_mitigation():
    eng = engine(invalidation_tolerance_ticks=2.0)
    eng.analyze(demand() + [NEAR_MISS_DEMAND], tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["invalidated"] is False
    assert record["mitigated"] is True
    assert record["state"] == "MITIGATED"


def test_two_ticks_of_tolerance_is_enough_to_spare_the_near_miss():
    strict = engine()
    strict.analyze(demand() + [NEAR_MISS_DEMAND], tf=900, symbol="EURUSD")
    loose = engine(invalidation_tolerance_ticks=2.0)
    loose.analyze(demand() + [NEAR_MISS_DEMAND], tf=900, symbol="EURUSD")
    assert last(strict)["state"] == "INVALIDATED"
    assert last(loose)["state"] == "MITIGATED"


def test_the_tolerance_knob_cannot_touch_an_unrevisited_block():
    for tolerance in (0.0, 2.0, 20.0):
        eng = engine(invalidation_tolerance_ticks=tolerance)
        eng.analyze(demand(), tf=900, symbol="EURUSD")
        assert last(eng)["state"] == "FRESH"


def test_deeper_penetration_scores_lower():
    shallow = engine()
    shallow.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    deep = engine()
    deep.analyze(deep_mitigation(), tf=900, symbol="EURUSD")
    assert last(shallow)["state"] == "MITIGATED"
    assert last(deep)["state"] == "MITIGATED"
    assert last(deep)["strength"] < last(shallow)["strength"]


def test_quality_fields_are_serialised_and_created_at_is_not():
    eng = engine()
    eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    record = last(eng)
    assert "used_fallback" in record
    assert "swept_before_mitigation" in record
    assert "strength" in record
    assert "created_at" not in record


def test_quality_fields_do_not_disturb_the_legacy_contract():
    eng = engine()
    eng.analyze(demand(), tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["direction"] == "BULLISH"
    assert record["near_edge"] == approx(1.0852)
    assert record["far_edge"] == approx(1.0838)
    assert record["state"] == "FRESH"
    assert record["block_span_pips"] == approx(14.0)


def test_supply_side_still_reports_its_own_edges():
    eng = engine()
    eng.analyze(supply(), tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["direction"] == "BEARISH"
    assert record["near_edge"] == approx(1.0836)
    assert record["far_edge"] == approx(1.0850)


def test_sweep_lookback_shrinking_still_reports_a_sweep():
    eng = engine(sweep_lookback_bars=1)
    eng.analyze(fronted(1.0848), tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["state"] == "MITIGATED"
    assert record["swept_before_mitigation"] is True


def test_sweep_lookback_wider_than_the_history_is_harmless():
    eng = engine(sweep_lookback_bars=3)
    eng.analyze(fronted(1.0848), tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["state"] == "MITIGATED"
    assert record["swept_before_mitigation"] is True


def test_sweep_flag_never_fires_without_a_mitigation():
    eng = engine()
    eng.analyze(supply() + [INVALIDATES_SUPPLY], tf=900, symbol="EURUSD")
    record = last(eng)
    assert record["state"] == "INVALIDATED"
    assert record["swept_before_mitigation"] is False


def test_events_still_use_the_legacy_names_after_the_enhancement():
    eng = engine()
    events = eng.analyze(demand() + [MITIGATES_DEMAND], tf=900, symbol="EURUSD")
    names = [event["event"] for event in events]
    assert MitigationBlockEvent.ZONE_FOUND.value in names
    assert MitigationBlockEvent.MITIGATED.value in names


def test_invalidation_event_still_fires_on_the_strict_read():
    eng = engine()
    events = eng.analyze(demand() + [INVALIDATES_DEMAND], tf=900, symbol="EURUSD")
    names = [event["event"] for event in events]
    assert MitigationBlockEvent.INVALIDATED.value in names
    assert MitigationBlockEvent.MITIGATED.value not in names
