"""Tests for the order block retest detector."""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.order_block_retest import (
    DEFAULT_ORDER_BLOCK_RETEST_CONFIG,
    OrderBlockRetestConfig,
    OrderBlockRetestDirection,
    OrderBlockRetestEngine,
    OrderBlockRetestEvent,
    OrderBlockRetestState,
    compute_atr,
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


def demand_base(day=0):
    return [
        flat(0, 1.0800, day=day),
        bar(1, 1.0802, 1.0798, close=1.0801, open_=1.0799, day=day),
        bar(2, 1.0803, 1.0799, close=1.0799, open_=1.0802, day=day),
        bar(3, 1.0801, 1.0797, close=1.0798, open_=1.0800, day=day),
        bar(4, 1.0850, 1.0798, close=1.0849, open_=1.0800, day=day),
        bar(5, 1.0840, 1.0820, close=1.0822, open_=1.0845, day=day),
        bar(6, 1.0825, 1.0805, close=1.0807, open_=1.0820, day=day),
    ]


def engine(**overrides):
    return OrderBlockRetestEngine(OrderBlockRetestConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


def test_compute_atr_positive_on_real_fixture():
    assert compute_atr(demand_base(), 14) > 0


def test_no_setup_on_empty_or_single():
    eng = engine()
    assert eng.analyze([], tf=900, symbol="EURUSD") == []
    assert eng.analyze([flat(0, 1.08)], tf=900, symbol="EURUSD") == []


def test_no_setup_when_price_never_retests():
    eng = engine()
    # departure leaves; price never comes back into the zone
    candles = demand_base() + [bar(7, 1.0900, 1.0880, close=1.0895, open_=1.0885)]
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert events == []
    rec = last(eng)
    assert rec["state"] == OrderBlockRetestState.MISSED.value


def test_long_setup_on_demand_retest():
    eng = engine()
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [OrderBlockRetestEvent.OB_RETEST_SETUP.value]
    rec = last(eng)
    assert rec["direction"] == OrderBlockRetestDirection.LONG.value
    assert rec["state"] == OrderBlockRetestState.RETESTED.value
    assert rec["held"] is True


def test_short_setup_on_supply_retest():
    supply = [
        flat(0, 1.0850),
        bar(1, 1.0852, 1.0848, close=1.0849, open_=1.0851),
        bar(2, 1.0851, 1.0847, close=1.0851, open_=1.0848),
        bar(3, 1.0853, 1.0849, close=1.0852, open_=1.0850),
        bar(4, 1.0852, 1.0800, close=1.0801, open_=1.0850),
        bar(5, 1.0830, 1.0810, close=1.0828, open_=1.0805),
        bar(6, 1.0845, 1.0825, close=1.0843, open_=1.0828),
    ]
    eng = engine()
    candles = supply + [bar(7, 1.0851, 1.0846, close=1.0848, open_=1.0850)]
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [OrderBlockRetestEvent.OB_RETEST_SETUP.value]
    rec = last(eng)
    assert rec["direction"] == OrderBlockRetestDirection.SHORT.value
    assert rec["held"] is True


def test_missed_when_retest_breaks_through():
    eng = engine()
    candles = demand_base() + [bar(7, 1.0804, 1.0785, close=1.0788, open_=1.0800)]
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert events == []
    rec = last(eng)
    assert rec["state"] == OrderBlockRetestState.MISSED.value
    assert rec["held"] is False


def test_event_payload_carries_zone_keys():
    eng = engine()
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert events
    p = events[0]
    assert p["event"] == OrderBlockRetestEvent.OB_RETEST_SETUP.value
    for k in ("zone_high", "zone_low", "direction", "held", "retest_price"):
        assert k in p


def test_created_at_absent_from_record():
    eng = engine()
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    eng.analyze(candles, tf=900, symbol="EURUSD")
    rec = last(eng)
    assert "created_at" not in rec


def test_state_isolated_per_symbol():
    eng = engine()
    c1 = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    c2 = demand_base(day=1) + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800, day=1)]
    eng.analyze(c1, tf=900, symbol="EURUSD")
    eng.analyze(c2, tf=900, symbol="GBPUSD")
    assert len(eng.get_all()) == 2


def test_reset_clears():
    eng = engine()
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    eng.analyze(candles, tf=900, symbol="EURUSD")
    assert eng.get_events() != []
    eng.reset()
    assert eng.get_all() == []
    assert eng.get_active() == []
    assert eng.get_events() == []


def test_deterministic():
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    a = engine()
    b = engine()
    a.analyze(candles, tf=900, symbol="EURUSD")
    b.analyze(candles, tf=900, symbol="EURUSD")
    assert a.get_all() == b.get_all()


def test_default_config_identity():
    assert DEFAULT_ORDER_BLOCK_RETEST_CONFIG.base_max_bars == 3
    assert DEFAULT_ORDER_BLOCK_RETEST_CONFIG.impulse_min_atr_mult == 1.5
    assert DEFAULT_ORDER_BLOCK_RETEST_CONFIG.atr_period == 14
    assert DEFAULT_ORDER_BLOCK_RETEST_CONFIG.retest_tolerance_pips == 2.0
    assert DEFAULT_ORDER_BLOCK_RETEST_CONFIG.pip_size == 0.0001
    assert engine().config == OrderBlockRetestConfig()
