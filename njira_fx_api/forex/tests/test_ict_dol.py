"""Tests for the standalone ICT draw-on-liquidity detector (the target side)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.dol import (
    DEFAULT_DOL_CONFIG,
    DOLConfig,
    DOLDirection,
    DOLEngine,
    DOLEvent,
    PoolKind,
)

DAY = 86400
HOUR = 3600
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())   # a Monday


def candle(ts, o, h, l, c):
    return {"open_time": ts, "open": o, "high": h, "low": l, "close": c}


def _engine(**overrides):
    return DOLEngine(DOLConfig(**overrides))


def up_scenario():
    """PDH 1.08620 is the nearest un-swept pool; price 1.08490."""
    return [
        candle(MONDAY - 7 * DAY, 1.08500, 1.09000, 1.08000, 1.08600),  # last week
        candle(MONDAY - DAY, 1.08300, 1.08620, 1.08150, 1.08470),      # yesterday
        candle(MONDAY, 1.08450, 1.08500, 1.08420, 1.08460),
        candle(MONDAY + HOUR, 1.08460, 1.08480, 1.08430, 1.08470),
        candle(MONDAY + 2 * HOUR, 1.08470, 1.08600, 1.08450, 1.08580),
        candle(MONDAY + 3 * HOUR, 1.08580, 1.08590, 1.08480, 1.08490),
    ]


def down_scenario():
    """PDL 1.07900 is the nearest un-swept pool; price 1.07950."""
    return [
        candle(MONDAY - 7 * DAY, 1.08500, 1.09000, 1.07500, 1.08600),  # last week
        candle(MONDAY - DAY, 1.08300, 1.08400, 1.07900, 1.08000),      # yesterday
        candle(MONDAY, 1.07950, 1.07980, 1.07930, 1.07960),
        candle(MONDAY + HOUR, 1.07960, 1.07980, 1.07940, 1.07950),
    ]


# ── the draw ────────────────────────────────────────────────────────────────

def test_nearest_buy_side_pool_is_the_target():
    engine = _engine()
    events = engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    assert len(events) == 1
    rec = engine.get_active()[0]
    assert rec["direction"] == DOLDirection.UP.value
    assert rec["target_kind"] == PoolKind.PDH.value
    assert rec["target_price"] == approx(1.08620)
    assert rec["distance_pips"] == approx(13.0)


def test_nearest_sell_side_pool_is_the_target():
    engine = _engine()
    engine.analyze(down_scenario(), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["direction"] == DOLDirection.DOWN.value
    assert rec["target_kind"] == PoolKind.PDL.value
    assert rec["distance_pips"] == approx(5.0)


def test_targets_are_ranked_nearest_first():
    engine = _engine()
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    distances = [t["distance_pips"] for t in engine.get_active()[0]["targets"]]
    assert distances == sorted(distances)
    assert len(distances) >= 2


def test_no_pools_at_all_is_neutral():
    engine = _engine(pool_lookback=2)
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["direction"] == DOLDirection.NEUTRAL.value
    assert rec["target_kind"] == ""
    assert rec["targets"] == []


# ── sweep semantics ─────────────────────────────────────────────────────────

def test_a_pool_taken_out_after_it_formed_is_dropped():
    engine = _engine()
    candles = down_scenario()
    # price knifes through PDL — that pool is consumed, the draw must move on
    candles.append(candle(MONDAY + 2 * HOUR, 1.07950, 1.07960, 1.07800, 1.07820))
    engine.analyze(candles, tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert PoolKind.PDL.value not in [t["kind"] for t in rec["targets"]]


def test_last_weeks_high_does_not_sweep_this_weeks_pools():
    """The regression that matters: an old extreme must not erase today's magnets."""
    engine = _engine()
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    kinds = [t["kind"] for t in rec["targets"]]
    assert PoolKind.PDH.value in kinds              # yesterday's high survives
    assert rec["target_kind"] == PoolKind.PDH.value  # and it is the nearest magnet


# ── knobs ───────────────────────────────────────────────────────────────────

def test_min_distance_pips_filters_close_pools():
    assert _engine().analyze(up_scenario(), tf=15, symbol="EURUSD")
    rec = _engine(min_distance_pips=20.0)
    rec.analyze(up_scenario(), tf=15, symbol="EURUSD")
    assert rec.get_active()[0]["target_kind"] != PoolKind.PDH.value


def test_weekly_pools_can_be_switched_off():
    engine = _engine(include_weekly=False)
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    kinds = [t["kind"] for t in engine.get_active()[0]["targets"]]
    assert PoolKind.PWH.value not in kinds
    assert PoolKind.PWL.value not in kinds


def test_session_extremes_are_off_by_default_and_can_be_turned_on():
    engine = _engine()
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    assert PoolKind.TODAY_LOW.value not in [t["kind"] for t in
                                            engine.get_active()[0]["targets"]]

    engine2 = _engine(include_session_extremes=True)
    engine2.analyze(up_scenario(), tf=15, symbol="EURUSD")
    rec = engine2.get_active()[0]
    assert rec["target_kind"] == PoolKind.TODAY_LOW.value   # 7 pips beats PDH's 13
    assert rec["direction"] == DOLDirection.DOWN.value


def test_equal_highs_cluster_into_one_pool():
    highs = [1.0800, 1.0820, 1.0850, 1.0820, 1.0810,
             1.0820, 1.08505, 1.0820, 1.0810, 1.0840]
    candles = [candle(MONDAY + i * HOUR, h, h, h - 0.0010, h)
               for i, h in enumerate(highs)]
    candles[-1] = candle(MONDAY + 9 * HOUR, 1.0830, 1.0840, 1.0830, 1.0835)

    engine = _engine(swing_lookback=2, include_weekly=False)
    engine.analyze(candles, tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert PoolKind.EQUAL_HIGH.value in [t["kind"] for t in rec["targets"]]
    assert rec["direction"] == DOLDirection.UP.value

    equal_high = next(t for t in rec["targets"]
                      if t["kind"] == PoolKind.EQUAL_HIGH.value)
    assert equal_high["price"] == approx(1.085025, abs=1e-6)   # 1.08500 and 1.08505 merged
    assert equal_high["distance_pips"] == approx(15.25, abs=0.01)


def test_max_targets_caps_the_list():
    engine = _engine(max_targets=2)
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    assert len(engine.get_active()[0]["targets"]) == 2


# ── events ──────────────────────────────────────────────────────────────────

def test_first_analysis_emits_target_set():
    events = _engine().analyze(up_scenario(), tf=15, symbol="EURUSD")
    assert events[0]["event"] == DOLEvent.TARGET_SET.value


def test_flipping_direction_emits_target_changed():
    engine = _engine()
    candles = up_scenario()
    engine.analyze(candles, tf=15, symbol="EURUSD")

    candles.append(candle(MONDAY + 4 * HOUR, 1.08490, 1.08490, 1.08160, 1.08170))
    events = engine.analyze(candles, tf=15, symbol="EURUSD")
    assert len(events) == 1
    assert events[0]["event"] == DOLEvent.TARGET_CHANGED.value
    assert events[0]["previous"] == DOLDirection.UP.value
    assert events[0]["direction"] == DOLDirection.DOWN.value


def test_unchanged_draw_emits_nothing():
    engine = _engine()
    candles = up_scenario()
    engine.analyze(candles, tf=15, symbol="EURUSD")
    candles.append(candle(MONDAY + 4 * HOUR, 1.08490, 1.08500, 1.08470, 1.08492))
    assert engine.analyze(candles, tf=15, symbol="EURUSD") == []


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert _engine().analyze([], tf=15, symbol="EURUSD") == []
    assert _engine().analyze(up_scenario()[:1], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = _engine(), _engine()
    a.analyze(up_scenario(), tf=15, symbol="EURUSD")
    b.analyze(up_scenario(), tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    engine = _engine()
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    engine.analyze(down_scenario(), tf=15, symbol="XAUUSD")
    assert len(engine.get_all()) == 2
    assert [r["direction"] for r in engine.get_all()] == [
        DOLDirection.UP.value, DOLDirection.DOWN.value]


def test_reset_clears_everything():
    engine = _engine()
    engine.analyze(up_scenario(), tf=15, symbol="EURUSD")
    engine.reset()
    assert engine.get_active() == []
    assert engine.get_events() == []


def test_default_config_is_the_module_default():
    assert DOLEngine().config is DEFAULT_DOL_CONFIG
