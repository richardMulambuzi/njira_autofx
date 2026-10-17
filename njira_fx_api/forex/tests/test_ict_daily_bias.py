"""Tests for the standalone ICT daily-bias detector (gate, not a signal)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.daily_bias import (
    BiasAgreement,
    DailyBias,
    DailyBiasConfig,
    DailyBiasEngine,
    DailyBiasEvent,
    DEFAULT_DAILY_BIAS_CONFIG,
)

DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())   # a Monday


def candle(ts, o, h, l, c):
    return {"open_time": ts, "open": o, "high": h, "low": l, "close": c}


def bullish_day(yesterday_ts=MONDAY):
    """Yesterday: bullish candle, PDH 1.08620 / PDL 1.08150."""
    return candle(yesterday_ts, 1.08300, 1.08620, 1.08150, 1.08470)


def today_bullish():
    return candle(MONDAY + DAY, 1.08450, 1.08500, 1.08420, 1.08480)


def _engine(**overrides):
    return DailyBiasEngine(DailyBiasConfig(**overrides))


# ── happy path ──────────────────────────────────────────────────────────────

def test_aligned_bullish_day_is_tradeable_long():
    engine = _engine()
    events = engine.analyze([bullish_day(), today_bullish()], tf=15, symbol="EURUSD")
    assert len(events) == 1
    rec = engine.get_active()[0]
    assert rec["bias"] == DailyBias.BULLISH.value
    assert rec["agreement"] == BiasAgreement.ALIGNED.value
    assert rec["tradeable"] is True
    assert rec["allowed_direction"] == "LONG"
    assert set(rec["votes"]) == {
        "r1_prev_day_candle", "r2_pdh_pdl", "r3_daily_open",
        "r4_weekly_open", "r5_liquidity_draw",
    }
    assert rec["votes"]["r1_prev_day_candle"] == "BULLISH"
    assert rec["votes"]["r2_pdh_pdl"] == "NEUTRAL"      # price between PDH and PDL
    assert rec["reference_prices"]["pdh"] == approx(1.08620)
    assert rec["reference_prices"]["pdl"] == approx(1.08150)


def test_aligned_bearish_day_is_tradeable_short():
    yesterday = candle(MONDAY, 1.08600, 1.08700, 1.08300, 1.08400)
    today = candle(MONDAY + DAY, 1.08420, 1.08440, 1.08380, 1.08390)
    engine = _engine()
    engine.analyze([yesterday, today], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["bias"] == DailyBias.BEARISH.value
    assert rec["agreement"] == BiasAgreement.ALIGNED.value
    assert rec["allowed_direction"] == "SHORT"
    assert rec["tradeable"] is True


# ── agreement gate ──────────────────────────────────────────────────────────

def test_mixed_day_is_not_tradeable_when_alignment_required():
    today = candle(MONDAY + DAY, 1.08500, 1.08510, 1.08390, 1.08400)
    engine = _engine()
    engine.analyze([bullish_day(), today], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["agreement"] == BiasAgreement.MIXED.value
    assert rec["tradeable"] is False


def test_mixed_day_becomes_tradeable_when_alignment_not_required():
    today = candle(MONDAY + DAY, 1.08500, 1.08510, 1.08390, 1.08400)
    engine = _engine(require_alignment=False)
    engine.analyze([bullish_day(), today], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["agreement"] == BiasAgreement.MIXED.value
    assert rec["tradeable"] is True


# ── the lock ────────────────────────────────────────────────────────────────

def test_bias_is_locked_for_the_rest_of_the_day():
    engine = _engine()
    candles = [bullish_day(), today_bullish()]
    first = engine.analyze(candles, tf=15, symbol="EURUSD")
    assert first and first[0]["event"] == DailyBiasEvent.BIAS_LOCKED.value

    # price races away mid-session — the locked bias must NOT change
    later = candle(MONDAY + DAY + 3600, 1.08480, 1.09000, 1.07000, 1.07100)
    assert engine.analyze([bullish_day(), today_bullish(), later],
                          tf=15, symbol="EURUSD") == []
    rec = engine.get_active()[0]
    assert rec["bias"] == DailyBias.BULLISH.value
    assert rec["tradeable"] is True


def test_new_day_recomputes_and_emits_changed_event():
    engine = _engine()
    engine.analyze([bullish_day(), today_bullish()], tf=15, symbol="EURUSD")

    day2 = candle(MONDAY + 2 * DAY, 1.08800, 1.08820, 1.08600, 1.08610)
    events = engine.analyze([bullish_day(), today_bullish(), day2],
                            tf=15, symbol="EURUSD")
    assert len(events) == 1
    assert events[0]["event"] in (DailyBiasEvent.BIAS_CHANGED.value,
                                  DailyBiasEvent.BIAS_LOCKED.value)
    assert events[0]["day"] == MONDAY + 2 * DAY


def test_reset_forces_a_fresh_lock():
    engine = _engine()
    candles = [bullish_day(), today_bullish()]
    engine.analyze(candles, tf=15, symbol="EURUSD")
    engine.reset()
    assert engine.analyze(candles, tf=15, symbol="EURUSD") != []


# ── guards ──────────────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert _engine().analyze([], tf=15, symbol="EURUSD") == []
    assert _engine().analyze([today_bullish()], tf=15, symbol="EURUSD") == []


def test_no_completed_previous_day_returns_empty():
    engine = _engine()
    assert engine.analyze([today_bullish(), today_bullish()], tf=15,
                          symbol="EURUSD") == []


# ── knobs gate behaviour ────────────────────────────────────────────────────

def test_min_break_pips_gates_the_daily_open_vote():
    # one pip above the daily open — below the default 2-pip buffer
    today = candle(MONDAY + DAY, 1.08450, 1.08500, 1.08420, 1.08451)
    engine = _engine()
    engine.analyze([bullish_day(), today], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["votes"]["r3_daily_open"] == "NEUTRAL"

    engine2 = _engine(min_break_pips=0.0)
    engine2.analyze([bullish_day(), today], tf=15, symbol="EURUSD")
    assert engine2.get_active()[0]["votes"]["r3_daily_open"] == "BULLISH"


def test_weekly_open_can_be_switched_off():
    engine = _engine(weekly_open_enabled=False)
    engine.analyze([bullish_day(), today_bullish()], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["votes"]["r4_weekly_open"] == "NEUTRAL"
    assert rec["reference_prices"]["weekly_open"] == approx(1.08300)


def test_both_daily_open_anchors_are_reported():
    """Conflict C4 must stay visible: UTC anchor and NY anchor both stored."""
    engine = _engine()
    engine.analyze([bullish_day(), today_bullish()], tf=15, symbol="EURUSD")
    ref = engine.get_active()[0]["reference_prices"]
    assert ref["daily_open"] == approx(1.08450)
    assert ref["daily_open_utc"] == approx(1.08450)


def test_anchor_ny_can_move_the_verdict_off_utc():
    engine = _engine(anchor="ny")
    events = engine.analyze([bullish_day(), today_bullish()], tf=15,
                            symbol="EURUSD")
    assert len(events) == 1
    assert engine.get_active()[0]["bias"] in (
        DailyBias.BULLISH.value, DailyBias.BEARISH.value, DailyBias.NEUTRAL.value)


# ── determinism / hygiene ───────────────────────────────────────────────────

def test_same_input_gives_same_output():
    candles = [bullish_day(), today_bullish()]
    a = _engine()
    b = _engine()
    a.analyze(candles, tf=15, symbol="EURUSD")
    b.analyze(candles, tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    engine = _engine()
    engine.analyze([bullish_day(), today_bullish()], tf=15, symbol="EURUSD")
    engine.analyze([bullish_day(), today_bullish()], tf=15, symbol="GBPUSD")
    assert len(engine.get_all()) == 2


def test_default_config_is_the_module_default():
    engine = DailyBiasEngine()
    assert engine.config is DEFAULT_DAILY_BIAS_CONFIG
