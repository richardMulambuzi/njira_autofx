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
from forex.analysis.bos_continuation import (
    DEFAULT_BOS_CONTINUATION_CONFIG,
    BOSContinuationEngine,
)
from forex.analysis.breaker_reversal import (
    DEFAULT_BREAKER_REVERSAL_CONFIG,
    BreakerReversalEngine,
)
from forex.analysis.daily_bias import DEFAULT_DAILY_BIAS_CONFIG, DailyBiasEngine
from forex.analysis.dol import DEFAULT_DOL_CONFIG, DOLEngine
from forex.analysis.fvg_entry import DEFAULT_FVG_ENTRY_CONFIG, FVGEntryEngine
from forex.analysis.inducement_resistant import (
    DEFAULT_INDUCEMENT_RESISTANT_CONFIG,
    InducementResistantEngine,
)
from forex.analysis.mitigation_entry import (
    DEFAULT_MITIGATION_ENTRY_CONFIG,
    MitigationEntryEngine,
)
from forex.analysis.mtf_top_down import DEFAULT_MTF_TOP_DOWN_CONFIG, MtfTopDownEngine
from forex.analysis.ote import DEFAULT_OTE_CONFIG, OTEEngine
from forex.analysis.order_block_retest import (
    DEFAULT_ORDER_BLOCK_RETEST_CONFIG,
    OrderBlockRetestEngine,
)
from forex.analysis.premium_discount import (
    DEFAULT_PREMIUM_DISCOUNT_CONFIG,
    PremiumDiscountEngine,
)
from forex.analysis.premium_discount_entry import (
    DEFAULT_PREMIUM_DISCOUNT_ENTRY_CONFIG,
    PremiumDiscountEntryEngine,
)
from forex.analysis.supply_demand_zone import (
    DEFAULT_SUPPLY_DEMAND_ZONE_CONFIG,
    SupplyDemandZoneEngine,
)
from forex.analysis.sweep_mss_model import (
    DEFAULT_SWEEP_MSS_CONFIG,
    SweepMSSEngine,
)
from forex.analysis.strategy import Strategy, StrategyReport, register_strategy

logger = logging.getLogger("forex.analysis.ict_strategies")

# `position` outranks `leg_direction`: a premium/discount setup trades the
# position it sits in, while a bare leg direction (OTE) trades the leg.
_DIRECTION_KEYS = ("direction", "bias", "position", "leg_direction", "side")

_BULLISH_MARKS = ("BULL", "LONG", "BUY", "DEMAND", "DISCOUNT")
_BEARISH_MARKS = ("BEAR", "SHORT", "SELL", "SUPPLY", "PREMIUM")
# Whole-token only: a substring match would read "UP" inside "SUPPLY".
_BULLISH_EXACT = ("UP",)
_BEARISH_EXACT = ("DOWN",)


def normalize_direction(value: Any) -> str:
    """Collapse any leaf module's direction vocabulary into the report's three."""
    if value is None:
        return "NEUTRAL"
    text = getattr(value, "value", value)
    token = text.upper() if isinstance(text, str) else str(text).upper()
    if token in _BULLISH_EXACT:
        return "BULLISH"
    if token in _BEARISH_EXACT:
        return "BEARISH"
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
    if hasattr(record, "to_dict"):
        return record.to_dict()
    return record if isinstance(record, dict) else {}


# Leaf modules name their zone differently: order blocks use zone_*, FVG uses
# gap_*, OTE uses entry_zone_*, premium/discount entry uses range_*.
_ZONE_PAIRS = (
    ("entry_zone_low", "entry_zone_high"),
    ("gap_low", "gap_high"),
    ("zone_low", "zone_high"),
    ("range_low", "range_high"),
)

# Some modules publish a finished trade and should be taken at their word
# rather than re-derived from a zone.
_LEVEL_KEYS = ("entry", "stop", "target")

# A published target may go by another name (DOL names it target_price).
_TARGET_ALIASES = ("target_price",)

# The daily bias detector reads 4H bars in this codebase (see IctDailyBiasStrategy).
DAILY_BIAS_TF_SECONDS = 14400


def zones_of(record: Any) -> tuple[float, float]:
    """The record's actionable zone, whichever key pair that module uses."""
    payload = payload_of(record)
    for low_key, high_key in _ZONE_PAIRS:
        low = float(payload.get(low_key) or 0.0)
        high = float(payload.get(high_key) or 0.0)
        if low > 0.0 and high > low:
            return low, high
    return 0.0, 0.0


def levels_of(record: Any) -> dict[str, float]:
    """A record's own entry/stop/target, when it publishes a complete trade."""
    payload = payload_of(record)
    levels: dict[str, float] = {}
    for key in _LEVEL_KEYS:
        value = float(payload.get(key) or 0.0)
        if value > 0.0:
            levels[key] = value
    if "target" not in levels:
        for alias in _TARGET_ALIASES:
            value = float(payload.get(alias) or 0.0)
            if value > 0.0:
                levels["target"] = value
                break
    return levels


_BLOCKING_FLAGS = ("actionable", "tradeable", "aligned")


def is_actionable(record: Any) -> bool:
    """False when a record explicitly reports itself untradable."""
    payload = payload_of(record)
    for key in _BLOCKING_FLAGS:
        if key in payload and payload[key] is False:
            return False
    return str(payload.get("status") or "").upper() != "GATE_BLOCKED"


# Leaf records carry no `strength` field, so score from the evidence they do
# publish instead of leaving every combo on an identical 50.
_SCORE_UP = {"held": 10.0, "actionable": 15.0, "aligned": 15.0, "tradeable": 15.0,
             "in_killzone": 5.0, "deep_zone": 10.0, "in_roadmap_band": 10.0}
_SCORE_DOWN = {"held": -10.0, "actionable": -10.0, "aligned": -10.0}
_DROPPED_STATES = ("MISSED", "BROKEN", "CONFLICT", "SHALLOW", "GATE_BLOCKED")


def confidence_of(record: Any) -> float:
    """Score a record from the evidence it actually carries."""
    payload = payload_of(record)
    raw = payload.get("strength")
    if raw is not None:
        value = float(raw)
        return max(30.0, min(90.0, value * 100.0 if value <= 1.0 else value))

    score = 50.0
    for key, delta in _SCORE_UP.items():
        if payload.get(key) is True:
            score += delta
    for key, delta in _SCORE_DOWN.items():
        if payload.get(key) is False:
            score += delta
    if str(payload.get("state") or "").upper() in _DROPPED_STATES:
        score -= 10.0
    return max(30.0, min(90.0, score))


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


def run_leaf(
    engine_cls: Any,
    config: Any,
    candles: list[dict],
    tf: int = 0,
    symbol: str = "",
    **ctx: Any,
) -> list[Any]:
    """Analyze candles with one leaf engine and return its still-active records."""
    leaf = engine_cls(config=config)
    leaf.analyze(candles, tf=tf, symbol=symbol, **ctx)
    return leaf.get_active()


class _LeafStrategy(Strategy):
    """Shared plumbing for the one-concept-per-strategy wrappers."""

    leaf_engine: Any = None
    leaf_config: Any = None
    tf_seconds: int = 900
    config: dict[str, Any] = {}

    def _leaf_ctx(self, symbol: str, engine: Any) -> dict[str, Any]:
        """Extra analyse() inputs this concept needs. Most need none."""
        return {}

    def _records(self, symbol: str, engine: Any) -> list[Any]:
        candles = candles_for(engine, symbol, self.tf_seconds)
        if len(candles) < 3:
            return []
        config = with_overrides(self.leaf_config, self.config)
        try:
            return run_leaf(
                self.leaf_engine,
                config,
                candles,
                tf=self.tf_seconds,
                symbol=symbol,
                **self._leaf_ctx(symbol, engine),
            )
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


@register_strategy
class IctOrderBlockRetestStrategy(_LeafStrategy):
    name = "ict_order_block_retest"
    leaf_engine = OrderBlockRetestEngine
    leaf_config = DEFAULT_ORDER_BLOCK_RETEST_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no retested order block")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["a retested order block"],
            confirmations=[f"retest state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"order block retest reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctFvgEntryStrategy(_LeafStrategy):
    name = "ict_fvg_entry"
    leaf_engine = FVGEntryEngine
    leaf_config = DEFAULT_FVG_ENTRY_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no fresh FVG entry")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["an open fair value gap"],
            confirmations=[f"fvg state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"FVG entry reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctBreakerReversalStrategy(_LeafStrategy):
    name = "ict_breaker_reversal"
    leaf_engine = BreakerReversalEngine
    leaf_config = DEFAULT_BREAKER_REVERSAL_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no breaker reversal")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["a flipped breaker block"],
            confirmations=[f"breaker state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"breaker reversal reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctMitigationEntryStrategy(_LeafStrategy):
    name = "ict_mitigation_entry"
    leaf_engine = MitigationEntryEngine
    leaf_config = DEFAULT_MITIGATION_ENTRY_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no mitigation entry")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["a mitigated block"],
            confirmations=[f"mitigation state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"mitigation entry reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctSupplyDemandZoneStrategy(_LeafStrategy):
    name = "ict_supply_demand_zone"
    leaf_engine = SupplyDemandZoneEngine
    leaf_config = DEFAULT_SUPPLY_DEMAND_ZONE_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no supply/demand zone")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["a fresh supply/demand zone"],
            confirmations=[f"zone state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"supply/demand zone reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctPremiumDiscountEntryStrategy(_LeafStrategy):
    name = "ict_premium_discount_entry"
    leaf_engine = PremiumDiscountEntryEngine
    leaf_config = DEFAULT_PREMIUM_DISCOUNT_ENTRY_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no premium/discount entry")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["price in a dealing range"],
            confirmations=[f"entry state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"premium/discount entry reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctInducementResistantStrategy(_LeafStrategy):
    name = "ict_inducement_resistant"
    leaf_engine = InducementResistantEngine
    leaf_config = DEFAULT_INDUCEMENT_RESISTANT_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no inducement-resistant entry")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["an inducement-resistant setup"],
            confirmations=[f"state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"inducement-resistant reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctBosContinuationStrategy(_LeafStrategy):
    name = "ict_bos_continuation"
    leaf_engine = BOSContinuationEngine
    leaf_config = DEFAULT_BOS_CONTINUATION_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no BOS continuation")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["a confirmed BOS"],
            confirmations=[f"continuation state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"BOS continuation reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctMtfTopDownStrategy(_LeafStrategy):
    name = "ict_mtf_top_down"
    leaf_engine = MtfTopDownEngine
    leaf_config = DEFAULT_MTF_TOP_DOWN_CONFIG
    tf_seconds = 900

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no MTF top-down alignment")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["multi-timeframe alignment"],
            confirmations=[f"alignment state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"MTF top-down reads {payload.get('state', 'UNKNOWN')}",
        )


@register_strategy
class IctSweepMssStrategy(_LeafStrategy):
    name = "ict_sweep_mss"
    leaf_engine = SweepMSSEngine
    leaf_config = DEFAULT_SWEEP_MSS_CONFIG
    tf_seconds = 900

    def _leaf_ctx(self, symbol: str, engine: Any) -> dict[str, Any]:
        """The flagship refuses to trade without the locked daily bias."""
        candles = candles_for(engine, symbol, DAILY_BIAS_TF_SECONDS)
        if not candles:
            return {}
        bias_engine = DailyBiasEngine()
        bias_engine.analyze(candles, tf=DAILY_BIAS_TF_SECONDS, symbol=symbol)
        active = bias_engine.get_active(limit=1)
        return {"bias": payload_of(active[-1])} if active else {}

    def evaluate(self, symbol, context=None, engine=None, for_signals=False):
        records = self._records(symbol, engine)
        if not records:
            return self._inactive("no liquidity sweep + MSS")
        payload = payload_of(records[-1])
        direction = direction_of(records[-1])
        return StrategyReport(
            name=self.name, active=direction != "NEUTRAL", applicable=True,
            direction=direction,
            preconditions=["a liquidity sweep followed by MSS"],
            confirmations=[f"state is {payload.get('state', 'UNKNOWN')}"],
            reason=f"sweep + MSS reads {payload.get('state', 'UNKNOWN')}",
        )
