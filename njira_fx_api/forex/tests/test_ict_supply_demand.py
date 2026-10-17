"""Tests for the supply & demand zone detector.

Synthetic candles only - no fixtures, no DB, no network. Every Config knob is
asserted to actually change behaviour, not merely exist.

ATR note: the fixtures below mostly run the default ``atr_period=14``, so on a
7-bar list the ATR window is ALL of it and the departure bar's own true range is
counted - hand-derived per fixture rather than assumed. The one test that pins a
specific ATR uses ``atr_period=1`` (a single true range) so arithmetic is exact.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.supply_demand import (
    DEFAULT_SUPPLY_DEMAND_CONFIG,
    SupplyDemandConfig,
    SupplyDemandEngine,
    SupplyDemandEvent,
    SupplyDemandSide,
    SupplyDemandState,
    compute_atr,
    find_zone,
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
    """A zero-range bar: never a departure, never a base wild."""
    return bar(hour, price, price, close=price, open_=price, day=day)


def demand_base(day=0):
    """Quiet base at 01:00-03:00, a 46-pip up departure at 04:00, then drift back.

    The base is bars 1-3 and the departure is bar 4, so the zone is bar 3 (the
    last DOWN candle in the base): high 1.0801, low 1.0797.
    """
    return [
        flat(0, 1.0800, day=day),
        bar(1, 1.0802, 1.0798, close=1.0801, open_=1.0799, day=day),
        bar(2, 1.0803, 1.0799, close=1.0799, open_=1.0802, day=day),
        bar(3, 1.0801, 1.0797, close=1.0798, open_=1.0800, day=day),
        bar(4, 1.0850, 1.0798, close=1.0849, open_=1.0800, day=day),
        bar(5, 1.0840, 1.0820, close=1.0822, open_=1.0845, day=day),
        bar(6, 1.0825, 1.0805, close=1.0807, open_=1.0820, day=day),
    ]


def supply_base(day=0):
    """Mirror of ``demand_base``: a 46-pip down departure.

    The zone is bar 3 (the last UP candle in the base): high 1.0853, low 1.0849.
    """
    return [
        flat(0, 1.0850, day=day),
        bar(1, 1.0852, 1.0848, close=1.0849, open_=1.0851, day=day),
        bar(2, 1.0851, 1.0847, close=1.0851, open_=1.0848, day=day),
        bar(3, 1.0853, 1.0849, close=1.0852, open_=1.0850, day=day),
        bar(4, 1.0852, 1.0800, close=1.0801, open_=1.0850, day=day),
        bar(5, 1.0830, 1.0810, close=1.0828, open_=1.0805, day=day),
        bar(6, 1.0845, 1.0825, close=1.0843, open_=1.0828, day=day),
    ]


# price returns into the demand zone and closes back inside it
TOUCHES_DEMAND = bar(7, 1.0810, 1.0799, close=1.0800, open_=1.0807)
# price returns into the supply zone and closes back inside it
TOUCHES_SUPPLY = bar(7, 1.0851, 1.0840, close=1.0850, open_=1.0843)
# a close beyond the far edge of each zone
BREAKS_DEMAND = bar(7, 1.0804, 1.0785, close=1.0788, open_=1.0800)
BREAKS_SUPPLY = bar(7, 1.0866, 1.0846, close=1.0863, open_=1.0850)


def engine(**overrides):
    return SupplyDemandEngine(SupplyDemandConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


# ── pure helpers ────────────────────────────────────────────────────────────

def test_compute_atr_averages_true_ranges():
    candles = [flat(0, 1.0800), bar(1, 1.0810, 1.0800, close=1.0805, open_=1.0800)]
    assert compute_atr(candles, 2) == approx(0.0010)


def test_compute_atr_guards():
    assert compute_atr([], 2) == 0.0
    assert compute_atr([flat(0, 1.08)], 2) == 0.0
    assert compute_atr([flat(0, 1.08), flat(1, 1.08)], 0) == 0.0
    # two zero-range bars produce a zero true range, not a division error
    assert compute_atr([flat(0, 1.08), flat(1, 1.08)], 2) == 0.0


def test_find_zone_needs_a_real_departure():
    assert find_zone([flat(0, 1.08), flat(1, 1.08)], DEFAULT_SUPPLY_DEMAND_CONFIG, 0.0) is None
    # a base with no departure leg is not a zone
    quiet = [flat(0, 1.0800), flat(1, 1.0800), flat(2, 1.0800)]
    assert find_zone(quiet, DEFAULT_SUPPLY_DEMAND_CONFIG, 0.0005) is None


def test_find_zone_locates_the_departure():
    found = find_zone(demand_base(), DEFAULT_SUPPLY_DEMAND_CONFIG, 0.00245)
    assert found == (SupplyDemandSide.DEMAND, 1, 3, 4)
    found = find_zone(supply_base(), DEFAULT_SUPPLY_DEMAND_CONFIG, 0.00245)
    assert found == (SupplyDemandSide.SUPPLY, 1, 3, 4)


# ── fresh zones ─────────────────────────────────────────────────────────────

def test_fresh_demand_zone():
    eng = engine()
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["side"] == SupplyDemandSide.DEMAND.value
    assert rec["zone_mode"] == "last_opposing_candle"
    assert rec["zone_high"] == approx(1.0801)
    assert rec["zone_low"] == approx(1.0797)
    assert rec["zone_span_pips"] == approx(4.0)
    assert rec["base_bars"] == 3
    assert rec["departure_move_pips"] == approx(46.0)
    assert rec["atr_pips"] == approx(18.83)
    assert rec["base_start_time"] == MONDAY + 1 * HOUR
    assert rec["base_end_time"] == MONDAY + 3 * HOUR
    assert rec["departure_time"] == MONDAY + 4 * HOUR
    assert rec["touched"] is False
    assert rec["broken"] is False
    assert rec["state"] == SupplyDemandState.FRESH.value


def test_fresh_supply_zone():
    eng = engine()
    eng.analyze(supply_base(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["side"] == SupplyDemandSide.SUPPLY.value
    assert rec["zone_high"] == approx(1.0853)
    assert rec["zone_low"] == approx(1.0849)
    assert rec["state"] == SupplyDemandState.FRESH.value


# ── touch: price came back and the zone held ────────────────────────────────

def test_demand_zone_is_touched():
    eng = engine()
    eng.analyze(demand_base() + [TOUCHES_DEMAND], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["touched"] is True
    assert rec["broken"] is False
    assert rec["touch_time"] == MONDAY + 7 * HOUR
    assert rec["state"] == SupplyDemandState.TOUCHED.value


def test_supply_zone_is_touched():
    eng = engine()
    eng.analyze(supply_base() + [TOUCHES_SUPPLY], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["touched"] is True
    assert rec["broken"] is False
    assert rec["state"] == SupplyDemandState.TOUCHED.value


# ── break: the zone failed ──────────────────────────────────────────────────

def test_demand_zone_is_broken():
    eng = engine()
    eng.analyze(demand_base() + [BREAKS_DEMAND], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["broken"] is True
    assert rec["break_time"] == MONDAY + 7 * HOUR
    assert rec["state"] == SupplyDemandState.BROKEN.value


def test_supply_zone_is_broken():
    eng = engine()
    eng.analyze(supply_base() + [BREAKS_SUPPLY], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["broken"] is True
    assert rec["state"] == SupplyDemandState.BROKEN.value


def test_a_bar_that_touches_and_closes_through_is_broken_not_touched():
    # the close-through check runs first, so a failing close can never also be a clean touch
    both = bar(7, 1.0810, 1.0785, close=1.0788, open_=1.0807)
    eng = engine()
    eng.analyze(demand_base() + [both], tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["broken"] is True
    assert rec["touched"] is False
    assert rec["state"] == SupplyDemandState.BROKEN.value


# ── Config knobs actually change behaviour ──────────────────────────────────

def test_zone_mode_base_extent_widens_the_zone():
    # "last_opposing_candle" reports bar 3 alone (1.0801/1.0797);
    # "base_extent" reports the whole base (1.0803/1.0797)
    eng = engine(zone_mode="base_extent")
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["zone_mode"] == "base_extent"
    assert rec["zone_high"] == approx(1.0803)
    assert rec["zone_low"] == approx(1.0797)
    assert rec["zone_span_pips"] == approx(6.0)


def test_base_max_bars_gate_shrinks_the_base():
    eng = engine(base_max_bars=1)
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["base_bars"] == 1
    # bar 3 is the last opposing candle either way, so the zone never moves
    assert rec["zone_high"] == approx(1.0801)
    assert rec["zone_low"] == approx(1.0797)


def test_impulse_min_atr_mult_gate():
    # 10 x the ATR is far above the 46-pip departure: nothing qualifies
    assert engine(impulse_min_atr_mult=10).analyze(
        demand_base(), tf=900, symbol="EURUSD"
    ) == []


def test_atr_period_changes_the_atr():
    # atr_period=1 leaves just the last true range (bars 5-6) = 20 pips
    eng = engine(atr_period=1)
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["atr_pips"] == approx(20.0)
    assert rec["side"] == SupplyDemandSide.DEMAND.value


def test_extend_zone_pads_both_edges():
    eng = engine(extend_zone=True)
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    rec = last(eng)
    assert rec["zone_high"] == approx(1.0802)
    assert rec["zone_low"] == approx(1.0796)
    assert rec["zone_span_pips"] == approx(6.0)
    assert rec["state"] == SupplyDemandState.FRESH.value


# ── lifecycle events ────────────────────────────────────────────────────────

def test_events_fire_once_per_step():
    eng = engine()
    first = eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    assert [e["event"] for e in first] == [SupplyDemandEvent.ZONE_FOUND.value]
    assert eng.analyze(demand_base(), tf=900, symbol="EURUSD") == []

    hit = eng.analyze(demand_base() + [TOUCHES_DEMAND], tf=900, symbol="EURUSD")
    assert [e["event"] for e in hit] == [SupplyDemandEvent.TOUCHED.value]
    assert eng.analyze(demand_base() + [TOUCHES_DEMAND], tf=900, symbol="EURUSD") == []


def test_first_look_on_touched_emits_full_path():
    eng = engine()
    events = eng.analyze(demand_base() + [TOUCHES_DEMAND], tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [
        SupplyDemandEvent.ZONE_FOUND.value,
        SupplyDemandEvent.TOUCHED.value,
    ]


def test_first_look_on_broken_emits_full_path():
    eng = engine()
    events = eng.analyze(demand_base() + [BREAKS_DEMAND], tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [
        SupplyDemandEvent.ZONE_FOUND.value,
        SupplyDemandEvent.BROKEN.value,
    ]


def test_event_payload_carries_record_minus_created_at():
    eng = engine()
    payload = eng.analyze(demand_base(), tf=900, symbol="EURUSD")[0]
    assert payload["event"] == "SD_ZONE_FOUND"
    # created_at comes from the LAST candle, not the zone bar
    assert payload["timestamp"] == MONDAY + 6 * HOUR
    assert payload["zone_high"] == approx(1.0801)
    assert "created_at" not in payload


def test_new_departure_re_emits_zone_found():
    eng = engine()
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    events = eng.analyze(demand_base(day=1), tf=900, symbol="EURUSD")
    assert SupplyDemandEvent.ZONE_FOUND.value in [e["event"] for e in events]


# ── engine hygiene ──────────────────────────────────────────────────────────

def test_analyze_is_deterministic():
    candles = demand_base() + [TOUCHES_DEMAND]
    a = engine()
    b = engine()
    a.analyze(candles, tf=900, symbol="EURUSD")
    b.analyze(candles, tf=900, symbol="EURUSD")
    assert a.get_all() == b.get_all()
    assert a.get_events() == b.get_events()


def test_state_isolated_per_symbol():
    eng = engine()
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    eng.analyze(supply_base(), tf=900, symbol="GBPUSD")
    assert len(eng.get_all()) == 2


def test_reset_clears():
    eng = engine()
    eng.analyze(demand_base(), tf=900, symbol="EURUSD")
    assert eng.get_events() != []
    eng.reset()
    assert eng.get_all() == []
    assert eng.get_active() == []
    assert eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_SUPPLY_DEMAND_CONFIG.zone_mode == "last_opposing_candle"
    assert DEFAULT_SUPPLY_DEMAND_CONFIG.base_max_bars == 3
    assert DEFAULT_SUPPLY_DEMAND_CONFIG.impulse_min_atr_mult == 1.5
    assert DEFAULT_SUPPLY_DEMAND_CONFIG.atr_period == 14
    assert DEFAULT_SUPPLY_DEMAND_CONFIG.extend_zone is False
    assert DEFAULT_SUPPLY_DEMAND_CONFIG.pip_size == 0.0001
    assert engine().config == SupplyDemandConfig()


# ── boundaries ──────────────────────────────────────────────────────────────

def test_single_candle_and_malformed_input():
    eng = engine()
    assert eng.analyze([flat(0, 1.08)], tf=900, symbol="EURUSD") == []
    assert eng.analyze([None, {}], tf=900, symbol="EURUSD") == []
    assert eng.analyze([flat(0, 1.08), None], tf=900, symbol="EURUSD") == []


def test_no_zone_returns_empty():
    eng = engine()
    candles = [flat(0, 1.0800), flat(1, 1.0800), flat(2, 1.0800)]
    assert eng.analyze(candles, tf=900, symbol="EURUSD") == []
