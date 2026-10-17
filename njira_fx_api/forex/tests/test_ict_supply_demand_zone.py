"""Tests for the supply/demand zone entry detector."""
from datetime import datetime, timezone

from forex.analysis.supply_demand_zone import (
    DEFAULT_SUPPLY_DEMAND_ZONE_CONFIG,
    SupplyDemandZoneConfig,
    SupplyDemandZoneDirection,
    SupplyDemandZoneEngine,
    SupplyDemandZoneEvent,
    SupplyDemandZoneState,
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
    return SupplyDemandZoneEngine(SupplyDemandZoneConfig(**overrides))


def last(eng):
    out = eng.get_all()
    return out[-1] if out else {}


def test_no_setup_on_short_input():
    eng = engine()
    assert eng.analyze([], tf=900, symbol="EURUSD") == []


def test_long_setup_on_retest():
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert [e["event"] for e in events] == [SupplyDemandZoneEvent.SD_ZONE_ENTRY_SETUP.value]
    rec = last(eng)
    assert rec["direction"] == SupplyDemandZoneDirection.LONG.value
    assert rec["held"] is True


def test_missed_when_no_hold():
    candles = demand_base() + [bar(7, 1.0804, 1.0785, close=1.0788, open_=1.0800)]
    eng = engine()
    events = eng.analyze(candles, tf=900, symbol="EURUSD")
    assert events == []
    assert last(eng)["state"] == SupplyDemandZoneState.MISSED.value


def test_created_at_absent():
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    assert "created_at" not in last(eng)


def test_reset_clears():
    candles = demand_base() + [bar(7, 1.0804, 1.0799, close=1.0801, open_=1.0800)]
    eng = engine()
    eng.analyze(candles, tf=900, symbol="EURUSD")
    eng.reset()
    assert eng.get_all() == [] and eng.get_events() == []


def test_default_config_identity():
    assert DEFAULT_SUPPLY_DEMAND_ZONE_CONFIG.base_max_bars == 3
    assert DEFAULT_SUPPLY_DEMAND_ZONE_CONFIG.pip_size == 0.0001
    assert engine().config == SupplyDemandZoneConfig()
