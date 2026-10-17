"""Tests for the premium/discount detector (ICT register item §18).

Synthetic candles only — no fixtures, no network, no DB.
"""
from __future__ import annotations

from pytest import approx

from forex.analysis.premium_discount import (
    DEFAULT_PREMIUM_DISCOUNT_CONFIG,
    PDEvent,
    PDPosition,
    PremiumDiscountConfig,
    PremiumDiscountEngine,
    dealing_range,
    in_band,
    retracement_of,
)


def _candles(closes: list[float], highs: list[float] | None = None,
             lows: list[float] | None = None, tf: int = 60) -> list[dict]:
    highs = highs or [c + 0.0002 for c in closes]
    lows = lows or [c - 0.0002 for c in closes]
    return [
        {
            "open_time": i * tf,
            "open": c,
            "high": h,
            "low": l,
            "close": c,
            "volume": 1.0,
            "timeframe": tf,
        }
        for i, (c, h, l) in enumerate(zip(closes, highs, lows))
    ]


def _bullish_leg(last_close: float) -> list[dict]:
    """Low 1.1000 early, high 1.1100 late, then one final bar closing at last_close."""
    closes = [1.1010 + 0.0004 * i for i in range(19)] + [last_close]
    highs = [c + 0.0001 for c in closes]
    lows = [c - 0.0001 for c in closes]
    highs[0] = 1.1012
    lows[0] = 1.1000
    highs[18] = 1.1100
    lows[18] = 1.1098
    highs[19] = max(last_close, 1.1099)
    return _candles(closes, highs, lows)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_dealing_range_identifies_bullish_leg():
    low, high, leg = dealing_range(_bullish_leg(1.1080), DEFAULT_PREMIUM_DISCOUNT_CONFIG)
    assert low == 1.1000
    assert high == 1.1100
    assert leg == "BULLISH"


def test_retracement_endpoints():
    assert retracement_of(1.1100, 1.1000, 1.1100, "BULLISH") == approx(0.0)
    assert retracement_of(1.1000, 1.1000, 1.1100, "BULLISH") == approx(1.0)
    assert retracement_of(1.1050, 1.1000, 1.1100, "BULLISH") == approx(0.5)
    assert retracement_of(1.1050, 1.1000, 1.1100, "BEARISH") == approx(0.5)


def test_in_band_boundaries_inclusive():
    assert in_band(0.50, 0.50, 0.618) is True
    assert in_band(0.618, 0.50, 0.618) is True
    assert in_band(0.619, 0.50, 0.618) is False


# ── engine: happy paths ─────────────────────────────────────────────────────

def test_premium_when_price_near_leg_high():
    engine = PremiumDiscountEngine()
    engine.analyze(_bullish_leg(1.1090), 60, symbol="EURUSD")
    state = engine.get_active(1)[0]
    assert state["position"] == "PREMIUM"
    assert state["leg_direction"] == "BULLISH"
    assert state["retracement"] == approx(0.1)


def test_discount_when_price_near_leg_low():
    engine = PremiumDiscountEngine()
    engine.analyze(_bullish_leg(1.1015), 60, symbol="EURUSD")
    assert engine.get_active(1)[0]["position"] == "DISCOUNT"


def test_equilibrium_at_midpoint():
    engine = PremiumDiscountEngine()
    engine.analyze(_bullish_leg(1.1050), 60, symbol="EURUSD")
    state = engine.get_active(1)[0]
    assert state["position"] == "EQUILIBRIUM"
    assert state["in_active_band"] is True          # 0.50 sits in the litefinance band


# ── engine: no-signal paths ─────────────────────────────────────────────────

def test_no_data_returns_nothing():
    engine = PremiumDiscountEngine()
    assert engine.analyze([], 60) == []
    assert engine.analyze(_candles([1.1])[:1], 60) == []


def test_flat_range_rejected():
    flat = _candles([1.1000] * 10, highs=[1.1000] * 10, lows=[1.1000] * 10)
    assert PremiumDiscountEngine().analyze(flat, 60) == []


def test_min_range_pips_gates_out_tiny_range():
    # span = 0.0001 = 1 pip, below min_range_pips (5)
    tiny = _candles([1.1000] * 10, highs=[1.10005] * 10, lows=[1.09995] * 10)
    assert PremiumDiscountEngine().analyze(tiny, 60) == []


# ── config thresholds actually gate behaviour ───────────────────────────────

def test_equilibrium_tolerance_gates_zone():
    candles = _bullish_leg(1.1050 + 0.00009)      # just above the midpoint
    loose = PremiumDiscountEngine(
        PremiumDiscountConfig(equilibrium_tolerance_pct=0.02))
    tight = PremiumDiscountEngine(
        PremiumDiscountConfig(equilibrium_tolerance_pct=0.0))
    loose.analyze(candles, 60, symbol="EURUSD")
    tight.analyze(candles, 60, symbol="EURUSD")
    assert loose.get_active(1)[0]["position"] == "EQUILIBRIUM"
    assert tight.get_active(1)[0]["position"] == "PREMIUM"


def test_active_band_selector_switches_reported_band():
    candles = _bullish_leg(1.1030)                # retracement ~0.70
    lite = PremiumDiscountEngine(
        PremiumDiscountConfig(active_band="litefinance"))
    road = PremiumDiscountEngine(PremiumDiscountConfig(active_band="roadmap"))
    lite.analyze(candles, 60, symbol="EURUSD")
    road.analyze(candles, 60, symbol="EURUSD")
    assert lite.get_active(1)[0]["in_active_band"] is False
    assert road.get_active(1)[0]["in_active_band"] is True
    # both bands are always reported, whichever is primary
    assert road.get_active(1)[0]["in_litefinance_band"] is False
    assert lite.get_active(1)[0]["in_roadmap_band"] is True


# ── lifecycle / events ──────────────────────────────────────────────────────

def test_zone_change_and_ote_events():
    engine = PremiumDiscountEngine()
    first = engine.analyze(_bullish_leg(1.1090), 60, symbol="EURUSD")
    assert first[0]["event"] == PDEvent.RANGE_LOCKED.value

    second = engine.analyze(_bullish_leg(1.1050), 60, symbol="EURUSD")
    names = {e["event"] for e in second}
    assert PDEvent.ZONE_CHANGED.value in names
    assert PDEvent.OTE_ENTERED.value in names

    third = engine.analyze(_bullish_leg(1.1090), 60, symbol="EURUSD")
    assert PDEvent.OTE_EXITED.value in {e["event"] for e in third}


def test_lifecycle_length_and_reset():
    engine = PremiumDiscountEngine()
    for price in (1.1090, 1.1050, 1.1010, 1.1050, 1.1090, 1.1080, 1.1090):
        engine.analyze(_bullish_leg(price), 60, symbol="EURUSD")
    assert len(engine.get_events(100)) <= 50
    engine.reset()
    assert engine.get_events(10) == []
    assert engine.get_active() == []


# ── determinism ─────────────────────────────────────────────────────────────

def test_deterministic_same_input_same_output():
    candles = _bullish_leg(1.1052)
    a, b = PremiumDiscountEngine(), PremiumDiscountEngine()
    assert a.analyze(candles, 60, symbol="EURUSD") == b.analyze(candles, 60, symbol="EURUSD")
    assert a.get_active() == b.get_active()


def test_per_symbol_state_is_separate():
    engine = PremiumDiscountEngine()
    engine.analyze(_bullish_leg(1.1090), 60, symbol="EURUSD")
    engine.analyze(_bullish_leg(1.1015), 60, symbol="XAUUSD")
    assert len(engine.get_active(10)) == 2
