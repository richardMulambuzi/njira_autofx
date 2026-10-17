"""Tests for the Point of Interest (POI) confluence detector.

Synthetic candles only - no fixtures, no DB, no network. Every Config knob is
asserted to actually change behaviour, not merely exist.

Overlap note: `_overlap_of_zones` counts a pair only when the overlap is
STRICTLY positive (`overlap_low < overlap_high`), so two zones that merely
touch produce no POI. depth_ticks is the price span divided by tick_size, which
defaults to 0.00001 - i.e. one tick is 0.1 pip here, not one pip.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.poi import (
    DEFAULT_POI_CONFIG,
    PoiConfig,
    PoiEngine,
    PoiEvent,
    PoiState,
    _overlap_of_zones,
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
    """A zero-range bar: enough to give the engine a timestamp and a close."""
    return bar(hour, price, price, close=price, open_=price, day=day)


def engine(**overrides):
    return PoiEngine(PoiConfig(**overrides))


def last(eng):
    evs = eng.get_events(1)
    return evs[-1] if evs else None


def candles(day=0, hour=1):
    return [flat(hour - 1, 1.0800, day=day), flat(hour, 1.0805, day=day)]


def two_zones():
    """FVG 1.0800-1.0810 and ORDER_BLOCK 1.0805-1.0815.

    Overlap is 1.0805-1.0810 = 0.0005 = 50 ticks at tick_size 0.00001.
    """
    return [
        {"low": 1.0800, "high": 1.0810, "kind": "FVG"},
        {"low": 1.0805, "high": 1.0815, "kind": "ORDER_BLOCK"},
    ]


# ── _overlap_of_zones ───────────────────────────────────────────────────────

def test_overlap_helper_finds_the_pair():
    overlaps = _overlap_of_zones(two_zones(), DEFAULT_POI_CONFIG.tick_size)
    assert len(overlaps) == 1
    low, high, depth_ticks, kinds = overlaps[0]
    assert low == approx(1.0805)
    assert high == approx(1.0810)
    assert depth_ticks == approx(50.0)
    assert kinds == ("FVG", "ORDER_BLOCK")


def test_overlap_helper_needs_two_valid_zones():
    assert _overlap_of_zones([], 0.00001) == []
    assert _overlap_of_zones([{"low": 1.08, "high": 1.09}], 0.00001) == []


def test_overlap_helper_rejects_a_mere_touch():
    """A shared edge is zero depth, which is not an overlap."""
    zones = [{"low": 1.0800, "high": 1.0810}, {"low": 1.0810, "high": 1.0820}]
    assert _overlap_of_zones(zones, 0.00001) == []


def test_overlap_helper_skips_malformed_zones_without_raising():
    zones = [None, "not-a-zone", {"low": 1.08}, {}, two_zones()[0], two_zones()[1]]
    overlaps = _overlap_of_zones(zones, DEFAULT_POI_CONFIG.tick_size)
    assert len(overlaps) == 1
    assert overlaps[0][3] == ("FVG", "ORDER_BLOCK")


def test_overlap_helper_orders_kinds_alphabetically():
    zones = [{"low": 1.08, "high": 1.09, "kind": "ZONE"}, {"low": 1.085, "high": 1.095, "kind": "ALPHA"}]
    assert _overlap_of_zones(zones, 0.00001)[0][3] == ("ALPHA", "ZONE")


def test_overlap_helper_defaults_missing_kind_to_unknown():
    zones = [{"low": 1.08, "high": 1.09}, {"low": 1.085, "high": 1.095}]
    assert _overlap_of_zones(zones, 0.00001)[0][3] == ("UNKNOWN",)


# ── happy path ──────────────────────────────────────────────────────────────

def test_happy_path_emits_found_and_eligible():
    eng = engine()
    events = eng.analyze(candles(), symbol="EURUSD", zones=two_zones())
    assert [e["event"] for e in events] == [PoiEvent.POI_FOUND.value, PoiEvent.POI_ELIGIBLE.value]
    assert events[0]["low"] == approx(1.0805)
    assert events[0]["high"] == approx(1.0810)
    assert events[0]["overlap_depth_ticks"] == approx(50.0)
    assert events[0]["contributing_kinds"] == ["FVG", "ORDER_BLOCK"]
    assert events[0]["state"] == PoiState.ELIGIBLE.value
    assert events[0]["eligible"] is True
    assert events[0]["symbol"] == "EURUSD"


def test_default_config_marks_overlaps_eligible():
    """With no killzone or discount gate the overlap is eligible by default."""
    assert DEFAULT_POI_CONFIG.require_killzone is False
    assert DEFAULT_POI_CONFIG.require_discount_for_longs is False
    assert last(engine()) is None
    assert engine().analyze(candles(), symbol="EURUSD", zones=two_zones())[0]["eligible"] is True


# ── no-signal paths ─────────────────────────────────────────────────────────

def test_no_signal_without_enough_candles():
    eng = engine()
    assert eng.analyze([flat(0, 1.08)], symbol="EURUSD", zones=two_zones()) == []
    assert eng.analyze([], symbol="EURUSD", zones=two_zones()) == []


def test_no_signal_without_zones():
    eng = engine()
    assert eng.analyze(candles(), symbol="EURUSD") == []
    assert eng.analyze(candles(), symbol="EURUSD", zones=[]) == []


def test_no_signal_when_zones_do_not_overlap():
    eng = engine()
    zones = [{"low": 1.0800, "high": 1.0805}, {"low": 1.0810, "high": 1.0815}]
    assert eng.analyze(candles(), symbol="EURUSD", zones=zones) == []


# ── every Config knob gates behaviour ───────────────────────────────────────

def test_min_overlap_ticks_filters_shallow_overlaps():
    eng = engine(require_overlap=True, min_overlap_ticks=100.0)
    assert eng.analyze(candles(), symbol="EURUSD", zones=two_zones()) == []
    # 49.0 rather than the exact 50.0: (1.0810 - 1.0805) / 0.00001 lands on
    # 49.999999999999944 in binary floating point, so an exact threshold fails.
    eng2 = engine(require_overlap=True, min_overlap_ticks=49.0)
    assert len(eng2.analyze(candles(), symbol="EURUSD", zones=two_zones())) == 2


def test_require_overlap_false_ignores_the_depth_floor():
    eng = engine(require_overlap=False, min_overlap_ticks=9999.0)
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=two_zones())) == 2


def test_max_pois_truncates_the_deepest_first():
    zones = [
        {"low": 1.0800, "high": 1.0810, "kind": "A"},
        {"low": 1.0805, "high": 1.0815, "kind": "B"},
        {"low": 1.0808, "high": 1.0820, "kind": "C"},
    ]
    eng = engine(max_pois=1)
    events = eng.analyze(candles(), symbol="EURUSD", zones=zones)
    found = [e for e in events if e["event"] == PoiEvent.POI_FOUND.value]
    assert len(found) == 1
    assert found[0]["low"] == approx(1.0808)
    assert found[0]["high"] == approx(1.0815)
    assert found[0]["overlap_depth_ticks"] == approx(70.0)


def test_require_killzone_gates_eligibility():
    in_kz = [flat(7, 1.0800), flat(8, 1.0805)]
    out_kz = [flat(20, 1.0800), flat(21, 1.0805)]
    eng = engine(require_killzone=True)
    events = eng.analyze(in_kz, symbol="EURUSD", zones=two_zones())
    assert events[0]["eligible"] is True
    assert events[0]["state"] == PoiState.ELIGIBLE.value
    eng2 = engine(require_killzone=True)
    events2 = eng2.analyze(out_kz, symbol="EURUSD", zones=two_zones())
    assert events2[0]["eligible"] is False
    assert events2[0]["state"] == PoiState.CANDIDATE.value
    assert [e["event"] for e in events2] == [PoiEvent.POI_FOUND.value]


def test_require_discount_for_longs_gates_eligibility():
    eng = engine(require_discount_for_longs=True)
    events = eng.analyze(candles(), symbol="EURUSD", zones=two_zones(), context={})
    assert events[0]["eligible"] is False
    eng2 = engine(require_discount_for_longs=True)
    events2 = eng2.analyze(
        candles(), symbol="EURUSD", zones=two_zones(), context={"position": "DISCOUNT"}
    )
    assert events2[0]["eligible"] is True
    eng3 = engine(require_discount_for_longs=True)
    events3 = eng3.analyze(
        candles(), symbol="EURUSD", zones=two_zones(), context={"in_discount": True}
    )
    assert events3[0]["eligible"] is True


# ── lifecycle: each event fires exactly once per step ───────────────────────

def test_repeat_call_with_identical_input_is_silent():
    eng = engine()
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=two_zones())) == 2
    assert eng.analyze(candles(), symbol="EURUSD", zones=two_zones()) == []


def test_a_genuinely_new_poi_still_fires():
    eng = engine()
    eng.analyze(candles(), symbol="EURUSD", zones=two_zones())
    new_zones = [{"low": 1.0900, "high": 1.0910}, {"low": 1.0905, "high": 1.0915}]
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=new_zones)) == 2


def test_first_look_on_an_already_seen_set_emits_the_whole_path():
    zones = two_zones()
    eng = engine()
    eng.analyze(candles(), symbol="EURUSD", zones=zones)
    fresh = engine()
    events = fresh.analyze(candles(), symbol="EURUSD", zones=zones)
    assert [e["event"] for e in events] == [PoiEvent.POI_FOUND.value, PoiEvent.POI_ELIGIBLE.value]


def test_stacking_a_second_overlap_onto_a_seen_one_fires_only_the_new_pair():
    eng = engine()
    eng.analyze(candles(), symbol="EURUSD", zones=two_zones())
    zones = two_zones() + [{"low": 1.0808, "high": 1.0820, "kind": "C"}]
    events = eng.analyze(candles(), symbol="EURUSD", zones=zones)
    found = [e for e in events if e["event"] == PoiEvent.POI_FOUND.value]
    assert len(found) == 2


# ── payload shape ───────────────────────────────────────────────────────────

def test_payload_carries_the_record_minus_created_at():
    eng = engine()
    ev = eng.analyze(candles(), symbol="EURUSD", zones=two_zones())[0]
    assert "created_at" not in ev
    assert ev["timestamp"] == MONDAY + HOUR
    assert set(ev) == {
        "event",
        "timestamp",
        "symbol",
        "timeframe",
        "low",
        "high",
        "overlap_depth_ticks",
        "contributing_kinds",
        "eligible",
        "state",
    }


def test_events_are_capped_at_fifty():
    eng = engine()
    for i in range(40):
        shift = i * 0.0010
        zones = [
            {"low": 1.0800 + shift, "high": 1.0810 + shift},
            {"low": 1.0805 + shift, "high": 1.0815 + shift},
        ]
        eng.analyze(candles(), symbol="EURUSD", zones=zones)
    assert len(eng._events) <= 50


# ── isolation, reset, determinism ───────────────────────────────────────────

def test_symbol_isolation():
    eng = engine()
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=two_zones())) == 2
    assert len(eng.analyze(candles(), symbol="XAUUSD", zones=two_zones())) == 2
    assert eng.analyze(candles(), symbol="EURUSD", zones=two_zones()) == []


def test_timeframe_isolation():
    eng = engine()
    assert len(eng.analyze(candles(), tf=60, symbol="EURUSD", zones=two_zones())) == 2
    assert len(eng.analyze(candles(), tf=300, symbol="EURUSD", zones=two_zones())) == 2


def test_reset_clears_state_events_and_seen():
    eng = engine()
    eng.analyze(candles(), symbol="EURUSD", zones=two_zones())
    eng.reset()
    assert eng.get_all() == []
    assert eng.get_events() == []
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=two_zones())) == 2


def test_get_active_and_get_all_expose_the_latest_record():
    eng = engine()
    eng.analyze(candles(), symbol="EURUSD", zones=two_zones())
    assert eng.get_active()[0]["low"] == approx(1.0805)
    assert eng.get_all()[0]["state"] == PoiState.ELIGIBLE.value


def test_determinism():
    a = engine().analyze(candles(), symbol="EURUSD", zones=two_zones())
    b = engine().analyze(candles(), symbol="EURUSD", zones=two_zones())
    assert a == b


def test_default_config_identity():
    assert DEFAULT_POI_CONFIG == PoiConfig()
    assert DEFAULT_POI_CONFIG.min_overlap_ticks == 0.0
    assert DEFAULT_POI_CONFIG.max_pois == 3
    assert DEFAULT_POI_CONFIG.tick_size == approx(0.00001)
    assert DEFAULT_POI_CONFIG.pip_size == approx(0.0001)


def test_boundary_keys_lower_upper_are_accepted():
    zones = [
        {"lower_boundary": 1.0800, "upper_boundary": 1.0810, "type": "FVG"},
        {"lower_boundary": 1.0805, "upper_boundary": 1.0815, "type": "ORDER_BLOCK"},
    ]
    events = engine().analyze(candles(), symbol="EURUSD", zones=zones)
    assert events[0]["low"] == approx(1.0805)
    assert events[0]["high"] == approx(1.0810)


def test_zone_bounds_are_swapped_when_inverted():
    zones = [{"low": 1.0810, "high": 1.0800}, {"low": 1.0805, "high": 1.0815}]
    events = engine().analyze(candles(), symbol="EURUSD", zones=zones)
    assert events[0]["low"] == approx(1.0805)
    assert events[0]["high"] == approx(1.0810)
