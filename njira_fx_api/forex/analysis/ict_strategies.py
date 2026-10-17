"""Thin ICT strategy adapters, one leaf concept per strategy.

Each adapter below wraps exactly ONE Wave 0 engine so that concept can be tuned
on its own. No adapter contains trading logic: each pulls candles out of the
SignalEngine cache, runs one pure engine, and translates the result into a
StrategyReport.

`strategy.py` is never edited. Importing this module is what makes the registry
aware of the `ict_*` names, because every class declares itself with the
`@register_strategy` decorator.
"""

from __future__ import annotations

import logging
from dataclasses import fields, replace
from typing import Any, Optional

from forex.analysis.context import MarketContextState
from forex.analysis.daily_bias import DEFAULT_DAILY_BIAS_CONFIG, DailyBiasEngine
from forex.analysis.dol import DEFAULT_DOL_CONFIG, DOLEngine
from forex.analysis.ote import DEFAULT_OTE_CONFIG, OTEEngine
from forex.analysis.premium_discount import (
    DEFAULT_PREMIUM_DISCOUNT_CONFIG,
    PremiumDiscountEngine,
)
from forex.analysis.strategy import Strategy, StrategyReport, register_strategy

logger = logging.getLogger("forex.analysis.ict_strategies")

_DIRECTION_KEYS = ("direction", "bias", "position", "side")

_BULLISH_MARKS = ("BULL", "LONG", "BUY", "DEMAND", "DISCOUNT")
_BEARISH_MARKS = ("BEAR", "SHORT", "SELL", "SUPPLY", "PREMIUM")


def normalize_direction(value: Any) -> str:
    """Collapse any leaf module's direction vocabulary into the report's three."""
    if value is None:
        return "NEUTRAL"
    text = getattr(value, "value", value)
    token = text.upper() if isinstance(text, str) else str(text).upper()
    if any(mark in token for mark in _BULLISH_MARKS):
        return "BULLISH"
    if any(mark in token for mark in _BEARISH_MARKS):
        return "BEARISH"
    return "NEUTRAL"


def direction_of(record: Any) -> str:
    """Read a leaf record's direction field, whichever name that module uses."""
    payload = record.to_dict() if hasattr(record, "to_dict") else record
    if not isinstance(payload, dict):
        return normalize_direction(record)
    for key in _DIRECTION_KEYS:
        if key in payload:
            return normalize_direction(payload[key])
    return "NEUTRAL"


def payload_of(record: Any) -> dict:
    """A leaf record as a plain dict, so callers never touch engine internals."""
    return record.to_dict() if hasattr(record, "to_dict") else {}


def candles_for(engine: Any, symbol: str, tf_seconds: int) -> list[dict]:
    """Module-format candles from the SignalEngine cache, or an empty list."""
    cache = getattr(engine, "_cache", None)
    convert = getattr(engine, "_module_candles", None)
    if cache is None or convert is None:
        return []
    try:
        return list(convert(cache.get(symbol, tf_seconds)) or [])
    except Exception:
        logger.debug("[ICT] no candles for %s tf=%s", symbol, tf_seconds, exc_info=True)
        return []


def with_overrides(default: Any, overrides: dict[str, Any]) -> Any:
    """Fold an adapter's config overrides into a frozen leaf Config."""
    if not overrides:
        return default
    unknown = sorted(set(overrides) - {field.name for field in fields(default)})
    if unknown:
        raise ValueError(f"unknown config override(s): {', '.join(unknown)}")
    return replace(default, **overrides)


def run_leaf(engine_cls: Any, config: Any, candles: list[dict]) -> list[Any]:
    """Analyze candles with one leaf engine and return its still-active records."""
    leaf = engine_cls(config=config)
    leaf.analyze(candles)
    return leaf.get_active()


class _LeafStrategy(Strategy):
    """Shared plumbing for the one-concept-per-strategy wrappers."""

    leaf_engine: Any = None
    leaf_config: Any = None
    tf_seconds: int = 900
    config: dict[str, Any] = {}

    def _records(self, symbol: str, engine: Any) -> list[Any]:
        candles = candles_for(engine, symbol, self.tf_seconds)
        if len(candles) < 3:
            return []
        config = with_overrides(self.leaf_config, self.config)
        try:
            return run_leaf(self.leaf_engine, config, candles)
        except Exception:
            logger.warning("[ICT] %s failed on %s", self.name, symbol, exc_info=True)
            return []

    def _inactive(self, reason: str) -> StrategyReport:
        return StrategyReport(
            name=self.name,
            active=False,
            applicable=False,
            direction="NEUTRAL",
            missing_confirmations=[reason],
            reason=reason,
        )


@register_strategy
class IctPremiumDiscountStrategy(_LeafStrategy):
    """Where the live price sits inside the current dealing range."""

    name = "ict_premium_discount"
    leaf_engine = PremiumDiscountEngine
    leaf_config = DEFAULT_PREMIUM_DISCOUNT_CONFIG
    tf_seconds = 900

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no dealing range available")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        position = str(payload.get("position", "UNKNOWN"))
        return StrategyReport(
            name=self.name,
            active=direction != "NEUTRAL",
            applicable=True,
            direction=direction,
            preconditions=["a dealing range bounded by a swing high and low"],
            confirmations=[f"price trades in {position}"],
            reason=f"price is {position} of the dealing range",
        )


@register_strategy
class IctOteStrategy(_LeafStrategy):
    """Whether price has retraced into the optimal trade entry band."""

    name = "ict_ote"
    leaf_engine = OTEEngine
    leaf_config = DEFAULT_OTE_CONFIG
    tf_seconds = 900

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("price is outside the optimal trade entry band")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        state = str(payload.get("state", "UNKNOWN"))
        return StrategyReport(
            name=self.name,
            active=direction != "NEUTRAL",
            applicable=True,
            direction=direction,
            preconditions=["a completed impulse leg with a measurable range"],
            confirmations=[f"entry band state is {state}"],
            reason=f"optimal trade entry state is {state}",
        )


@register_strategy
class IctDolStrategy(_LeafStrategy):
    """The draw on liquidity: which untapped pool price is reaching for."""

    name = "ict_dol"
    leaf_engine = DOLEngine
    leaf_config = DEFAULT_DOL_CONFIG
    tf_seconds = 3600

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no unswept liquidity pool in range")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        pool = str(payload.get("pool_kind", payload.get("kind", "UNKNOWN")))
        level = payload.get("level")
        confirms = [f"target pool is {pool}"]
        if isinstance(level, (int, float)):
            confirms.append(f"pool level is {level}")
        return StrategyReport(
            name=self.name,
            active=direction != "NEUTRAL",
            applicable=True,
            direction=direction,
            preconditions=["a liquidity pool that has not been swept"],
            confirmations=confirms,
            reason=f"draw on liquidity points at {pool}",
        )


@register_strategy
class IctDailyBiasStrategy(_LeafStrategy):
    """Daily directional bias, reported as its own independently tunable read."""

    name = "ict_daily_bias"
    leaf_engine = DailyBiasEngine
    leaf_config = DEFAULT_DAILY_BIAS_CONFIG
    tf_seconds = 14400

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("daily bias is undecided")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        bias = str(payload.get("bias", "UNKNOWN"))
        agreement = str(payload.get("agreement", ""))
        confirmations = [f"bias reads {bias}"]
        if agreement:
            confirmations.append(f"factor agreement is {agreement}")
        return StrategyReport(
            name=self.name,
            active=direction != "NEUTRAL",
            applicable=True,
            direction=direction,
            preconditions=["a daily open and a weekly open inside the lookback"],
            confirmations=confirmations,
            reason=f"daily bias reads {bias}",
        )
