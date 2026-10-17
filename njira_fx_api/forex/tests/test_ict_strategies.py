"""Tests for the ICT strategy adapters."""

from __future__ import annotations

from enum import Enum

import pytest

from forex.analysis.daily_bias import DailyBiasEngine
from forex.analysis.dol import DOLEngine
from forex.analysis.ict_strategies import (
    IctDailyBiasStrategy,
    IctDolStrategy,
    IctOteStrategy,
    IctPremiumDiscountStrategy,
    candles_for,
    direction_of,
    normalize_direction,
    payload_of,
    run_leaf,
    with_overrides,
)
from forex.analysis.ote import OTEEngine
from forex.analysis.premium_discount import (
    DEFAULT_PREMIUM_DISCOUNT_CONFIG,
    PremiumDiscountEngine,
)
from forex.analysis.strategy import Strategy, StrategyRegistry


class _Direction(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class _Record:
    def __init__(self, **payload):
        self._payload = payload

    def to_dict(self):
        return dict(self._payload)


class _FakeCache:
    def __init__(self, candles):
        self._candles = candles
        self.calls = []

    def get(self, symbol, tf):
        self.calls.append((symbol, tf))
        return self._candles


class _FakeEngine:
    def __init__(self, candles=None):
        self._cache = _FakeCache(candles if candles is not None else [])

    @staticmethod
    def _module_candles(candles):
        return candles


class _BareEngine:
    pass


class _StubLeaf:
    def __init__(self, config=None):
        self.config = config
        self.analyzed = False

    def analyze(self, candles):
        self.analyzed = True
        return []

    def get_active(self):
        return [{"stub": True}] if self.analyzed else []


def _bar(open_, high, low, close, t=1_700_000_000):
    return {"open": open_, "high": high, "low": low, "close": close, "open_time": t}


def _candles(n=6):
    return [_bar(1.10 + i * 0.001, 1.105 + i * 0.001, 1.095 + i * 0.001, 1.102 + i * 0.001) for i in range(n)]


ALL_ADAPTERS = (
    IctPremiumDiscountStrategy,
    IctOteStrategy,
    IctDolStrategy,
    IctDailyBiasStrategy,
)


def test_all_adapters_are_registered():
    for cls in ALL_ADAPTERS:
        assert cls.name.startswith("ict_")
        assert cls.name in StrategyRegistry.names()
        assert StrategyRegistry.get(cls.name) is cls


def test_all_adapters_subclass_strategy():
    for cls in ALL_ADAPTERS:
        assert issubclass(cls, Strategy)


def test_adapter_names_are_unique():
    names = [cls.name for cls in ALL_ADAPTERS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "cls,leaf",
    [
        (IctPremiumDiscountStrategy, PremiumDiscountEngine),
        (IctOteStrategy, OTEEngine),
        (IctDolStrategy, DOLEngine),
        (IctDailyBiasStrategy, DailyBiasEngine),
    ],
)
def test_adapter_wraps_one_leaf_engine(cls, leaf):
    assert cls.leaf_engine is leaf


@pytest.mark.parametrize(
    "cls,tf",
    [
        (IctPremiumDiscountStrategy, 900),
        (IctOteStrategy, 900),
        (IctDolStrategy, 3600),
        (IctDailyBiasStrategy, 14400),
    ],
)
def test_adapter_timeframes(cls, tf):
    assert cls.tf_seconds == tf


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "NEUTRAL"),
        ("", "NEUTRAL"),
        ("flat", "NEUTRAL"),
        ("BULLISH", "BULLISH"),
        ("bullish", "BULLISH"),
        ("BEARISH", "BEARISH"),
        ("LONG", "BULLISH"),
        ("SHORT", "BEARISH"),
        ("BUY", "BULLISH"),
        ("SELL", "BEARISH"),
        ("BULLISH_OB", "BULLISH"),
        ("BEARISH_OB", "BEARISH"),
        ("DEMAND", "BULLISH"),
        ("SUPPLY", "BEARISH"),
        ("DISCOUNT", "BULLISH"),
        ("PREMIUM", "BEARISH"),
    ],
)
def test_normalize_direction_maps_every_vocabulary(value, expected):
    assert normalize_direction(value) == expected


def test_normalize_direction_reads_enum_members():
    assert normalize_direction(_Direction.BEARISH) == "BEARISH"
    assert normalize_direction(_Direction.BULLISH) == "BULLISH"


def test_normalize_direction_handles_non_string_values():
    assert normalize_direction(7) == "NEUTRAL"


def test_direction_of_prefers_direction_key():
    assert direction_of(_Record(direction="BULLISH", bias="BEARISH")) == "BULLISH"


def test_direction_of_falls_back_to_bias():
    assert direction_of(_Record(bias="BEARISH")) == "BEARISH"


def test_direction_of_falls_back_to_position():
    assert direction_of(_Record(position="DISCOUNT")) == "BULLISH"


def test_direction_of_falls_back_to_side():
    assert direction_of(_Record(side="SUPPLY")) == "BEARISH"


def test_direction_of_returns_neutral_without_a_direction_key():
    assert direction_of(_Record(state="VALID")) == "NEUTRAL"


def test_direction_of_accepts_a_plain_dict():
    assert direction_of({"direction": "LONG"}) == "BULLISH"


def test_direction_of_normalizes_a_bare_value():
    assert direction_of("SHORT") == "BEARISH"


def test_payload_of_dictifies_a_record():
    assert payload_of(_Record(state="VALID")) == {"state": "VALID"}


def test_payload_of_returns_empty_dict_for_a_plain_object():
    assert payload_of(object()) == {}


def test_candles_for_returns_cached_candles():
    bars = _candles()
    assert candles_for(_FakeEngine(bars), "EURUSD", 900) == bars


def test_candles_for_passes_symbol_and_tf_to_the_cache():
    engine = _FakeEngine(_candles())
    candles_for(engine, "XAUUSD", 3600)
    assert engine._cache.calls == [("XAUUSD", 3600)]


def test_candles_for_returns_empty_without_a_cache():
    assert candles_for(_BareEngine(), "EURUSD", 900) == []


def test_candles_for_returns_empty_without_a_converter():
    class NoConverter:
        _cache = _FakeCache(_candles())

    assert candles_for(NoConverter(), "EURUSD", 900) == []


def test_candles_for_returns_empty_when_the_cache_raises():
    class Exploding:
        _cache = None

        @staticmethod
        def _module_candles(candles):
            raise RuntimeError("boom")

    engine = Exploding()
    engine._cache = _FakeCache(_candles())
    assert candles_for(engine, "EURUSD", 900) == []


def test_with_overrides_returns_the_default_when_empty():
    assert with_overrides(DEFAULT_PREMIUM_DISCOUNT_CONFIG, {}) is DEFAULT_PREMIUM_DISCOUNT_CONFIG


def test_with_overrides_applies_a_known_key():
    config = with_overrides(DEFAULT_PREMIUM_DISCOUNT_CONFIG, {"pip_size": 0.0001})
    assert config.pip_size == 0.0001
    assert config is not DEFAULT_PREMIUM_DISCOUNT_CONFIG


def test_with_overrides_rejects_an_unknown_key():
    with pytest.raises(ValueError, match="unknown config override"):
        with_overrides(DEFAULT_PREMIUM_DISCOUNT_CONFIG, {"nope": 1})


def test_run_leaf_analyzes_and_returns_active_records():
    assert run_leaf(_StubLeaf, DEFAULT_PREMIUM_DISCOUNT_CONFIG, _candles()) == [{"stub": True}]


def test_run_leaf_passes_the_config_through():
    class Capture(_StubLeaf):
        seen = None

        def __init__(self, config=None):
            super().__init__(config)
            Capture.seen = config

    run_leaf(Capture, "sentinel", _candles())
    assert Capture.seen == "sentinel"


@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_is_inactive_when_no_candles_are_cached(cls):
    report = cls().evaluate("EURUSD", None, _FakeEngine([]), False)
    assert report.active is False
    assert report.applicable is False
    assert report.direction == "NEUTRAL"
    assert report.missing_confirmations
    assert report.reason
    assert report.name == cls.name


@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_is_inactive_when_the_cache_is_missing(cls):
    report = cls().evaluate("EURUSD", None, _BareEngine(), False)
    assert report.active is False
    assert report.direction == "NEUTRAL"


@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_is_inactive_with_too_few_candles(cls):
    report = cls().evaluate("EURUSD", None, _FakeEngine(_candles(2)), False)
    assert report.active is False
    assert report.reason


@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_survives_a_leaf_engine_failure(cls, monkeypatch):
    monkeypatch.setattr(
        "forex.analysis.ict_strategies.run_leaf",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = cls().evaluate("EURUSD", None, _FakeEngine(_candles()), False)
    assert report.active is False
    assert report.applicable is False


@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_rejects_an_unknown_config_override(cls):
    strategy = cls()
    strategy.config = {"not_a_real_knob": 1}
    with pytest.raises(ValueError, match="unknown config override"):
        strategy.evaluate("EURUSD", None, _FakeEngine(_candles()), False)


@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_ignores_the_context_argument(cls):
    assert cls().evaluate("EURUSD", None, _FakeEngine(_candles()), False).name == cls.name


@pytest.mark.parametrize("cls", ALL_ADAPTERS)
def test_adapter_reports_are_json_friendly(cls):
    report = cls().evaluate("EURUSD", None, _FakeEngine([]), False)
    payload = report.to_dict()
    assert payload["name"] == cls.name
    assert isinstance(payload["preconditions"], list)
    assert isinstance(payload["confirmations"], list)
