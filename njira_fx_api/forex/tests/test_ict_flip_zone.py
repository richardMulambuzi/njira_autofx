"""Tests for the flip-zone (polarity reversal) detector.

Synthetic candles only - no fixtures, no DB, no network.  Every Config knob is
asserted to actually change behaviour, not merely exist.

Bar convention: the bar under test is ``candles[-2]`` (the last CLOSED bar) and
``candles[-1]`` only supplies the timestamp, so a three-bar list evaluates the
middle bar and appending a pad bar is what advances the clock.

Zone used throughout: low 1.0800 / high 1.0820, i.e. height 0.0020 = 20 pips.
"""
from datetime import datetime, timezone
from enum import Enum

from forex.analysis.flip_zone import (
    CONCEPT_NAME,
    DEFAULT_FLIP_ZONE_CONFIG,
    FlipZoneConfig,
    FlipZoneDirection,
    FlipZoneEngine,
    FlipZoneEvent,
    FlipZoneState,
    direction_from_metadata,
    flip_direction,
    initial_direction,
    normalize_zone,
)

HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())

ZONE = {"low": 1.0800, "high": 1.0820}          # height 0.0020 = 20 pips
REGISTERED_KEYS = {
    "event", "timestamp", "symbol", "timeframe", "zone_low", "zone_high",
    "height_pips", "direction", "original_direction", "state", "flip_count",
    "bars_since_created", "created_at", "flipped_at",
}
FLIPPED_KEYS = REGISTERED_KEYS | {"direction_before", "direction_after"}


def bar(hour, close, high=None, low=None, open_=None, day=0):
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = close if open_ is None else open_
    high = max(open_, close) if high is None else high
    low = min(open_, close) if low is None else low
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def bars(closes):
    """One flat bar per close price, one hour apart: high == low == close."""
    return [bar(h, c) for h, c in enumerate(closes)]


def engine(**overrides):
    return FlipZoneEngine(FlipZoneConfig(**overrides))


def last(events):
    return events[-1] if events else None


class _Side(Enum):
    """A foreign enum, to prove values (not just instances) are understood."""
    SUPPLY = "SUPPLY"



def test_normalize_zone_accepts_low_high():
    assert normalize_zone({"low": 1.0, "high": 1.5}) == (1.0, 1.5)


def test_normalize_zone_accepts_boundary_spelling():
    assert normalize_zone({"lower_boundary": 1.0, "upper_boundary": 1.5}) == (1.0, 1.5)


def test_normalize_zone_rejects_zero_height():
    assert normalize_zone({"low": 1.0, "high": 1.0}) is None


def test_normalize_zone_rejects_inverted_bounds():
    assert normalize_zone({"low": 1.5, "high": 1.0}) is None


def test_normalize_zone_rejects_missing_edge():
    assert normalize_zone({"low": 1.0}) is None


def test_normalize_zone_rejects_non_numeric():
    assert normalize_zone({"low": "n/a", "high": 1.5}) is None


def test_normalize_zone_rejects_non_dict():
    assert normalize_zone(1234) is None
    assert normalize_zone(None) is None


def test_normalize_zone_coerces_numeric_strings():
    assert normalize_zone({"low": "1.0", "high": "1.5"}) == (1.0, 1.5)



def test_metadata_reads_side_supply():
    assert direction_from_metadata({"side": "SUPPLY"}) is FlipZoneDirection.SUPPLY


def test_metadata_reads_direction_demand():
    assert direction_from_metadata({"direction": "DEMAND"}) is FlipZoneDirection.DEMAND


def test_metadata_maps_bullish_to_demand_and_bearish_to_supply():
    assert direction_from_metadata({"direction": "BULLISH"}) is FlipZoneDirection.DEMAND
    assert direction_from_metadata({"direction": "BEARISH"}) is FlipZoneDirection.SUPPLY


def test_metadata_is_case_insensitive():
    assert direction_from_metadata({"kind": "resistance"}) is FlipZoneDirection.SUPPLY


def test_metadata_reads_type_and_kind():
    assert direction_from_metadata({"type": "support"}) is FlipZoneDirection.DEMAND
    assert direction_from_metadata({"kind": "SUPPLY"}) is FlipZoneDirection.SUPPLY


def test_metadata_passes_through_a_direction_instance():
    assert direction_from_metadata({"side": FlipZoneDirection.DEMAND}) is FlipZoneDirection.DEMAND


def test_metadata_reads_the_value_of_a_foreign_enum():
    assert direction_from_metadata({"side": _Side.SUPPLY}) is FlipZoneDirection.SUPPLY


def test_metadata_returns_none_when_absent_or_unrecognised():
    assert direction_from_metadata({}) is None
    assert direction_from_metadata({"side": "SIDEWAYS"}) is None
    assert direction_from_metadata(None) is None


def test_metadata_skips_a_blank_key_and_keeps_looking():
    assert direction_from_metadata({"direction": None, "type": "SUPPORT"}) is FlipZoneDirection.DEMAND



def test_initial_direction_prefers_the_callers_label():
    zone = {"low": 1.0800, "high": 1.0820, "direction": "SUPPLY"}
    assert initial_direction(zone, 1.1000) is FlipZoneDirection.SUPPLY


def test_initial_direction_infers_demand_when_price_is_above():
    assert initial_direction(ZONE, 1.0830) is FlipZoneDirection.DEMAND


def test_initial_direction_infers_supply_when_price_is_below():
    assert initial_direction(ZONE, 1.0790) is FlipZoneDirection.SUPPLY


def test_initial_direction_infers_supply_when_price_is_inside():
    assert initial_direction(ZONE, 1.0810) is FlipZoneDirection.SUPPLY


def test_initial_direction_is_unknown_without_usable_bounds():
    assert initial_direction({"direction": "SIDEWAYS"}, 1.0) is FlipZoneDirection.UNKNOWN



def test_flip_direction_flips_supply_on_a_close_above_the_high():
    got = flip_direction(FlipZoneDirection.SUPPLY, 1.0830, 1.0830, 1.0790, 1.0800, 1.0820)
    assert got is FlipZoneDirection.DEMAND


def test_flip_direction_holds_supply_inside_the_zone():
    got = flip_direction(FlipZoneDirection.SUPPLY, 1.0810, 1.0810, 1.0805, 1.0800, 1.0820)
    assert got is None


def test_flip_direction_holds_supply_when_price_trades_away_below():
    """A close below a supply zone is supply still being respected."""
    got = flip_direction(FlipZoneDirection.SUPPLY, 1.0700, 1.0700, 1.0690, 1.0800, 1.0820)
    assert got is None


def test_flip_direction_flips_demand_on_a_close_below_the_low():
    got = flip_direction(FlipZoneDirection.DEMAND, 1.0780, 1.0810, 1.0780, 1.0800, 1.0820)
    assert got is FlipZoneDirection.SUPPLY


def test_flip_direction_holds_demand_inside_the_zone():
    got = flip_direction(FlipZoneDirection.DEMAND, 1.0810, 1.0815, 1.0805, 1.0800, 1.0820)
    assert got is None


def test_flip_direction_holds_demand_when_price_trades_away_above():
    got = flip_direction(FlipZoneDirection.DEMAND, 1.0900, 1.0900, 1.0890, 1.0800, 1.0820)
    assert got is None


def test_flip_direction_never_flips_an_unknown_direction():
    assert flip_direction(FlipZoneDirection.UNKNOWN, 1.0900, 1.0900, 1.0700, 1.0800, 1.0820) is None


def test_flip_direction_never_flips_a_zero_height_zone():
    assert flip_direction(FlipZoneDirection.SUPPLY, 1.0830, 1.0830, 1.0830, 1.0800, 1.0800) is None


def test_flip_direction_buffer_is_a_fraction_of_zone_height():
    """0.5 * 0.0020 = 0.0010, so the close must clear 1.0830.

    The fixture stays clear of the edge itself: 1.0820 + 0.0010 is not exactly
    representable in binary, so a value sitting ON the edge would test the float
    parser rather than the buffer.
    """
    short = (1.0825, 1.0825, 1.0790, 1.0800, 1.0820)
    assert flip_direction(FlipZoneDirection.SUPPLY, *short, confirm_close_pct=0.5) is None
    past = (1.0835, 1.0835, 1.0790, 1.0800, 1.0820)
    assert flip_direction(FlipZoneDirection.SUPPLY, *past, confirm_close_pct=0.5) is FlipZoneDirection.DEMAND


def test_flip_direction_buffer_applies_to_the_low_side():
    """0.5 * 0.0020 = 0.0010, so the close must clear 1.0790 downwards."""
    short = (1.0795, 1.0810, 1.0795, 1.0800, 1.0820)
    assert flip_direction(FlipZoneDirection.DEMAND, *short, confirm_close_pct=0.5) is None
    deeper = (1.0785, 1.0810, 1.0785, 1.0800, 1.0820)
    assert flip_direction(FlipZoneDirection.DEMAND, *deeper, confirm_close_pct=0.5) is FlipZoneDirection.SUPPLY


def test_flip_direction_default_buffer_is_zero():
    got = flip_direction(FlipZoneDirection.SUPPLY, 1.0821, 1.0821, 1.0790, 1.0800, 1.0820)
    assert got is FlipZoneDirection.DEMAND


def test_flip_direction_close_mode_ignores_a_wick_alone():
    got = flip_direction(FlipZoneDirection.SUPPLY, 1.0810, 1.0900, 1.0790, 1.0800, 1.0820)
    assert got is None


def test_flip_direction_wick_mode_accepts_a_wick_alone():
    got = flip_direction(FlipZoneDirection.SUPPLY, 1.0810, 1.0900, 1.0790, 1.0800, 1.0820,
                         allow_wick_confirm=True)
    assert got is FlipZoneDirection.DEMAND


def test_flip_direction_wick_mode_uses_the_low_on_the_down_side():
    got = flip_direction(FlipZoneDirection.DEMAND, 1.0810, 1.0850, 1.0770, 1.0800, 1.0820,
                         allow_wick_confirm=True)
    assert got is FlipZoneDirection.SUPPLY



def test_concept_name_and_defaults():
    assert CONCEPT_NAME == "flip_zone"
    assert DEFAULT_FLIP_ZONE_CONFIG == FlipZoneConfig()
    assert DEFAULT_FLIP_ZONE_CONFIG.confirm_close_pct == 0.0
    assert DEFAULT_FLIP_ZONE_CONFIG.allow_wick_confirm is False
    assert DEFAULT_FLIP_ZONE_CONFIG.max_age_bars == 30
    assert DEFAULT_FLIP_ZONE_CONFIG.pip_size == 0.0001


def test_enum_values_are_stable():
    assert FlipZoneDirection.SUPPLY.value == "SUPPLY"
    assert FlipZoneDirection.DEMAND.value == "DEMAND"
    assert FlipZoneState.UNBROKEN.value == "UNBROKEN"
    assert FlipZoneState.FLIPPED.value == "FLIPPED"
    assert FlipZoneEvent.ZONE_REGISTERED.value == "FZ_ZONE_REGISTERED"
    assert FlipZoneEvent.POLARITY_FLIPPED.value == "FZ_POLARITY_FLIPPED"
    assert FlipZoneEvent.ZONE_EXPIRED.value == "FZ_ZONE_EXPIRED"



def test_too_few_candles_returns_empty():
    eng = engine()
    assert eng.analyze([], zones=[ZONE]) == []
    assert eng.analyze([bar(0, 1.0790)], zones=[ZONE]) == []
    assert eng.get_active() == []


def test_a_bar_without_a_close_is_ignored():
    eng = engine()
    odd = [{"open_time": MONDAY, "high": 1.09, "low": 1.08},
           {"open_time": MONDAY + HOUR, "high": 1.09, "low": 1.08}]
    assert eng.analyze(odd, zones=[ZONE]) == []
    assert eng.get_active() == []



def test_registers_a_zone_and_infers_supply():
    ev = engine().analyze(bars([1.0790, 1.0790, 1.0790]), zones=[ZONE])
    assert len(ev) == 1
    assert ev[0]["event"] == "FZ_ZONE_REGISTERED"
    assert ev[0]["direction"] == "SUPPLY"
    assert ev[0]["original_direction"] == "SUPPLY"
    assert ev[0]["state"] == "UNBROKEN"
    assert ev[0]["flip_count"] == 0
    assert ev[0]["bars_since_created"] == 0
    assert ev[0]["zone_low"] == 1.08
    assert ev[0]["zone_high"] == 1.082
    assert ev[0]["height_pips"] == 20.0


def test_registers_a_zone_and_infers_demand():
    ev = engine().analyze(bars([1.0830, 1.0830, 1.0830]), zones=[ZONE])
    assert ev[0]["direction"] == "DEMAND"
    assert ev[0]["original_direction"] == "DEMAND"


def test_registration_event_payload_keys():
    ev = engine().analyze(bars([1.0790, 1.0790]), zones=[ZONE])
    assert set(ev[0]) == REGISTERED_KEYS


def test_registration_honours_the_callers_label():
    zone = {"low": 1.0800, "high": 1.0820, "direction": "DEMAND"}
    ev = engine().analyze(bars([1.0790, 1.0790]), zones=[zone])
    assert ev[0]["direction"] == "DEMAND"


def test_registration_uses_the_last_bar_timestamp():
    ev = engine().analyze(bars([1.0790, 1.0790, 1.0790]), zones=[ZONE])
    assert ev[0]["timestamp"] == MONDAY + 2 * HOUR
    assert ev[0]["created_at"] == MONDAY + 2 * HOUR
    assert ev[0]["flipped_at"] is None


def test_height_pips_uses_pip_size():
    gold = {"low": 100.00, "high": 100.05}
    ev = engine(pip_size=0.01).analyze(bars([99.0, 99.0]), zones=[gold])
    assert ev[0]["height_pips"] == 5.0


def test_repassing_the_same_zone_registers_once():
    eng = engine()
    for _ in range(3):
        ev = eng.analyze(bars([1.0790, 1.0790]), zones=[ZONE])
    assert ev == []
    assert len(eng.get_active()) == 1


def test_two_zones_are_tracked_independently():
    a = {"low": 1.0800, "high": 1.0820}
    b = {"low": 1.0850, "high": 1.0870}
    eng = engine()
    ev = eng.analyze(bars([1.0790, 1.0790]), zones=[a, b])
    assert len(ev) == 2
    assert len(eng.get_active()) == 2


def test_malformed_zones_are_skipped():
    eng = engine()
    ev = eng.analyze(bars([1.0790, 1.0790]),
                     zones=[{"low": 1.08, "high": 1.08}, {"low": 1.08}, None, "x", ZONE])
    assert len(ev) == 1
    assert len(eng.get_active()) == 1



def test_supply_flips_to_demand_on_a_close_above():
    closes = [1.0790, 1.0790, 1.0810, 1.0830, 1.0830]
    eng = engine()
    assert [e["event"] for e in eng.analyze(bars(closes[:3]), zones=[ZONE])] == ["FZ_ZONE_REGISTERED"]
    assert eng.analyze(bars(closes[:4])) == []
    ev = eng.analyze(bars(closes))
    assert [e["event"] for e in ev] == ["FZ_POLARITY_FLIPPED"]
    assert ev[0]["direction"] == "DEMAND"
    zone = eng.get_active()[0]
    assert zone["direction"] == "DEMAND"
    assert zone["state"] == "FLIPPED"
    assert zone["flip_count"] == 1


def test_demand_flips_to_supply_on_a_close_below():
    closes = [1.0830, 1.0830, 1.0810, 1.0780, 1.0780]
    eng = engine()
    eng.analyze(bars(closes[:3]), zones=[ZONE])
    assert eng.get_active()[0]["direction"] == "DEMAND"
    assert eng.analyze(bars(closes[:4])) == []
    ev = eng.analyze(bars(closes))
    assert [e["event"] for e in ev] == ["FZ_POLARITY_FLIPPED"]
    assert ev[0]["direction_before"] == "DEMAND"
    assert ev[0]["direction_after"] == "SUPPLY"
    assert eng.get_active()[0]["original_direction"] == "DEMAND"


def test_no_flip_while_price_stays_inside():
    closes = [1.0790, 1.0810, 1.0805, 1.0810]
    eng = engine()
    eng.analyze(bars(closes[:2]), zones=[ZONE])
    assert eng.analyze(bars(closes[:3])) == []
    assert eng.analyze(bars(closes)) == []
    zone = eng.get_active()[0]
    assert zone["flip_count"] == 0
    assert zone["state"] == "UNBROKEN"


def test_flip_event_carries_direction_before_and_after():
    eng = engine()
    eng.analyze(bars([1.0790, 1.0790, 1.0790]), zones=[ZONE])
    ev = eng.analyze(bars([1.0790, 1.0790, 1.0830, 1.0830]))
    assert ev[0]["direction_before"] == "SUPPLY"
    assert ev[0]["direction_after"] == "DEMAND"
    assert ev[0]["timestamp"] == MONDAY + 3 * HOUR
    assert ev[0]["flipped_at"] == MONDAY + 3 * HOUR


def test_flip_event_payload_keys():
    eng = engine()
    eng.analyze(bars([1.0790, 1.0790, 1.0790]), zones=[ZONE])
    ev = eng.analyze(bars([1.0790, 1.0790, 1.0830, 1.0830]))
    assert set(ev[0]) == FLIPPED_KEYS


def test_reanalyzing_the_same_list_flip_is_idempotent():
    eng = engine()
    eng.analyze(bars([1.0790, 1.0790, 1.0790]), zones=[ZONE])
    flip = bars([1.0790, 1.0790, 1.0830, 1.0830])
    assert [e["event"] for e in eng.analyze(flip)] == ["FZ_POLARITY_FLIPPED"]
    assert eng.analyze(flip) == []
    assert eng.get_active()[0]["flip_count"] == 1


def test_a_second_flip_counts_as_two_and_keeps_the_original_direction():
    closes = [1.0790, 1.0790, 1.0830, 1.0780, 1.0780]
    eng = engine()
    eng.analyze(bars(closes[:3]), zones=[ZONE])
    assert [e["direction_after"] for e in eng.analyze(bars(closes[:4]))] == ["DEMAND"]
    assert [e["direction_after"] for e in eng.analyze(bars(closes))] == ["SUPPLY"]
    zone = eng.get_active()[0]
    assert zone["flip_count"] == 2
    assert zone["direction"] == "SUPPLY"
    assert zone["original_direction"] == "SUPPLY"
    assert zone["state"] == "FLIPPED"


def test_formed_candle_alone_never_flips():
    eng = engine()
    ev = eng.analyze(bars([1.0790, 1.0790, 1.0830]), zones=[ZONE])
    assert [e["event"] for e in ev] == ["FZ_ZONE_REGISTERED"]
    assert eng.get_active()[0]["direction"] == "SUPPLY"


def test_flip_does_not_fire_without_a_registered_zone():
    eng = engine()
    assert eng.analyze(bars([1.0700, 1.0900])) == []
    assert eng.get_active() == []


def test_buffer_defers_the_flip():
    """The same bar flips under the default buffer and holds under a wider one."""
    closes = [1.0790, 1.0790, 1.0825, 1.0835, 1.0835]

    strict = engine(confirm_close_pct=0.5)
    strict.analyze(bars(closes[:3]), zones=[ZONE])
    assert strict.analyze(bars(closes[:4])) == []

    loose = engine()
    loose.analyze(bars(closes[:3]), zones=[ZONE])
    assert [e["event"] for e in loose.analyze(bars(closes[:4]))] == ["FZ_POLARITY_FLIPPED"]

    ev = strict.analyze(bars(closes))
    assert [e["event"] for e in ev] == ["FZ_POLARITY_FLIPPED"]


def test_wick_confirm_flips_on_a_wick_alone():
    def series():
        return [bar(0, 1.0790), bar(1, 1.0790),
                bar(2, 1.0810, high=1.0900, low=1.0805),
                bar(3, 1.0810, high=1.0900, low=1.0805)]

    for allow, expected in ((False, []), (True, ["FZ_POLARITY_FLIPPED"])):
        eng = engine(allow_wick_confirm=allow)
        eng.analyze(series()[:3], zones=[ZONE])
        assert [e["event"] for e in eng.analyze(series())] == expected



def test_zone_expires_after_max_age_bars():
    closes = [1.0790] * 6
    eng = engine(max_age_bars=2)
    eng.analyze(bars(closes[:3]), zones=[ZONE])
    assert eng.analyze(bars(closes[:4])) == []
    assert eng.analyze(bars(closes[:5])) == []
    ev = eng.analyze(bars(closes))
    assert [e["event"] for e in ev] == ["FZ_ZONE_EXPIRED"]
    assert ev[0]["bars_since_created"] == 3
    assert eng.get_active() == []


def test_max_age_bars_of_zero_survives_its_own_registration_bar():
    closes = [1.0790] * 4
    eng = engine(max_age_bars=0)
    ev = eng.analyze(bars(closes[:3]), zones=[ZONE])
    assert [e["event"] for e in ev] == ["FZ_ZONE_REGISTERED"]
    assert len(eng.get_active()) == 1
    ev = eng.analyze(bars(closes))
    assert [e["event"] for e in ev] == ["FZ_ZONE_EXPIRED"]


def test_repeated_calls_without_a_new_bar_never_expire():
    candles = bars([1.0790] * 3)
    eng = engine(max_age_bars=0)
    eng.analyze(candles, zones=[ZONE])
    for _ in range(5):
        assert eng.analyze(candles) == []
    assert len(eng.get_active()) == 1
    assert eng.get_active()[0]["bars_since_created"] == 0


def test_expired_zone_can_be_registered_again():
    closes = [1.0790] * 6
    eng = engine(max_age_bars=1)
    eng.analyze(bars(closes[:3]), zones=[ZONE])
    eng.analyze(bars(closes[:4]))
    ev = eng.analyze(bars(closes[:5]))
    assert [e["event"] for e in ev] == ["FZ_ZONE_EXPIRED"]
    ev = eng.analyze(bars(closes), zones=[ZONE])
    assert [e["event"] for e in ev] == ["FZ_ZONE_REGISTERED"]
    assert eng.get_active()[0]["bars_since_created"] == 0


def test_expiry_event_payload_keys():
    eng = engine(max_age_bars=0)
    eng.analyze(bars([1.0790, 1.0790, 1.0790]), zones=[ZONE])
    ev = eng.analyze(bars([1.0790, 1.0790, 1.0790, 1.0790]))
    assert set(ev[0]) == REGISTERED_KEYS



def test_symbols_do_not_share_state():
    candles = bars([1.0790, 1.0790])
    eng = engine()
    eng.analyze(candles, symbol="EURUSD", zones=[ZONE])
    assert eng.analyze(candles, symbol="GBPUSD") == []
    assert len(eng.get_active()) == 1
    assert eng.get_active()[0]["symbol"] == "EURUSD"


def test_timeframes_do_not_share_state():
    candles = bars([1.0790, 1.0790])
    eng = engine()
    eng.analyze(candles, tf=60, symbol="EURUSD", zones=[ZONE])
    assert eng.analyze(candles, tf=15, symbol="EURUSD") == []
    assert len(eng.get_active()) == 1
    assert eng.get_active()[0]["timeframe"] == 60


def test_reset_clears_zones_cursor_and_events():
    candles = bars([1.0790, 1.0790, 1.0790])
    eng = engine()
    eng.analyze(candles, zones=[ZONE])
    eng.reset()
    assert eng.get_active() == []
    assert eng.get_events() == []
    ev = eng.analyze(candles, zones=[ZONE])
    assert [e["event"] for e in ev] == ["FZ_ZONE_REGISTERED"]


def test_get_active_and_get_all_respect_the_limit():
    zones = [{"low": 1.000 + i * 0.01, "high": 1.005 + i * 0.01} for i in range(3)]
    eng = engine()
    eng.analyze(bars([1.50, 1.50]), zones=zones)
    assert len(eng.get_active()) == 3
    assert len(eng.get_active(2)) == 2
    assert len(eng.get_all(20)) == 3
    assert len(eng.get_all(1)) == 1


def test_event_log_is_capped_at_fifty():
    zones = [{"low": 1.000 + i * 0.01, "high": 1.005 + i * 0.01} for i in range(55)]
    eng = engine()
    ev = eng.analyze(bars([1.50, 1.50]), zones=zones)
    assert len(ev) == 55
    assert len(eng.get_events(100)) == 50


def test_engine_uses_the_injected_config():
    assert engine(max_age_bars=7).config.max_age_bars == 7
    assert FlipZoneEngine().config == DEFAULT_FLIP_ZONE_CONFIG

