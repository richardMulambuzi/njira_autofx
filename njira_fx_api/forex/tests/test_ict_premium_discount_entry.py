"""Tests for the premium/discount entry detector."""
from datetime import datetime, timezone

from forex.analysis.premium_discount_entry import (
    DEFAULT_PREMIUM_DISCOUNT_ENTRY_CONFIG,
    PremiumDiscountEntryConfig,
    PremiumDiscountEntryDirection,
    PremiumDiscountEntryEngine,
    PremiumDiscountEntryEvent,
    PremiumDiscountEntryState,
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
    return PremiumDiscountEntryEngine(PremiumDiscountEntryConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


def test_discount_gives_long():
    candles = [bar(0, 1.0810, 1.0790, close=1.0795), bar(1, 1.0810, 1.0790, close=1.0795), bar(2, 1.0810, 1.0790, close=1.0795)]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [PremiumDiscountEntryEvent.PD_ENTRY_SETUP.value]
    rec = last(eng)
    assert rec["state"] == PremiumDiscountEntryState.DISCOUNT.value
    assert rec["direction"] == PremiumDiscountEntryDirection.LONG.value


def test_premium_gives_short():
    candles = [bar(0, 1.0810, 1.0790, close=1.0808), bar(1, 1.0810, 1.0790, close=1.0808), bar(2, 1.0810, 1.0790, close=1.0808)]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [PremiumDiscountEntryEvent.PD_ENTRY_SETUP.value]
    rec = last(eng)
    assert rec["state"] == PremiumDiscountEntryState.PREMIUM.value
    assert rec["direction"] == PremiumDiscountEntryDirection.SHORT.value


def test_created_at_absent():
    candles = [bar(0, 1.0810, 1.0790, close=1.0795), bar(1, 1.0810, 1.0790, close=1.0795), bar(2, 1.0810, 1.0790, close=1.0795)]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    assert "created_at" not in last(eng)


def test_reset_clears():
    candles = [bar(0, 1.0810, 1.0790, close=1.0795)]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    eng.reset()
    assert eng.get_all() == [] and eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_PREMIUM_DISCOUNT_ENTRY_CONFIG.lookback == 20
    assert DEFAULT_PREMIUM_DISCOUNT_ENTRY_CONFIG.pip_size == 0.0001
    assert engine().config == PremiumDiscountEntryConfig()
