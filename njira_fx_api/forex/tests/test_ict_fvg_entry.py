"""Tests for the FVG entry detector."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.fvg_entry import (
    DEFAULT_FVG_ENTRY_CONFIG,
    FVGEntryConfig,
    FVGEntryDirection,
    FVGEntryEngine,
    FVGEntryEvent,
    FVGEntryState,
)


HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())


def bar(hour, high, low, close=None, open_=None, day=0):
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = low if open_ is None else open_
    close = open_ if close is None else close
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def flat(hour, price, day=0):
    return bar(hour, price, price, close=price, open_=price, day=day)


def bull_fvg_candles():
    # candle1 high 1.0800, candle3 low 1.0805 -> 5 pip bullish gap
    return [
        bar(0, 1.0800, 1.0795, close=1.0798, open_=1.0796),
        bar(1, 1.0810, 1.0802, close=1.0809, open_=1.0803),
        bar(2, 1.0815, 1.0805, close=1.0812, open_=1.0806),
        bar(3, 1.0812, 1.0807, close=1.0810, open_=1.0808),
    ]


def engine(**overrides):
    return FVGEntryEngine(FVGEntryConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


def test_no_setup_on_short_input():
    eng = engine()
    assert eng.analyze([bar(0, 1.08, 1.07)], tf=900, symbol="EURUSD") == []


def test_long_setup_on_bullish_fvg_retest():
    candles = bull_fvg_candles() + [bar(4, 1.0808, 1.0804, close=1.0806, open_=1.0805)]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [FVGEntryEvent.FVG_ENTRY_SETUP.value]
    rec = last(eng)
    assert rec["direction"] == FVGEntryDirection.LONG.value
    assert rec["held"] is True


def test_missed_when_retest_breaks_through():
    candles = bull_fvg_candles() + [bar(4, 1.0804, 1.0790, close=1.0792, open_=1.0800)]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert events == []
    rec = last(eng)
    assert rec["state"] == FVGEntryState.MISSED.value


def test_event_payload_carries_gap_keys():
    candles = bull_fvg_candles() + [bar(4, 1.0808, 1.0804, close=1.0806, open_=1.0805)]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert events
    p = events[0]
    for k in ("gap_high", "gap_low", "direction", "held", "retest_price"):
        assert k in p


def test_created_at_absent_from_record():
    candles = bull_fvg_candles() + [bar(4, 1.0808, 1.0804, close=1.0806, open_=1.0805)]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    rec = last(eng)
    assert "created_at" not in rec


def test_reset_clears():
    candles = bull_fvg_candles() + [bar(4, 1.0808, 1.0804, close=1.0806, open_=1.0805)]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    eng.reset()
    assert eng.get_all() == [] and eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_FVG_ENTRY_CONFIG.min_gap_pips == 3.0
    assert DEFAULT_FVG_ENTRY_CONFIG.pip_size == 0.0001
    assert engine().config == FVGEntryConfig()
