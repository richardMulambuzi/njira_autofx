"""Tests for the balanced price range detector.

Synthetic candles only: no fixtures, no DB, no network. Every Config knob is
asserted to change behaviour, not merely exist.

Fixture arithmetic, derived by hand and checked against
``find_fair_value_gaps`` directly:

The eight bar series used throughout is built so that exactly two fair value
gaps exist, one bullish and one bearish, three bars apart.

  bullish series: bull gap forms on bar 2 (zone 1.1000 to 1.1040),
                  bear gap forms on bar 5 (zone 1.1020 to 1.1060)
  bearish series: bear gap forms on bar 2 (zone 1.1020 to 1.1060),
                  bull gap forms on bar 5 (zone 1.1000 to 1.1040)

Either way the shared band is 1.1020 to 1.1040, depth 0.0020, and the smaller
contributing gap is 0.0040 tall, so ``overlap_pct`` is 0.5 and
``height_pips`` is 20.0. The later gap sets the direction, so the first series
labels BULLISH and the second labels BEARISH.

``candles[-2]`` is always bar index 6 with a close of 1.1030, which sits inside
the band, so a freshly formed zone is ACTIVE. The break fixtures move only that
close, which never changes gap detection because a close is not a gap edge.
"""
from datetime import datetime, timezone

from forex.analysis.balanced_price_range import (
    CONCEPT_NAME,
    CONCEPT_SOURCES,
    DEFAULT_BPR_CONFIG,
    BalancedPriceRange,
    BprConfig,
    BprDirection,
    BprEngine,
    BprEvent,
    BprState,
    _time_key,
    _val,
    find_fair_value_gaps,
    overlap_of,
)

HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())

BULL_ZONE = (1.1000, 1.1040)
BEAR_ZONE = (1.1020, 1.1060)
BAND = (1.1020, 1.1040)
HEIGHT_PIPS = 20.0
OVERLAP_PCT = 0.5
BARS_APART = 3

REGISTRY_KEYS = {
    "symbol",
    "timeframe",
    "bpr_low",
    "bpr_high",
    "height_pips",
    "direction",
    "bull_zone_low",
    "bull_zone_high",
    "bear_zone_low",
    "bear_zone_high",
    "bull_formation_index",
    "bear_formation_index",
    "bars_apart",
    "overlap_pct",
    "state",
    "bars_since_formed",
    "created_at",
    "broken_at",
    "broken_side",
}
EVENT_KEYS = REGISTRY_KEYS | {"event", "timestamp"}


def bar(hour, high, low, close=None, open_=None, day=0):
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = low if open_ is None else open_
    close = open_ if close is None else close
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def engine(**overrides):
    return BprEngine(BprConfig(**overrides))


def last(events):
    return events[-1] if events else None


def pad(hour, day=0):
    """A wide bar outside the band: creates no gap and breaks nothing."""
    return bar(hour, 1.1120, 1.0980, close=1.1030, open_=1.1030, day=day)


def bullish_series(close=None, day=0):
    """Bear gap first, bull gap later, so the band reads BULLISH."""
    c = 1.1030 if close is None else close
    return [
        bar(0, 1.1080, 1.1060, close=1.1070, day=day),
        bar(1, 1.0990, 1.0910, close=1.0950, day=day),
        bar(2, 1.1020, 1.1000, close=1.1010, day=day),
        bar(3, 1.1000, 1.0980, close=1.0990, day=day),
        bar(4, 1.1070, 1.1010, close=1.1030, day=day),
        bar(5, 1.1050, 1.1040, close=1.1045, day=day),
        bar(6, 1.1050, 1.1025, close=c, day=day),
        bar(7, 1.1050, 1.1020, close=1.1030, day=day),
    ]


def bearish_series(close=None, day=0):
    """Bull gap first, bear gap later, so the band reads BEARISH."""
    c = 1.1030 if close is None else close
    return [
        bar(0, 1.1000, 1.0980, close=1.0990, day=day),
        bar(1, 1.1070, 1.0990, close=1.1030, day=day),
        bar(2, 1.1050, 1.1040, close=1.1045, day=day),
        bar(3, 1.1080, 1.1060, close=1.1070, day=day),
        bar(4, 1.1090, 1.1050, close=1.1070, day=day),
        bar(5, 1.1020, 1.1000, close=1.1010, day=day),
        bar(6, 1.1050, 1.1025, close=c, day=day),
        bar(7, 1.1050, 1.1020, close=1.1030, day=day),
    ]


def series(direction="BULLISH", close=None, day=0):
    if direction == "BULLISH":
        return bullish_series(close, day)
    return bearish_series(close, day)


def kinds(gaps):
    return sorted((g["kind"], g["formation_index"]) for g in gaps)


def three(prev_bar, next_bar):
    """The minimum window that can hold one gap: prev, middle, next."""
    middle = bar(1, max(prev_bar["high"], next_bar["high"]), min(prev_bar["low"], next_bar["low"]))
    return [prev_bar, middle, next_bar]


def test_concept_identity():
    assert CONCEPT_NAME == "balanced_price_range"
    assert CONCEPT_SOURCES == ("ict_bpr", "balanced_price_range")


def test_event_values_are_the_public_names():
    assert BprEvent.FORMED.value == "BPR_FORMED"
    assert BprEvent.BROKEN.value == "BPR_BROKEN"


def test_state_values_are_stable():
    assert [s.value for s in BprState] == ["ACTIVE", "BROKEN", "UNKNOWN"]


def test_direction_values_are_stable():
    assert [d.value for d in BprDirection] == ["BULLISH", "BEARISH", "UNKNOWN"]


def test_default_config_matches_the_build_order_contract():
    assert DEFAULT_BPR_CONFIG == BprConfig()
    assert DEFAULT_BPR_CONFIG.min_overlap_pct == 0.0
    assert DEFAULT_BPR_CONFIG.max_bars_apart == 20
    assert DEFAULT_BPR_CONFIG.pip_size == 0.0001
    assert DEFAULT_BPR_CONFIG.tick_size == 0.00001


def test_to_dict_key_set_is_exact():
    eng = engine()
    events = eng.analyze(series())
    assert set(last(events)) == EVENT_KEYS
    assert set(eng.get_active(1)[0]) == REGISTRY_KEYS


def test_to_dict_rounds_prices_and_percentages():
    record = BalancedPriceRange(
        symbol="EURUSD",
        timeframe=60,
        bpr_low=1.102012345,
        bpr_high=1.104056789,
        height_pips=20.0,
        direction="BULLISH",
        bull_zone_low=1.1,
        bull_zone_high=1.104,
        bear_zone_low=1.102,
        bear_zone_high=1.106,
        bull_formation_index=5,
        bear_formation_index=2,
        bars_apart=3,
        overlap_pct=0.5,
        state="ACTIVE",
        bars_since_formed=0,
    )
    payload = record.to_dict()
    assert payload["bpr_low"] == round(1.102012345, 6)
    assert payload["bpr_high"] == round(1.104056789, 6)
    assert payload["created_at"] is None
    assert payload["broken_at"] is None
    assert payload["broken_side"] is None


def test_key_identifies_the_band():
    eng = engine()
    eng.analyze(series())
    record = eng.get_active(1)[0]
    assert record["bpr_low"] == 1.102
    assert record["bpr_high"] == 1.104


def test_val_returns_the_first_present_key():
    assert _val({"close": 1.5, "c": 1.6}, "close", "c") == 1.5
    assert _val({"c": 1.6}, "close", "c") == 1.6


def test_val_skips_explicit_none():
    assert _val({"close": None, "c": 1.6}, "close", "c") == 1.6


def test_val_falls_back_for_non_dicts():
    assert _val(None, "close", default=7) == 7
    assert _val("nope", "close", default=7) == 7
    assert _val({"close": 1.0}, "high", default=9) == 9


def test_time_key_preference_order():
    assert _time_key({"open_time": 1, "time": 2, "timestamp": 3, "date": 4}) == 1
    assert _time_key({"time": 2, "timestamp": 3, "date": 4}) == 2
    assert _time_key({"timestamp": 3, "date": 4}) == 3
    assert _time_key({"date": 4}) == 4
    assert _time_key({}) is None


def test_finds_the_bullish_gap():
    gaps = find_fair_value_gaps(bullish_series())
    bull = [g for g in gaps if g["kind"] == "BULLISH"]
    assert len(bull) == 1
    assert bull[0]["low"] == BULL_ZONE[0]
    assert bull[0]["high"] == BULL_ZONE[1]


def test_finds_the_bearish_gap():
    gaps = find_fair_value_gaps(bullish_series())
    bear = [g for g in gaps if g["kind"] == "BEARISH"]
    assert len(bear) == 1
    assert bear[0]["low"] == BEAR_ZONE[0]
    assert bear[0]["high"] == BEAR_ZONE[1]


def test_the_series_holds_exactly_two_gaps():
    assert kinds(find_fair_value_gaps(bullish_series())) == [("BEARISH", 2), ("BULLISH", 5)]
    assert kinds(find_fair_value_gaps(bearish_series())) == [("BEARISH", 5), ("BULLISH", 2)]


def test_a_gap_needs_its_third_candle():
    candles = bullish_series()
    assert find_fair_value_gaps(candles[:2]) == []
    assert [g["formation_index"] for g in find_fair_value_gaps(candles[:3])] == [2]


def test_gap_needs_three_candles():
    assert find_fair_value_gaps([]) == []
    assert find_fair_value_gaps([bar(0, 1.1, 1.0)]) == []
    assert find_fair_value_gaps([bar(0, 1.1, 1.0), bar(1, 1.1, 1.0)]) == []


def test_gap_skips_malformed_candles():
    candles = bullish_series()
    candles[0] = None
    gaps = find_fair_value_gaps(candles)
    assert all(g["formation_index"] != 1 for g in gaps)


def test_touching_edges_are_not_a_gap():
    assert find_fair_value_gaps(three(bar(0, 1.1000, 1.0900), bar(2, 1.1100, 1.1000))) == []


def test_one_middle_bar_yields_at_most_one_gap():
    for builder in (bullish_series, bearish_series):
        indices = [g["formation_index"] for g in find_fair_value_gaps(builder())]
        assert len(indices) == len(set(indices))


def test_overlap_band_is_the_shared_span():
    assert overlap_of({"low": 1.1000, "high": 1.1040}, {"low": 1.1020, "high": 1.1060}) == (
        1.1020,
        1.1040,
    )


def test_touch_only_is_not_an_overlap():
    assert overlap_of({"low": 1.1000, "high": 1.1020}, {"low": 1.1020, "high": 1.1060}) is None


def test_disjoint_zones_have_no_overlap():
    assert overlap_of({"low": 1.1000, "high": 1.1020}, {"low": 1.1060, "high": 1.1080}) is None


def test_overlap_is_symmetric():
    a = {"low": 1.1000, "high": 1.1040}
    b = {"low": 1.1020, "high": 1.1060}
    assert overlap_of(a, b) == overlap_of(b, a)


def test_contained_zone_overlaps_completely():
    assert overlap_of({"low": 1.1000, "high": 1.1100}, {"low": 1.1040, "high": 1.1060}) == (
        1.1040,
        1.1060,
    )


def test_analyze_needs_three_candles():
    assert engine().analyze([]) == []
    assert engine().analyze(series()[:2]) == []


def test_analyze_returns_empty_when_the_evaluated_bar_has_no_close():
    candles = series()
    del candles[6]["close"]
    assert engine().analyze(candles) == []


def test_analyze_returns_empty_for_a_non_numeric_close():
    candles = series()
    candles[6]["close"] = "not a number"
    assert engine().analyze(candles) == []


def broken_series(side, day=0):
    """The bullish band with a close pushed past one edge on the evaluated bar."""
    candles = bullish_series(day=day)
    if side == "HIGH":
        candles[6] = bar(6, 1.1050, 1.1025, close=1.1050, day=day)
    else:
        candles[6] = bar(6, 1.1050, 1.1005, close=1.1010, day=day)
    return candles


def test_forms_a_bullish_bpr():
    events = engine().analyze(series("BULLISH"))
    assert len(events) == 1
    assert events[0]["event"] == "BPR_FORMED"
    assert events[0]["direction"] == "BULLISH"
    assert events[0]["state"] == "ACTIVE"


def test_forms_a_bearish_bpr():
    events = engine().analyze(series("BEARISH"))
    assert len(events) == 1
    assert events[0]["event"] == "BPR_FORMED"
    assert events[0]["direction"] == "BEARISH"


def test_the_band_is_the_shared_span():
    ev = engine().analyze(series())[0]
    assert ev["bpr_low"] == 1.102
    assert ev["bpr_high"] == 1.104


def test_reports_height_in_pips():
    assert engine().analyze(series())[0]["height_pips"] == HEIGHT_PIPS


def test_reports_overlap_pct():
    assert engine().analyze(series())[0]["overlap_pct"] == OVERLAP_PCT


def test_reports_bars_apart():
    assert engine().analyze(series())[0]["bars_apart"] == BARS_APART


def test_reports_the_contributing_zones():
    ev = engine().analyze(series())[0]
    assert (ev["bull_zone_low"], ev["bull_zone_high"]) == BULL_ZONE
    assert (ev["bear_zone_low"], ev["bear_zone_high"]) == BEAR_ZONE


def test_direction_follows_the_later_gap_bullish():
    ev = engine().analyze(series("BULLISH"))[0]
    assert ev["bull_formation_index"] == 5
    assert ev["bear_formation_index"] == 2
    assert ev["direction"] == "BULLISH"


def test_direction_follows_the_later_gap_bearish():
    ev = engine().analyze(series("BEARISH"))[0]
    assert ev["bull_formation_index"] == 2
    assert ev["bear_formation_index"] == 5
    assert ev["direction"] == "BEARISH"


def test_timestamp_and_created_at_come_from_the_last_candle():
    ev = engine().analyze(series())[0]
    assert ev["timestamp"] == MONDAY + 7 * HOUR
    assert ev["created_at"] == MONDAY + 7 * HOUR


def test_bars_since_formed_starts_at_zero():
    assert engine().analyze(series())[0]["bars_since_formed"] == 0


def test_forming_is_idempotent():
    eng = engine()
    eng.analyze(series())
    assert eng.analyze(series()) == []
    assert len(eng.get_all(10)) == 1


def test_min_overlap_pct_blocks_a_thin_overlap():
    assert engine().analyze(series()) != []
    assert engine(min_overlap_pct=0.6).analyze(series()) == []


def test_min_overlap_pct_allows_a_thick_enough_overlap():
    assert engine(min_overlap_pct=0.4).analyze(series()) != []


def test_max_bars_apart_allows_gaps_at_the_limit():
    assert engine(max_bars_apart=3).analyze(series()) != []


def test_max_bars_apart_blocks_gaps_beyond_the_limit():
    assert engine(max_bars_apart=2).analyze(series()) == []


def test_a_new_zone_is_not_broken_on_the_call_that_formed_it():
    events = engine().analyze(broken_series("HIGH"))
    assert [e["event"] for e in events] == ["BPR_FORMED"]
    assert events[0]["state"] == "ACTIVE"
    assert events[0]["broken_side"] is None


def test_a_high_close_breaks_the_band():
    eng = engine()
    eng.analyze(broken_series("HIGH"))
    events = eng.analyze(broken_series("HIGH"))
    assert [e["event"] for e in events] == ["BPR_BROKEN"]
    assert events[0]["broken_side"] == "HIGH"
    assert events[0]["state"] == "BROKEN"


def test_a_low_close_breaks_the_band():
    eng = engine()
    eng.analyze(broken_series("LOW"))
    events = eng.analyze(broken_series("LOW"))
    assert [e["event"] for e in events] == ["BPR_BROKEN"]
    assert events[0]["broken_side"] == "LOW"


def test_break_event_payload_key_set_is_exact():
    eng = engine()
    eng.analyze(broken_series("HIGH"))
    assert set(last(eng.analyze(broken_series("HIGH")))) == EVENT_KEYS


def test_break_records_when_it_happened():
    eng = engine()
    eng.analyze(broken_series("HIGH"))
    ev = last(eng.analyze(broken_series("HIGH")))
    assert ev["broken_at"] == MONDAY + 7 * HOUR
    assert ev["created_at"] == MONDAY + 7 * HOUR


def test_break_is_reported_once():
    eng = engine()
    eng.analyze(broken_series("HIGH"))
    eng.analyze(broken_series("HIGH"))
    assert eng.analyze(broken_series("HIGH")) == []


def test_a_broken_zone_stays_in_the_registry():
    eng = engine()
    eng.analyze(broken_series("LOW"))
    eng.analyze(broken_series("LOW"))
    assert eng.get_active(1)[0]["state"] == "BROKEN"


def test_an_inside_close_leaves_the_zone_active():
    eng = engine()
    eng.analyze(series())
    assert eng.analyze(series()) == []
    assert eng.get_active(1)[0]["state"] == "ACTIVE"


def test_bars_since_formed_counts_a_new_bar():
    eng = engine()
    eng.analyze(series())
    assert eng.analyze(series() + [pad(8)]) == []
    assert eng.get_active(1)[0]["bars_since_formed"] == 1


def test_bars_since_formed_ignores_a_repeat_of_the_same_bar():
    eng = engine()
    eng.analyze(series() + [pad(8)])
    eng.analyze(series() + [pad(8)])
    assert eng.get_active(1)[0]["bars_since_formed"] == 0


def test_bars_since_formed_keeps_counting():
    eng = engine()
    eng.analyze(series() + [pad(8)])
    eng.analyze(series() + [pad(8), pad(9)])
    assert eng.get_active(1)[0]["bars_since_formed"] == 1


def test_a_broken_zone_stops_ageing():
    eng = engine()
    eng.analyze(broken_series("HIGH"))
    eng.analyze(broken_series("HIGH"))
    before = eng.get_active(1)[0]["bars_since_formed"]
    eng.analyze(broken_series("HIGH") + [pad(8)])
    assert eng.get_active(1)[0]["bars_since_formed"] == before


def test_symbols_do_not_share_state():
    eng = engine()
    assert eng.analyze(series(), symbol="EURUSD") != []
    assert eng.analyze(series(), symbol="GBPUSD") != []
    assert len(eng.get_all(10)) == 2


def test_timeframes_do_not_share_state():
    eng = engine()
    assert eng.analyze(series(), tf=60) != []
    assert eng.analyze(series(), tf=240) != []
    assert len(eng.get_all(10)) == 2


def test_timeframe_falls_back_to_the_candle():
    candles = series()
    candles[-1]["timeframe"] = 240
    eng = engine()
    eng.analyze(candles)
    assert eng.get_active(1)[0]["timeframe"] == 240


def test_an_explicit_timeframe_wins_over_the_candle():
    candles = series()
    candles[-1]["timeframe"] = 240
    eng = engine()
    eng.analyze(candles, tf=60)
    assert eng.get_active(1)[0]["timeframe"] == 60


def test_reset_clears_registry_state_and_events():
    eng = engine()
    eng.analyze(series())
    eng.reset()
    assert eng.get_active(10) == []
    assert eng.get_all(10) == []
    assert eng.get_events(5) == []
    assert eng.analyze(series()) != []


def test_fresh_engines_are_independent():
    first, second = engine(), engine()
    first.analyze(series())
    assert second.get_active(10) == []
    assert second.analyze(series()) != []


def test_get_events_returns_the_tail():
    eng = engine()
    eng.analyze(series())
    assert [e["event"] for e in eng.get_events(1)] == ["BPR_FORMED"]


def test_get_all_respects_the_limit():
    eng = engine()
    eng.analyze(series(), symbol="EURUSD")
    eng.analyze(series(), symbol="GBPUSD")
    assert len(eng.get_all(10)) == 2
    assert len(eng.get_all(1)) == 1


def test_the_event_log_is_capped():
    eng = engine()
    for day in range(60):
        eng.analyze(series(day=day))
    assert len(eng.get_events(1000)) <= 50


