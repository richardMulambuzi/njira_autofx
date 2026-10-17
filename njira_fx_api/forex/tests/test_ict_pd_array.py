"""Tests for the premium-discount array (PD Array) classifier.

Synthetic candles only - no fixtures, no DB, no network. Every Config knob is
asserted to actually change behaviour, not merely exist.

Tolerance note: `_merge_zones` merges two zones when their midpoints are within
`price_tolerance_ticks * tick_size`, which with the defaults is 5.0 * 0.00001 =
0.00005. It compares each candidate against a RUNNING AVERAGE of the group, so
a chain of zones can drift together. `matched` uses the same 0.00005 window
against the last candle's close.

Ranking note: `rank` is looked up in `priority_order`, NOT in `known_types`.
Those two tuples disagree on purpose: `BREAKER_BLOCK` sits in `priority_order`
(index 3) but is absent from `known_types`, so such a zone is `known=False`
while still ranking as priority 3.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.pd_array import (
    DEFAULT_PD_ARRAY_CONFIG,
    PdArrayConfig,
    PdArrayEngine,
    PdArrayEvent,
    PdArrayState,
    _merge_zones,
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


def candles(day=0, hour=1, price=1.0805):
    """Two bars; the second is the last candle, so it sets both the event
    timestamp and the `matched` reference price."""
    return [flat(hour - 1, price, day=day), flat(hour, price, day=day)]


def engine(**overrides):
    return PdArrayEngine(PdArrayConfig(**overrides))


def last(eng):
    events = eng.get_events(1)
    return events[-1] if events else None


def fvg(low=1.08000, high=1.08010):
    return {"low": low, "high": high, "kind": "FVG"}


def ob(low=1.08000, high=1.08010):
    return {"low": low, "high": high, "kind": "ORDER_BLOCK"}


def near_pair():
    """Two zones 2 ticks wide, 3 ticks apart - inside the default 5-tick
    merge window, so they collapse into one array spanning both."""
    return [
        {"low": 1.08000, "high": 1.08002, "kind": "FVG"},
        {"low": 1.08003, "high": 1.08005, "kind": "ORDER_BLOCK"},
    ]


def far_pair():
    """Two zones 9 ticks apart - outside the default window, so they stay
    separate arrays."""
    return [
        {"low": 1.08000, "high": 1.08002, "kind": "FVG"},
        {"low": 1.08009, "high": 1.08011, "kind": "ORDER_BLOCK"},
    ]


# ── the pure merge helper ───────────────────────────────────────────────────


def test_merge_zones_merges_midpoints_inside_tolerance():
    merged = _merge_zones(near_pair(), 5.0, 0.00001)
    assert len(merged) == 1
    assert merged[0]["low"] == approx(1.08000)
    assert merged[0]["high"] == approx(1.08005)
    assert merged[0]["types"] == ("FVG", "ORDER_BLOCK")


def test_merge_zones_keeps_midpoints_outside_tolerance_apart():
    merged = _merge_zones(far_pair(), 5.0, 0.00001)
    assert len(merged) == 2
    assert merged[0]["types"] == ("FVG",)
    assert merged[1]["types"] == ("ORDER_BLOCK",)


def test_merge_zones_tolerance_is_the_gate():
    # 3 ticks apart: merged at 5 ticks of tolerance, split at 1.
    assert len(_merge_zones(near_pair(), 5.0, 0.00001)) == 1
    assert len(_merge_zones(near_pair(), 1.0, 0.00001)) == 2


def test_merge_zones_empty_inputs():
    assert _merge_zones([], 5.0, 0.00001) == []
    assert _merge_zones([None, "nope", {}, {"low": 1.08}], 5.0, 0.00001) == []


def test_merge_zones_skips_malformed_but_keeps_valid():
    merged = _merge_zones([None, "nope", {}, fvg()], 5.0, 0.00001)
    assert len(merged) == 1
    assert merged[0]["types"] == ("FVG",)


def test_merge_zones_sorts_types_alphabetically():
    zones = [
        {"low": 1.08000, "high": 1.08002, "kind": "ORDER_BLOCK"},
        {"low": 1.08003, "high": 1.08005, "kind": "FVG"},
    ]
    merged = _merge_zones(zones, 5.0, 0.00001)
    assert merged[0]["types"] == ("FVG", "ORDER_BLOCK")


def test_merge_zones_defaults_missing_kind_to_unknown():
    merged = _merge_zones([{"low": 1.08000, "high": 1.08002}], 5.0, 0.00001)
    assert merged[0]["types"] == ("UNKNOWN",)


# ── happy path and no-signal path ───────────────────────────────────────────


def test_happy_path_classifies_one_array():
    eng = engine()
    events = eng.analyze(candles(price=1.080025), symbol="EURUSD", zones=near_pair())
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "PD_ARRAY_CLASSIFIED"
    assert ev["state"] == "CLASSIFIED"
    assert ev["symbol"] == "EURUSD"
    assert ev["price"] == approx(1.080025)
    assert ev["types"] == ["FVG", "ORDER_BLOCK"]
    assert ev["known"] is True
    assert ev["rank"] == 0
    assert ev["matched"] is True


def test_no_zones_is_a_no_signal():
    eng = engine()
    assert eng.analyze(candles(), symbol="EURUSD") == []
    assert eng.analyze(candles(), symbol="EURUSD", zones=[]) == []


def test_fewer_than_two_candles_is_a_no_signal():
    eng = engine()
    assert eng.analyze([], symbol="EURUSD", zones=near_pair()) == []
    assert eng.analyze([flat(1, 1.0805)], symbol="EURUSD", zones=near_pair()) == []


def test_matched_is_false_when_price_is_far_from_the_array():
    eng = engine()
    ev = last(eng)
    assert ev is None
    events = eng.analyze(candles(price=1.0900), symbol="EURUSD", zones=near_pair())
    assert events[0]["matched"] is False


# ── ranking: known_types and priority_order disagree on purpose ─────────────


def test_breaker_block_is_known_false_but_ranks_by_priority_order():
    eng = engine()
    zones = [{"low": 1.08000, "high": 1.08002, "kind": "BREAKER_BLOCK"}]
    ev = eng.analyze(candles(), symbol="EURUSD", zones=zones)[0]
    assert ev["known"] is False
    assert ev["rank"] == 3


def test_unknown_label_is_known_false_and_ranks_last():
    eng = engine()
    zones = [{"low": 1.08000, "high": 1.08002, "kind": "MYSTERY"}]
    ev = eng.analyze(candles(), symbol="EURUSD", zones=zones)[0]
    assert ev["known"] is False
    assert ev["rank"] == len(DEFAULT_PD_ARRAY_CONFIG.priority_order)


def test_known_types_knob_gates_known_without_touching_rank():
    eng = engine(known_types=("FVG",))
    zones = [{"low": 1.08000, "high": 1.08002, "kind": "ORDER_BLOCK"}]
    ev = eng.analyze(candles(), symbol="EURUSD", zones=zones)[0]
    assert ev["known"] is False
    assert ev["rank"] == 1


def test_priority_order_controls_ordering():
    eng = engine()
    zones = [
        {"low": 1.08009, "high": 1.08011, "kind": "ORDER_BLOCK"},
        {"low": 1.08000, "high": 1.08002, "kind": "FVG"},
    ]
    events = eng.analyze(candles(), symbol="EURUSD", zones=zones)
    assert [e["types"] for e in events] == [["FVG"], ["ORDER_BLOCK"]]


def test_priority_order_knob_reverses_ordering():
    eng = engine(priority_order=("ORDER_BLOCK", "FVG"))
    events = eng.analyze(candles(), symbol="EURUSD", zones=far_pair())
    assert [e["types"] for e in events] == [["ORDER_BLOCK"], ["FVG"]]


def test_matched_breaks_a_rank_tie():
    # Both are FVG so the ranks tie; the one straddling the last close wins.
    eng = engine()
    zones = [
        {"low": 1.09000, "high": 1.09002, "kind": "FVG"},
        {"low": 1.08000, "high": 1.08002, "kind": "FVG"},
    ]
    events = eng.analyze(candles(price=1.08001), symbol="EURUSD", zones=zones)
    assert events[0]["price"] == approx(1.08001)
    assert events[0]["matched"] is True
    assert events[1]["matched"] is False


def test_max_arrays_truncates_after_ranking():
    eng = engine()
    zones = [
        {"low": 1.08000, "high": 1.08002, "kind": "FVG"},
        {"low": 1.08010, "high": 1.08012, "kind": "ORDER_BLOCK"},
        {"low": 1.08020, "high": 1.08022, "kind": "BREAKER"},
        {"low": 1.08030, "high": 1.08032, "kind": "BREAKER_BLOCK"},
        {"low": 1.08040, "high": 1.08042, "kind": "MITIGATION_BLOCK"},
        {"low": 1.08050, "high": 1.08052, "kind": "LIQUIDITY_VOID"},
    ]
    events = eng.analyze(candles(), symbol="EURUSD", zones=zones)
    assert len(events) == 5
    assert "LIQUIDITY_VOID" not in [t for e in events for t in e["types"]]


def test_max_arrays_is_the_gate():
    assert len(engine(max_arrays=1).analyze(candles(), symbol="EURUSD", zones=far_pair())) == 1
    assert len(engine(max_arrays=2).analyze(candles(), symbol="EURUSD", zones=far_pair())) == 2


def test_price_tolerance_ticks_gates_merging():
    assert len(engine(price_tolerance_ticks=5.0).analyze(
        candles(), symbol="EURUSD", zones=near_pair()
    )) == 1
    assert len(engine(price_tolerance_ticks=1.0).analyze(
        candles(), symbol="EURUSD", zones=near_pair()
    )) == 2


# ── one event per step, not per poll ────────────────────────────────────────


def test_repeat_call_with_identical_zones_is_silent():
    eng = engine()
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=near_pair())) == 1
    assert eng.analyze(candles(), symbol="EURUSD", zones=near_pair()) == []


def test_a_genuinely_new_array_still_fires():
    eng = engine()
    eng.analyze(candles(), symbol="EURUSD", zones=near_pair())
    zones = near_pair() + [{"low": 1.08050, "high": 1.08052, "kind": "BREAKER"}]
    events = eng.analyze(candles(), symbol="EURUSD", zones=zones)
    assert len(events) == 1
    assert events[0]["types"] == ["BREAKER"]


def test_first_look_on_an_advanced_zone_set_emits_everything():
    seed = engine()
    assert len(seed.analyze(candles(), symbol="EURUSD", zones=far_pair())) == 2
    fresh = engine()
    assert len(fresh.analyze(candles(), symbol="EURUSD", zones=far_pair())) == 2


def test_symbol_isolation():
    eng = engine()
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=near_pair())) == 1
    assert len(eng.analyze(candles(), symbol="XAUUSD", zones=near_pair())) == 1
    assert eng.analyze(candles(), symbol="EURUSD", zones=near_pair()) == []


def test_timeframe_isolation():
    eng = engine()
    assert len(eng.analyze(candles(), tf=60, symbol="EURUSD", zones=near_pair())) == 1
    assert len(eng.analyze(candles(), tf=300, symbol="EURUSD", zones=near_pair())) == 1
    assert eng.analyze(candles(), tf=60, symbol="EURUSD", zones=near_pair()) == []


def test_event_buffer_is_capped_at_fifty():
    eng = engine()
    for i in range(60):
        zones = [{"low": 1.08 + i * 0.001, "high": 1.08 + i * 0.001 + 0.00002, "kind": "FVG"}]
        assert len(eng.analyze(candles(), symbol="EURUSD", zones=zones)) == 1
    assert len(eng.get_events(1000)) == 50


def test_reset_clears_state_events_and_the_seen_gate():
    eng = engine()
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=near_pair())) == 1
    eng.reset()
    assert eng.get_all() == []
    assert eng.get_events(10) == []
    assert len(eng.analyze(candles(), symbol="EURUSD", zones=near_pair())) == 1


def test_determinism():
    def run():
        return engine().analyze(candles(), symbol="EURUSD", zones=far_pair())

    assert run() == run()


# ── payload shape, getters, boundaries, config identity ─────────────────────


def test_payload_is_the_record_minus_created_at():
    eng = engine()
    ev = eng.analyze(candles(), symbol="EURUSD", zones=near_pair())[0]
    assert set(ev) == {
        "event",
        "timestamp",
        "symbol",
        "timeframe",
        "price",
        "types",
        "known",
        "rank",
        "matched",
        "state",
    }
    assert ev["timestamp"] == MONDAY + HOUR


def test_get_active_and_get_all_expose_the_latest_record():
    eng = engine()
    eng.analyze(candles(), symbol="EURUSD", zones=near_pair())
    assert len(eng.get_active()) == 1
    assert len(eng.get_all()) == 1
    assert eng.get_all()[0]["state"] == "CLASSIFIED"


def test_boundary_key_styles_are_accepted():
    eng = engine()
    zones = [{"lower_boundary": 1.08000, "upper_boundary": 1.08002, "type": "FVG"}]
    ev = eng.analyze(candles(), symbol="EURUSD", zones=zones)[0]
    assert ev["types"] == ["FVG"]
    assert ev["price"] == approx(1.08001)


def test_inverted_bounds_are_swapped():
    eng = engine()
    zones = [{"low": 1.08002, "high": 1.08000, "kind": "FVG"}]
    ev = eng.analyze(candles(), symbol="EURUSD", zones=zones)[0]
    assert ev["price"] == approx(1.08001)


def test_missing_kind_is_reported_as_unknown():
    eng = engine()
    ev = eng.analyze(candles(), symbol="EURUSD", zones=[{"low": 1.08000, "high": 1.08002}])[0]
    assert ev["types"] == ["UNKNOWN"]
    assert ev["known"] is False


def test_default_config_identity():
    assert DEFAULT_PD_ARRAY_CONFIG == PdArrayConfig()
    assert DEFAULT_PD_ARRAY_CONFIG.price_tolerance_ticks == approx(5.0)
    assert DEFAULT_PD_ARRAY_CONFIG.max_arrays == 5
    assert DEFAULT_PD_ARRAY_CONFIG.tick_size == approx(0.00001)
    assert DEFAULT_PD_ARRAY_CONFIG.pip_size == approx(0.0001)
    assert len(DEFAULT_PD_ARRAY_CONFIG.priority_order) == 10
    assert len(DEFAULT_PD_ARRAY_CONFIG.known_types) == 9
    assert "BREAKER_BLOCK" in DEFAULT_PD_ARRAY_CONFIG.priority_order
    assert "BREAKER_BLOCK" not in DEFAULT_PD_ARRAY_CONFIG.known_types
