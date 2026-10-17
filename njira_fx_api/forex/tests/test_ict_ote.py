"""Tests for the standalone ICT Optimal Trade Entry (OTE) band detector.

This is the one Wave-0 module that was written without a test file.  The band
conflict (0.500-0.618 vs 0.620-0.790) is a deliberate feature: both bands are
reported on every setup and `active_band` only says which one is primary.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.ote import (
    DEFAULT_OTE_CONFIG,
    OTEConfig,
    OTEEngine,
    OTEEvent,
    OTEState,
    band_prices,
    leg_range,
    overlaps,
    retracement_of,
    zone_bounds,
)

QUARTER = 900
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())


def bar(i, o, h, l, c):
    return {"open_time": MONDAY + i * QUARTER, "open": o, "high": h, "low": l, "close": c}


def engine(**overrides):
    return OTEEngine(OTEConfig(**overrides))


def bull_leg(close):
    """Low 1.08000 forms first, high 1.09000 forms later — a 100-pip bullish leg."""
    return [
        bar(0, 1.08100, 1.08120, 1.08000, 1.08050),
        bar(1, 1.08050, 1.08200, 1.08020, 1.08180),
        bar(2, 1.08180, 1.08500, 1.08150, 1.08480),
        bar(3, 1.08480, 1.09000, 1.08480, 1.08950),
        bar(4, 1.08480, 1.08500, 1.08050, close),
    ]


def bear_leg(close):
    """High 1.09000 forms first, low 1.08000 forms later — the mirror image."""
    return [
        bar(0, 1.08950, 1.09000, 1.08900, 1.08950),
        bar(1, 1.08950, 1.08980, 1.08500, 1.08520),
        bar(2, 1.08520, 1.08600, 1.08100, 1.08120),
        bar(3, 1.08120, 1.08200, 1.08000, 1.08050),
        bar(4, 1.08120, 1.08900, 1.08050, close),
    ]


ZONE = {"high": 1.08350, "low": 1.08250}          # sits inside the roadmap band
FAR_ZONE = {"high": 1.08100, "low": 1.08050}      # nowhere near it


# ── pure helpers ────────────────────────────────────────────────────────────

def test_leg_range_reports_which_extreme_came_last():
    assert leg_range(bull_leg(1.08300), 120) == (approx(1.08000), approx(1.09000), "BULLISH")
    assert leg_range(bear_leg(1.08700), 120) == (approx(1.08000), approx(1.09000), "BEARISH")


def test_leg_range_lookback_limits_the_window():
    # only the last two bars count: low 1.08050 at index 4, so the range narrows from
    # the full-window (1.08000, 1.09000) and the direction flips — inside that short
    # window bar 3's 1.09000 high forms before bar 4's low.
    low, high, leg = leg_range(bull_leg(1.08300), 2)
    assert (low, high) == (approx(1.08050), approx(1.09000))
    assert leg == "BEARISH"


def test_retracement_is_measured_from_the_right_end():
    assert retracement_of(1.08300, 1.08000, 1.09000, "BULLISH") == approx(0.70)
    assert retracement_of(1.08700, 1.08000, 1.09000, "BEARISH") == approx(0.70)
    assert retracement_of(1.08300, 1.08000, 1.09000, "NEUTRAL") == approx(0.0)
    assert retracement_of(1.08300, 1.09000, 1.09000, "BULLISH") == approx(0.0)


def test_band_prices_are_ordered_low_to_high_either_way():
    lo, hi = band_prices(1.08000, 1.09000, "BULLISH", 0.62, 0.79)
    assert (lo, hi) == (approx(1.08210), approx(1.08380))
    lo, hi = band_prices(1.08000, 1.09000, "BEARISH", 0.62, 0.79)
    assert (lo, hi) == (approx(1.08620), approx(1.08790))


def test_zone_bounds_accept_either_key_set_and_none_when_incomplete():
    assert zone_bounds({"high": 1.08350, "low": 1.08250}) == (approx(1.08250), approx(1.08350))
    assert zone_bounds({"upper_boundary": 1.08350, "lower_boundary": 1.08250}) == \
        (approx(1.08250), approx(1.08350))
    assert zone_bounds({"high": 1.08350}) is None
    assert zone_bounds({"low": 1.08250}) is None


def test_overlaps_includes_touching_edges():
    assert overlaps((1.08210, 1.08380), (1.08350, 1.08400)) is True
    assert overlaps((1.08210, 1.08380), (1.08050, 1.08100)) is False
    assert overlaps((1.08210, 1.08380), (1.08380, 1.08400)) is True


# ── the band ────────────────────────────────────────────────────────────────

def test_the_roadmap_band_is_the_default_and_prices_the_zone():
    events = engine().analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[ZONE])
    assert [ev["event"] for ev in events] == [OTEEvent.OTE_ZONE_SET.value]
    rec = engine().get_active()          # separate engine: proves nothing leaked
    setup = engine()
    setup.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[ZONE])
    rec = setup.get_active()[0]
    assert rec["leg_direction"] == "BULLISH"
    assert rec["band_low"] == approx(0.62)
    assert rec["band_high"] == approx(0.79)
    assert rec["entry_zone_low"] == approx(1.08210)
    assert rec["entry_zone_high"] == approx(1.08380)
    assert rec["retracement"] == approx(0.70)
    assert rec["state"] == OTEState.IN_BAND.value


def test_both_bands_are_always_reported():
    e = engine()
    e.analyze(bull_leg(1.08500), tf=15, symbol="EURUSD")      # 0.50 retracement
    rec = e.get_active()[0]
    assert rec["retracement"] == approx(0.50)
    assert rec["in_litefinance_band"] is True      # 0.500-0.618
    assert rec["in_roadmap_band"] is False         # 0.620-0.790
    assert rec["state"] == OTEState.SHALLOW.value  # the ACTIVE band decides the state


def test_the_active_band_decides_in_or_out():
    candles = bull_leg(1.08500)
    roadmap = engine()
    roadmap.analyze(candles, tf=15, symbol="EURUSD")
    assert roadmap.get_active()[0]["state"] == OTEState.SHALLOW.value

    lite = engine(active_band="litefinance")
    lite.analyze(candles, tf=15, symbol="EURUSD")
    rec = lite.get_active()[0]
    assert rec["state"] == OTEState.IN_BAND.value
    assert rec["band_low"] == approx(0.50)
    assert rec["entry_zone_low"] == approx(1.08382)
    assert rec["entry_zone_high"] == approx(1.08500)


def test_below_the_band_is_shallow_and_past_it_is_deep():
    shallow = engine()
    shallow.analyze(bull_leg(1.08800), tf=15, symbol="EURUSD")
    assert shallow.get_active()[0]["state"] == OTEState.SHALLOW.value

    deep = engine()
    deep.analyze(bull_leg(1.08150), tf=15, symbol="EURUSD")
    assert deep.get_active()[0]["state"] == OTEState.DEEP.value


def test_the_band_edges_are_inclusive():
    e = engine()
    e.analyze(bull_leg(1.08380), tf=15, symbol="EURUSD")      # exactly 0.62
    assert e.get_active()[0]["state"] == OTEState.IN_BAND.value
    assert e.get_active()[0]["retracement"] == approx(0.62)

    e2 = engine()
    e2.analyze(bull_leg(1.08210), tf=15, symbol="EURUSD")     # exactly 0.79
    assert e2.get_active()[0]["state"] == OTEState.IN_BAND.value
    assert e2.get_active()[0]["retracement"] == approx(0.79)


def test_a_bearish_leg_prices_the_band_upwards():
    e = engine()
    e.analyze(bear_leg(1.08700), tf=15, symbol="EURUSD", zones=[{"high": 1.08800, "low": 1.08600}])
    rec = e.get_active()[0]
    assert rec["leg_direction"] == "BEARISH"
    assert rec["entry_zone_low"] == approx(1.08620)
    assert rec["entry_zone_high"] == approx(1.08790)
    assert rec["state"] == OTEState.IN_BAND.value


# ── the injected zone ───────────────────────────────────────────────────────

def test_a_zone_inside_the_band_makes_it_actionable():
    e = engine()
    e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[ZONE])
    rec = e.get_active()[0]
    assert rec["zone_overlap"] is True
    assert rec["actionable"] is True


def test_a_zone_away_from_the_band_leaves_it_not_actionable():
    e = engine()
    e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[FAR_ZONE])
    rec = e.get_active()[0]
    assert rec["state"] == OTEState.IN_BAND.value
    assert rec["zone_overlap"] is False
    assert rec["actionable"] is False


def test_a_malformed_zone_is_ignored_rather_than_crashing():
    e = engine()
    e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD",
              zones=[{"high": 1.08350}, None, {}, ZONE])
    assert e.get_active()[0]["zone_overlap"] is True


def test_zone_overlap_can_be_waived():
    e = engine(require_zone_overlap=False)
    e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD")
    rec = e.get_active()[0]
    assert rec["zone_overlap"] is False
    assert rec["actionable"] is True


def test_the_overlap_tolerance_widens_the_band():
    tight = engine()
    tight.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD",
                  zones=[{"high": 1.08420, "low": 1.08400}])
    assert tight.get_active()[0]["zone_overlap"] is False

    loose = engine(zone_overlap_tolerance_pips=10.0)
    loose.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD",
                  zones=[{"high": 1.08420, "low": 1.08400}])
    assert loose.get_active()[0]["zone_overlap"] is True


def test_shallow_is_never_actionable_even_with_a_zone():
    e = engine()
    e.analyze(bull_leg(1.08800), tf=15, symbol="EURUSD",
              zones=[{"high": 1.09000, "low": 1.08500}])
    assert e.get_active()[0]["actionable"] is False


# ── events ──────────────────────────────────────────────────────────────────

def test_the_first_look_at_a_market_publishes_the_zone():
    e = engine()
    events = e.analyze(bull_leg(1.08800), tf=15, symbol="EURUSD")
    assert [ev["event"] for ev in events] == [OTEEvent.OTE_ZONE_SET.value]


def test_entering_and_leaving_the_band_are_announced():
    e = engine()
    e.analyze(bull_leg(1.08800), tf=15, symbol="EURUSD")               # SHALLOW
    entered = e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD")     # IN_BAND
    assert [ev["event"] for ev in entered] == [OTEEvent.OTE_ENTERED.value]
    stayed = e.analyze(bull_leg(1.08350), tf=15, symbol="EURUSD")      # still IN_BAND
    assert stayed == []
    left = e.analyze(bull_leg(1.08800), tf=15, symbol="EURUSD")        # back to SHALLOW
    assert [ev["event"] for ev in left] == [OTEEvent.OTE_EXITED.value]


def test_running_straight_through_to_the_deep_side_counts_as_entering():
    e = engine()
    e.analyze(bull_leg(1.08800), tf=15, symbol="EURUSD")               # SHALLOW
    events = e.analyze(bull_leg(1.08150), tf=15, symbol="EURUSD")      # DEEP
    assert [ev["event"] for ev in events] == [OTEEvent.OTE_ENTERED.value]
    assert e.get_active()[0]["state"] == OTEState.DEEP.value


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_a_range_below_the_minimum_is_ignored():
    tiny = [bar(0, 1.08000, 1.08000, 1.07960, 1.07980),
            bar(1, 1.07980, 1.08000, 1.07960, 1.07980),
            bar(2, 1.07980, 1.08000, 1.07960, 1.07980)]
    e = engine()
    assert e.analyze(tiny, tf=15, symbol="EURUSD") == []
    assert e.get_active() == []

    ok = engine(min_range_pips=1.0)
    ok.analyze(tiny, tf=15, symbol="EURUSD")
    assert len(ok.get_active()) == 1


def test_insufficient_candles_returns_empty():
    assert engine().analyze([], tf=15, symbol="EURUSD") == []
    assert engine().analyze([bar(0, 1.08, 1.09, 1.07, 1.085)], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = engine(), engine()
    a.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[ZONE])
    b.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[ZONE])
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    e = engine()
    e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD")
    e.analyze(bear_leg(1.08700), tf=15, symbol="XAUUSD")
    assert len(e.get_all()) == 2


def test_reset_clears_everything():
    e = engine()
    e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[ZONE])
    e.reset()
    assert e.get_active() == []
    assert e.get_events() == []


def test_default_config_is_the_module_default():
    assert OTEEngine().config is DEFAULT_OTE_CONFIG


def test_the_zone_is_not_published_in_the_setup_dict():
    # zones are injected, never stored on the record — keeps the detector stateless about them
    e = engine()
    e.analyze(bull_leg(1.08300), tf=15, symbol="EURUSD", zones=[ZONE])
    assert "zones" not in e.get_active()[0]
