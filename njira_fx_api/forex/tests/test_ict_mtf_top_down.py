"""Tests for the MTF top-down detector."""
from datetime import datetime, timezone

from forex.analysis.mtf_top_down import (
    DEFAULT_MTF_TOP_DOWN_CONFIG,
    MtfTopDownConfig,
    MtfTopDownDirection,
    MtfTopDownEngine,
    MtfTopDownEvent,
    MtfTopDownState,
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
    return MtfTopDownEngine(MtfTopDownConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


def test_aligned_long():
    candles = [
        bar(0, 1.0790, 1.0780, close=1.0785, open_=1.0782),
        bar(1, 1.0795, 1.0785, close=1.0790, open_=1.0788),
        bar(2, 1.0810, 1.0795, close=1.0808, open_=1.0800),
        bar(3, 1.0815, 1.0805, close=1.0812, open_=1.0808),
    ]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [MtfTopDownEvent.MTF_TOP_DOWN_SETUP.value]
    rec = last(eng)
    assert rec["state"] == MtfTopDownState.ALIGNED_LONG.value
    assert rec["aligned"] is True
    assert rec["direction"] == MtfTopDownDirection.LONG.value


def test_conflict():
    candles = [
        bar(0, 1.0810, 1.0795, close=1.0808, open_=1.0800),
        bar(1, 1.0805, 1.0790, close=1.0795, open_=1.0802),
        bar(2, 1.0790, 1.0780, close=1.0782, open_=1.0788),
        bar(3, 1.0785, 1.0775, close=1.0778, open_=1.0782),
    ]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [MtfTopDownEvent.MTF_TOP_DOWN_SETUP.value]
    rec = last(eng)
    assert rec["state"] == MtfTopDownState.ALIGNED_SHORT.value
    assert rec["aligned"] is True


def test_created_at_absent():
    candles = [
        bar(0, 1.0790, 1.0780, close=1.0785, open_=1.0782),
        bar(1, 1.0795, 1.0785, close=1.0790, open_=1.0788),
        bar(2, 1.0810, 1.0795, close=1.0808, open_=1.0800),
        bar(3, 1.0815, 1.0805, close=1.0812, open_=1.0808),
    ]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    assert "created_at" not in last(eng)


def test_reset_clears():
    candles = [
        bar(0, 1.0790, 1.0780, close=1.0785, open_=1.0782),
        bar(1, 1.0795, 1.0785, close=1.0790, open_=1.0788),
        bar(2, 1.0810, 1.0795, close=1.0808, open_=1.0800),
        bar(3, 1.0815, 1.0805, close=1.0812, open_=1.0808),
    ]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    eng.reset()
    assert eng.get_all() == [] and eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_MTF_TOP_DOWN_CONFIG.htf_ratio == 0.5
    assert DEFAULT_MTF_TOP_DOWN_CONFIG.pip_size == 0.0001
    assert engine().config == MtfTopDownConfig()
