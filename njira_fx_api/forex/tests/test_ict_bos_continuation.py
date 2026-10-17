"""Tests for the BOS continuation detector."""
from datetime import datetime, timezone

from forex.analysis.bos_continuation import (
    DEFAULT_BOS_CONTINUATION_CONFIG,
    BOSContinuationConfig,
    BOSContinuationDirection,
    BOSContinuationEngine,
    BOSContinuationEvent,
    BOSContinuationState,
)


HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())


def bar(hour, high, low, close=None, open_=None, day=0):
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = low if open_ is None else open_
    close = open_ if close is None else close
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def engine(**overrides):
    return BOSContinuationEngine(BOSContinuationConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


def test_bullish_bos():
    candles = [
        bar(0, 1.0800, 1.0790, close=1.0795),
        bar(1, 1.0805, 1.0792, close=1.0800),
        bar(2, 1.0820, 1.0805, close=1.0818),
    ]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [BOSContinuationEvent.BOS_CONTINUATION_SETUP.value]
    rec = last(eng)
    assert rec["state"] == BOSContinuationState.BULLISH_BOS.value
    assert rec["direction"] == BOSContinuationDirection.LONG.value


def test_bearish_bos():
    candles = [
        bar(0, 1.0810, 1.0795, close=1.0805),
        bar(1, 1.0805, 1.0790, close=1.0795),
        bar(2, 1.0790, 1.0775, close=1.0778),
    ]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [BOSContinuationEvent.BOS_CONTINUATION_SETUP.value]
    rec = last(eng)
    assert rec["state"] == BOSContinuationState.BEARISH_BOS.value
    assert rec["direction"] == BOSContinuationDirection.SHORT.value


def test_created_at_absent():
    candles = [
        bar(0, 1.0800, 1.0790, close=1.0795),
        bar(1, 1.0805, 1.0792, close=1.0800),
        bar(2, 1.0820, 1.0805, close=1.0818),
    ]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    assert "created_at" not in last(eng)


def test_reset_clears():
    candles = [
        bar(0, 1.0800, 1.0790, close=1.0795),
        bar(1, 1.0805, 1.0792, close=1.0800),
        bar(2, 1.0820, 1.0805, close=1.0818),
    ]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    eng.reset()
    assert eng.get_all() == [] and eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_BOS_CONTINUATION_CONFIG.lookback == 5
    assert DEFAULT_BOS_CONTINUATION_CONFIG.pip_size == 0.0001
    assert engine().config == BOSContinuationConfig()
