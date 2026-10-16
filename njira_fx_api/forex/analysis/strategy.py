"""
Strategy interface + registry (improve.md §20 — Phase 7).

Replaces the hardcoded if/elif dispatch in SignalEngine.evaluate() with a
registry of pluggable strategies. Every strategy reports its full status via
``StrategyReport`` (active/inactive, applicable, direction, preconditions,
confirmations, missing confirmations, invalidations, confidence, candidate
setup, reason) so the UI (§45) and strategy-agreement logic (§21) can surface
disagreement instead of hiding it.

The actual signal-building logic stays in SignalEngine (``_evaluate_confluence``
/ ``_evaluate_mtf``); a strategy's ``evaluate`` produces a *status report* by
delegating to the engine's analysis helpers (``_analyze_confluence`` /
``_analyze_mtf``). This keeps the strategy layer thin and the business logic in
one place.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Type

from forex.analysis.context import MarketContextState

logger = logging.getLogger("forex.analysis.strategy")


def _bias_to_direction(bias: str) -> str:
    """Map a context bias/regime string to a StrategyReport direction."""
    if not bias:
        return "NEUTRAL"
    b = bias.lower()
    if "bull" in b or b == "buy":
        return "BULLISH"
    if "bear" in b or b == "sell":
        return "BEARISH"
    return "NEUTRAL"


@dataclass
class StrategyReport:
    """Full status of one strategy for one symbol (improve.md §20)."""

    name: str
    active: bool
    applicable: bool
    direction: str  # "BULLISH" | "BEARISH" | "NEUTRAL"
    preconditions: list[str] = field(default_factory=list)
    confirmations: list[str] = field(default_factory=list)
    missing_confirmations: list[str] = field(default_factory=list)
    invalidations: list[str] = field(default_factory=list)
    confidence: float = 0.0  # 0-100
    candidate_setup: Optional[dict] = None  # entry/sl/tp (+ rr) when a setup exists
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "active": self.active,
            "applicable": self.applicable,
            "direction": self.direction,
            "preconditions": self.preconditions,
            "confirmations": self.confirmations,
            "missing_confirmations": self.missing_confirmations,
            "invalidations": self.invalidations,
            "confidence": self.confidence,
            "candidate_setup": self.candidate_setup,
            "reason": self.reason,
        }


class Strategy(ABC):
    """Base class for all trading strategies."""

    name: str = ""  # registry key — must be set by subclasses

    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        """
        Report this strategy's status against the shared market context.

        ``for_signals=True`` runs the heavier contradiction check so the report
        includes INVALIDATE reasons (used by get_strategy_reports). The hot
        signal path passes ``for_signals=False`` to avoid double-running the
        contradiction engine that SignalEngine.evaluate() runs again when it
        builds the actual signal.
        """
        ...


class StrategyRegistry:
    """Dict-based registry of strategy classes keyed by ``Strategy.name``."""

    _registry: dict[str, Type[Strategy]] = {}

    @classmethod
    def register(cls, strategy_cls: Type[Strategy]) -> Type[Strategy]:
        if not strategy_cls.name:
            raise ValueError(
                f"{strategy_cls.__name__} must define a non-empty 'name' class attribute"
            )
        cls._registry[strategy_cls.name] = strategy_cls
        logger.debug("[STRATEGY] registered %s as '%s'", strategy_cls.__name__, strategy_cls.name)
        return strategy_cls

    @classmethod
    def get(cls, name: str) -> Optional[Type[Strategy]]:
        return cls._registry.get(name)

    @classmethod
    def all(cls) -> list[Type[Strategy]]:
        return list(cls._registry.values())

    @classmethod
    def names(cls) -> list[str]:
        return list(cls._registry.keys())


def register_strategy(strategy_cls: Type[Strategy]) -> Type[Strategy]:
    """Decorator form of ``StrategyRegistry.register``."""
    return StrategyRegistry.register(strategy_cls)


@register_strategy
class MalysianStrategy(Strategy):
    """Status report for the malysian (Malaysian SNR) strategy.

    The engine implements the book's 5 ACTION PLANS (p.73):
      1. MARK THE ZONE      — HTF engulfing setup; the SNR line is drawn from a
                              candle CLOSE to the next opposite candle OPEN and
                              extended right (a sloped A/V trendline).
      2. PRICE REJECTION    — price taps the zone with a WICK and closes off the
                              level; the rejection candle must be an ENGULFING
                              or a PIN BAR ("NO VALIDATION, NO TRADE", p.46) and
                              the first touch must be a SHADOW, never a body
                              (p.16/p.20).
      3. PRICE BREAKOUT     — one timeframe lower, the LTF breaks in the same
                              direction with the HTF zone inside the LTF one.
      4. CONFLUENCE POINT   — trendline + SNR convergence (2–3 agreeing points).
      5. ENTRY CONFIRMATION — retest of the LTF ENGULFING ZONE; SL at the lower
                              low (buy) / higher high (sell); TP at the NEXT
                              FRESH SNR level in the trade direction (p.67),
                              falling back to 2x risk when none sits beyond.

    The engine runs the WHOLE SNR family behind this one registry key and this
    report shows the merged best setup:
      • core SNR      — 4H rejection + 15M breakout (above), with the course's
                        MISS level-validation, freshness tracking and pullback
                        entry.
      • engulfing     — an H4 GAP SNR refined into a 15M engulfing.
      • marriage      — two 4H SNRs married into one trendline, traded at
                        point #3 (the extension past the second SNR).
      • confluence    — the multi-TF convergence draft (HTF bias + OB/FVG +
                        sweep/displacement agreeing in one direction).
    Every candidate is then gated on the top-down storyline (weekly vs daily
    direction) and on entry timing inside the instrument's home sessions.  Candle
    history (d1/h4/h1/m15) is read from the engine's hot cache, so a missing
    timeframe only disables its own filter — it never breaks the report.

    Delegates to the engine's ``_analyze_snr`` — the same pattern as
    ``MovingAveragesStrategy`` → ``_analyze_mtf``.  The legacy
    ``analysis_module`` implementation was deleted; the real SNR logic lives in
    SignalEngine.
    """


    name = "malysian"

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        return engine._analyze_snr(symbol, for_signals=for_signals)


@register_strategy
class MovingAveragesStrategy(Strategy):
    """Wraps the multi-timeframe (Moving_averages) SMA-alignment logic."""

    name = "Moving_averages"

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        return engine._analyze_mtf(symbol, for_signals=for_signals)


@register_strategy
class OrderBlockStrategy(Strategy):
    """Reports status from the most recent active order block in context.

    BULLISH_OB → BUY bias, BEARISH_OB → SELL. Reads ``context.order_blocks``
    directly (each entry is an OB ``to_dict`` with keys ``direction``
    ("BULLISH_OB"/"BEARISH_OB"), ``state``, ``high``, ``low``, ``quality_score``).
    """

    name = "order_block"

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        active = context is not None
        if context is None or not context.order_blocks:
            return StrategyReport(
                name="order_block", active=active, applicable=False, direction="NEUTRAL",
                preconditions=["an active order block exists"],
                missing_confirmations=["no order block in market context"],
                reason="no order blocks in market context",
            )

        # Active = VALID or PARTIALLY_MITIGATED (mirrors context.major_order_blocks).
        active_obs = [
            o for o in context.order_blocks
            if o.get("state") in ("VALID", "PARTIALLY_MITIGATED")
        ]
        if not active_obs:
            return StrategyReport(
                name="order_block", active=active, applicable=False, direction="NEUTRAL",
                preconditions=["an active order block exists"],
                confirmations=[
                    f"{o.get('direction')} state={o.get('state')}"
                    for o in context.order_blocks[:3]
                ],
                missing_confirmations=["no VALID/PARTIALLY_MITIGATED order block"],
                invalidations=["order block not in active state"],
                reason="order blocks present but none active/valid",
            )

        ob = active_obs[-1]  # most recent active OB
        direction = (
            "BULLISH" if ob.get("direction") == "BULLISH_OB"
            else "BEARISH" if ob.get("direction") == "BEARISH_OB"
            else "NEUTRAL"
        )
        price = context.current_price
        ob_low = ob.get("low")
        ob_high = ob.get("high")
        near = (
            ob_low is not None and ob_high is not None
            and ob_low <= price <= ob_high
        )
        confirmations = [f"active OB {ob.get('direction')} state={ob.get('state')}"]
        if near:
            confirmations.append("price inside order block zone")
        invalidations = []
        if ob.get("state") in ("MITIGATED", "INVALIDATED", "EXPIRED"):
            invalidations.append("order block invalidated/consumed")
        return StrategyReport(
            name="order_block", active=active, applicable=direction != "NEUTRAL",
            direction=direction,
            preconditions=["an active order block exists"],
            confirmations=confirmations,
            missing_confirmations=[] if near else ["price not yet at order block"],
            invalidations=invalidations,
            confidence=float(ob.get("quality_score", 0.0) or 0.0),
            candidate_setup=(
                {"entry_zone_low": ob_low, "entry_zone_high": ob_high}
                if ob_low is not None and ob_high is not None else None
            ),
            reason=f"order block {ob.get('direction')} ({ob.get('state')})",
        )


@register_strategy
class FVGStrategy(Strategy):
    """Reports status from the most recent FVG in context.

    Reads ``context.fvgs`` (each entry is an FVG ``to_dict`` with keys
    ``direction`` ("BULLISH"/"BEARISH"), ``status`` (FVGState value),
    ``lower_boundary``, ``upper_boundary``, ``filled_percentage``).
    """

    name = "fvg"

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        active = context is not None
        if context is None or not context.fvgs:
            return StrategyReport(
                name="fvg", active=active, applicable=False, direction="NEUTRAL",
                preconditions=["an FVG exists"],
                missing_confirmations=["no FVG in market context"],
                reason="no FVGs in market context",
            )

        fvg = context.fvgs[-1]  # most recent FVG
        direction = (
            "BULLISH" if fvg.get("direction") == "BULLISH"
            else "BEARISH" if fvg.get("direction") == "BEARISH"
            else "NEUTRAL"
        )
        status = fvg.get("status")
        price = context.current_price
        low_b = fvg.get("lower_boundary")
        high_b = fvg.get("upper_boundary")
        in_fvg = (
            low_b is not None and high_b is not None and low_b <= price <= high_b
        )
        confirmations = [f"FVG {fvg.get('direction')} status={status}"]
        if in_fvg:
            confirmations.append("price inside FVG")
        invalidations = []
        if status in ("FILLED", "INVALIDATED") or fvg.get("fully_filled"):
            invalidations.append("FVG filled/invalidated")
        filled = float(fvg.get("filled_percentage", 0.0) or 0.0)
        confidence = round((1.0 - filled) * 100.0, 1)
        return StrategyReport(
            name="fvg", active=active, applicable=direction != "NEUTRAL",
            direction=direction,
            preconditions=["an FVG exists"],
            confirmations=confirmations,
            missing_confirmations=[] if in_fvg else ["price not yet in FVG"],
            invalidations=invalidations,
            confidence=confidence,
            candidate_setup=(
                {"entry_zone_low": low_b, "entry_zone_high": high_b}
                if low_b is not None and high_b is not None else None
            ),
            reason=f"FVG {fvg.get('direction')} ({status}, fill={filled:.0%})",
        )


@register_strategy
class LiquidityStrategy(Strategy):
    """Reports status from a recent liquidity sweep in context.

    A sweep of sell-side liquidity (a LOW pool: SWING_LOW/PDL/etc., sitting
    below price) → BUY bias (sweep + reversal). A sweep of buy-side liquidity
    (a HIGH pool) → SELL bias. Reads ``context.liquidity_sweeps`` (each entry
    is ``{"pool_type": <LiquidityType>, "price": float, "state": "SWEPT"}``).
    """

    name = "liquidity"

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        active = context is not None
        if context is None or not context.liquidity_sweeps:
            return StrategyReport(
                name="liquidity", active=active, applicable=False, direction="NEUTRAL",
                preconditions=["a liquidity sweep exists"],
                missing_confirmations=["no liquidity sweep in market context"],
                reason="no liquidity sweeps in market context",
            )

        sweep = context.liquidity_sweeps[-1]  # most recent sweep
        pool_type = (sweep.get("pool_type") or "").upper()
        # Sell-side liquidity = a LOW pool (below market) → sweep is bullish.
        # Buy-side liquidity = a HIGH pool (above market) → sweep is bearish.
        # Use explicit LiquidityType sets (substring checks miss PDH/PWL/etc.).
        _LOW_POOLS = {
            "EQUAL_LOW", "SWING_LOW", "PDL", "PWL", "SESSION_LOW", "RANGE_LOW",
        }
        _HIGH_POOLS = {
            "EQUAL_HIGH", "SWING_HIGH", "PDH", "PWH", "SESSION_HIGH", "RANGE_HIGH",
        }
        if pool_type in _LOW_POOLS:
            direction = "BULLISH"
        elif pool_type in _HIGH_POOLS:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"
        confirmations = [f"sweep of {pool_type} @ {sweep.get('price')}"]
        invalidations = []
        if sweep.get("state") in ("INVALIDATED",):
            invalidations.append("sweep pool invalidated")
        return StrategyReport(
            name="liquidity", active=active, applicable=direction != "NEUTRAL",
            direction=direction,
            preconditions=["a liquidity sweep exists"],
            confirmations=confirmations,
            missing_confirmations=[],
            invalidations=invalidations,
            confidence=50.0,  # sweep dict carries no strength field; nominal
            reason=f"liquidity sweep {pool_type} → {direction}",
        )


@register_strategy
class StructureStrategy(Strategy):
    """Reports status from market structure bias in context.

    Reads ``context.structure_state`` (LTF bias) and ``context.market_structure``
    (HTF regime); a bullish bias → BUY, bearish → SELL. (BOS/CHoCH events are
    not stored directly on the context snapshot, so the resulting structure bias
    is the available signal.)
    """

    name = "structure"

    def evaluate(
        self,
        symbol: str,
        context: Optional[MarketContextState],
        engine: Any,
        for_signals: bool = False,
    ) -> StrategyReport:
        active = context is not None
        if context is None:
            return StrategyReport(
                name="structure", active=False, applicable=False, direction="NEUTRAL",
                preconditions=["structure state exists"],
                missing_confirmations=["no market context"],
                reason="no market context",
            )

        bias = context.structure_state or context.market_structure or context.trend
        direction = _bias_to_direction(bias)
        if not bias:
            return StrategyReport(
                name="structure", active=active, applicable=False, direction="NEUTRAL",
                preconditions=["structure state exists"],
                missing_confirmations=["no structure bias in context"],
                reason="no market structure bias in context",
            )

        confirmations = [f"structure bias={bias}"]
        if context.volatility_regime:
            confirmations.append(f"volatility regime={context.volatility_regime}")
        invalidations = []
        # Opposite CHoCH: if an opposite bias is also present in MTF map, flag it.
        mtf = context.multi_timeframe_context or {}
        opposite = "BEARISH" if direction == "BULLISH" else "BULLISH"
        if any(_bias_to_direction(v) == opposite for v in mtf.values()):
            invalidations.append("opposite bias on another timeframe (CHoCH)")
        confidence = round(float(getattr(context, "trend_strength", 0.0) or 0.0) * 100.0, 1)
        return StrategyReport(
            name="structure", active=active, applicable=direction != "NEUTRAL",
            direction=direction,
            preconditions=["structure state exists"],
            confirmations=confirmations,
            missing_confirmations=[],
            invalidations=invalidations,
            confidence=confidence,
            reason=f"market structure {bias}",
        )
