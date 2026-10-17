"""Tests for the inducement-resistant entry detector."""
from datetime import datetime, timezone

from forex.analysis.inducement_resistant import (
    DEFAULT_INDUCEMENT_RESISTANT_CONFIG,
    InducementResistantConfig,
    InducementResistantDirection,
    InducementResistantEngine,
    InducementResistantEvent,
    InducementResistantState,
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
    return InducementResistantEngine(InducementResistantConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


LONG_CANDLES = [
    bar(0, 1.0800, 1.0790, close=1.0795),
    bar(1, 1.0795, 1.0785, close=1.0788),
    bar(2, 1.0805, 1.0780, close=1.0802),
]

SHORT_CANDLES = [
    bar(0, 1.0810, 1.0795, close=1.0805),
    bar(1, 1.0820, 1.0808, close=1.0818),
    bar(2, 1.0825, 1.0790, close=1.0795),
]


def test_long_on_swept_low_reclaimed():
    eng = engine()
    events = eng.analyze(LONG_CANDLES, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [InducementResistantEvent.INDUCEMENT_RESISTANT_SETUP.value]
    rec = last(eng)
    assert rec["direction"] == InducementResistantDirection.LONG.value
    assert rec["state"] == InducementResistantState.RESISTANT.value


def test_short_on_swept_high_reclaimed():
    eng = engine()
    events = eng.analyze(SHORT_CANDLES, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [InducementResistantEvent.INDUCEMENT_RESISTANT_SETUP.value]
    rec = last(eng)
    assert rec["direction"] == InducementResistantDirection.SHORT.value


def test_created_at_absent():
    eng = engine()
    eng.analyze(LONG_CANDLES, tf=900, symbol="EURUSD")
    assert "created_at" not in last(eng)


def test_reset_clears():
    eng = engine()
    eng.analyze(LONG_CANDLES, tf=900, symbol="EURUSD")
    eng.reset()
    assert eng.get_all() == [] and eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_INDUCEMENT_RESISTANT_CONFIG.min_reclaim_pips == 2.0
    assert DEFAULT_INDUCEMENT_RESISTANT_CONFIG.pip_size == 0.0001
    assert engine().config == InducementResistantConfig()
