"""Tests for the standalone ICT Judas-swing detector (fake move at a session open)."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.judas_swing import (
    DEFAULT_JUDAS_SWING_CONFIG,
    JudasState,
    JudasSwingConfig,
    JudasSwingEngine,
    JudasSwingEvent,
)

HOUR = 3600
QUARTER = 900
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())   # a Monday
LONDON = MONDAY + 7 * HOUR      # 07:00 UTC — London session open


def candle(ts, o, h, l, c):
    return {"open_time": ts, "open": o, "high": h, "low": l, "close": c}


def bar(i, o, h, l, c, base=LONDON):
    return candle(base + i * QUARTER, o, h, l, c)


def _engine(**overrides):
    return JudasSwingEngine(JudasSwingConfig(**overrides))


def bullish_judas():
    """Session opens 1.08400; price fakes DOWN to 1.08250 then closes back above."""
    return [
        bar(0, 1.08400, 1.08420, 1.08360, 1.08390),
        bar(1, 1.08390, 1.08400, 1.08250, 1.08280),   # the fake move down
        bar(2, 1.08280, 1.08450, 1.08270, 1.08430),   # reclaimed the open
    ]


def bearish_judas():
    """Session opens 1.08400; price fakes UP to 1.08560 then closes back below."""
    return [
        bar(0, 1.08400, 1.08430, 1.08380, 1.08410),
        bar(1, 1.08410, 1.08560, 1.08400, 1.08530),   # the fake move up
        bar(2, 1.08530, 1.08540, 1.08370, 1.08380),   # reclaimed the open
    ]


# ── detection ───────────────────────────────────────────────────────────────

def test_fake_down_then_reclaim_is_bullish_judas():
    engine = _engine()
    events = engine.analyze(bullish_judas(), tf=15, symbol="EURUSD")
    assert len(events) == 1
    assert events[0]["event"] == JudasSwingEvent.JUDAS_DETECTED.value

    rec = engine.get_active()[0]
    assert rec["state"] == JudasState.BULLISH_JUDAS.value
    assert rec["session"] == "london"
    assert rec["extreme_side"] == "DOWN"
    assert rec["session_open_price"] == approx(1.08400)
    assert rec["extreme_price"] == approx(1.08250)
    assert rec["thrust_pips"] == approx(15.0)
    assert rec["reclaimed"] is True
    assert rec["reclaim_price"] == approx(1.08430)


def test_fake_up_then_reclaim_is_bearish_judas():
    engine = _engine()
    engine.analyze(bearish_judas(), tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["state"] == JudasState.BEARISH_JUDAS.value
    assert rec["extreme_side"] == "UP"
    assert rec["extreme_price"] == approx(1.08560)
    assert rec["thrust_pips"] == approx(16.0)
    assert rec["reclaim_price"] == approx(1.08380)


def test_a_thrust_without_reclaim_is_only_pending():
    engine = _engine()
    engine.analyze(bullish_judas()[:2], tf=15, symbol="EURUSD")
    rec = engine.get_active()[0]
    assert rec["state"] == JudasState.PENDING.value
    assert rec["thrust_pips"] == approx(0.0)     # nothing detected, so nothing claimed


def test_no_reclaim_within_the_window_is_no_judas():
    engine = _engine(window_bars=1)
    engine.analyze(bullish_judas()[:2], tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == JudasState.NO_JUDAS.value


def test_a_thrust_below_the_threshold_stays_pending():
    engine = _engine(min_thrust_pips=30.0)
    engine.analyze(bullish_judas(), tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == JudasState.PENDING.value


def test_reclaim_can_be_measured_on_the_wick():
    candles = bullish_judas()
    candles[2] = bar(2, 1.08280, 1.08450, 1.08270, 1.08300)   # closed below the open

    body_mode = _engine()
    events = body_mode.analyze(candles, tf=15, symbol="EURUSD")
    assert [e["event"] for e in events] == [JudasSwingEvent.JUDAS_PENDING.value]
    assert body_mode.get_active()[0]["state"] == JudasState.PENDING.value

    engine = _engine(reclaim_mode="wick")
    engine.analyze(candles, tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["state"] == JudasState.BULLISH_JUDAS.value


# ── session selection ───────────────────────────────────────────────────────

def test_no_watched_session_open_means_silence():
    asian = [candle(MONDAY + 3 * HOUR, 1.08400, 1.08420, 1.08380, 1.08410),
             candle(MONDAY + 3 * HOUR + QUARTER, 1.08410, 1.08430, 1.08400, 1.08420)]
    engine = _engine()
    assert engine.analyze(asian, tf=15, symbol="EURUSD") == []
    assert engine.get_active() == []


def test_the_most_recently_opened_session_wins():
    """New York opens inside London's window — it must supersede, not hide behind it."""
    candles = list(bullish_judas())
    candles.append(bar(3, 1.08430, 1.08440, 1.08410, 1.08420, base=LONDON + 2 * HOUR))
    candles.append(candle(MONDAY + 12 * HOUR, 1.08600, 1.08620, 1.08540, 1.08610))
    candles.append(candle(MONDAY + 12 * HOUR + QUARTER, 1.08610, 1.08690, 1.08600, 1.08670))

    engine = _engine()
    engine.analyze(candles, tf=15, symbol="EURUSD")
    assert engine.get_active()[0]["session"] == "newyork"
    assert engine.get_active()[0]["session_open_price"] == approx(1.08600)


def test_sessions_watched_is_configurable():
    engine = _engine(sessions_watched=("sydney",))
    assert engine.analyze(bullish_judas(), tf=15, symbol="EURUSD") == []


# ── events ──────────────────────────────────────────────────────────────────

def test_pending_is_announced_once_per_session():
    engine = _engine()
    events = engine.analyze(bullish_judas()[:2], tf=15, symbol="EURUSD")
    assert events[0]["event"] == JudasSwingEvent.JUDAS_PENDING.value
    assert engine.analyze(bullish_judas()[:2], tf=15, symbol="EURUSD") == []


def test_detection_then_a_new_session_emits_again():
    engine = _engine()
    engine.analyze(bullish_judas(), tf=15, symbol="EURUSD")

    candles = list(bullish_judas())
    candles.append(candle(MONDAY + 12 * HOUR, 1.08600, 1.08620, 1.08540, 1.08610))
    events = engine.analyze(candles, tf=15, symbol="EURUSD")
    assert len(events) == 1
    assert events[0]["session"] == "newyork"


# ── guards / hygiene ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_empty():
    assert _engine().analyze([], tf=15, symbol="EURUSD") == []
    assert _engine().analyze(bullish_judas()[:1], tf=15, symbol="EURUSD") == []


def test_same_input_gives_same_output():
    a, b = _engine(), _engine()
    a.analyze(bearish_judas(), tf=15, symbol="EURUSD")
    b.analyze(bearish_judas(), tf=15, symbol="EURUSD")
    assert a.get_active()[0] == b.get_active()[0]


def test_state_is_isolated_per_symbol():
    engine = _engine()
    engine.analyze(bullish_judas(), tf=15, symbol="EURUSD")
    engine.analyze(bearish_judas(), tf=15, symbol="XAUUSD")
    assert len(engine.get_all()) == 2


def test_reset_clears_everything():
    engine = _engine()
    engine.analyze(bullish_judas(), tf=15, symbol="EURUSD")
    engine.reset()
    assert engine.get_active() == []
    assert engine.get_events() == []


def test_default_config_is_the_module_default():
    assert JudasSwingEngine().config is DEFAULT_JUDAS_SWING_CONFIG
