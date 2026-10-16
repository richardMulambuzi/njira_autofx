"""
Dynamic RR mapping (trade.md §3) — strength-driven, never fixed 1:2/1:3.

Pure service — the engine CONSUMES the strength score, it does not compute it.
The final RR is::

    base_rr   = min_rr + (max_rr - min_rr) * strength ** 1.3
    scaled    = base_rr * volatility_mult * regime_mult * performance_mult
    final_rr  = min(scaled, structural_rr)          # §3.7 structural cap
    # then break-even validation (§3.9) may raise final_rr up to structural_rr
    # or reject the signal.

NOTE on §3.9 — the spec says "reduce RR until the break-even margin is met".
That is mathematically inverted: break-even win rate is ``1 / (1 + rr)``, so
LOWERING rr RAISES the win rate you need to break even. To satisfy
``hist_wr >= break_even + 0.05`` you must RAISE rr (bounded by structure), not
lower it. This module implements the correct behaviour and raises final_rr up to
the structural cap; if the cap cannot satisfy the margin, the signal is
rejected. (See ``RRCalculation.margin_of_safety`` and the module tests.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StrategyType(str, Enum):
    SCALP = "scalp"
    MEAN_REVERSION = "mean_reversion"
    RANGE = "range"
    PULLBACK = "pullback"
    TREND_CONTINUATION = "trend_continuation"
    BREAKOUT = "breakout"
    REVERSAL = "reversal"
    NEWS = "news_event"
    LIQUIDITY_SWEEP = "liquidity_sweep"


class Regime(str, Enum):
    STRONG_TREND = "strong_trend"
    WEAK_TREND = "weak_trend"
    RANGE = "range"
    CHOPPY = "choppy"


# §3.2 — strategy-specific RR ranges.
STRATEGY_RR: dict[StrategyType, tuple[float, float]] = {
    StrategyType.SCALP: (1.0, 2.0),
    StrategyType.MEAN_REVERSION: (1.0, 2.5),
    StrategyType.RANGE: (1.5, 3.0),
    StrategyType.PULLBACK: (1.5, 4.0),
    StrategyType.TREND_CONTINUATION: (1.5, 5.0),
    StrategyType.BREAKOUT: (2.0, 6.0),
    StrategyType.REVERSAL: (2.0, 5.0),
    StrategyType.NEWS: (1.5, 3.0),
    StrategyType.LIQUIDITY_SWEEP: (2.0, 5.5),
}

# §3.4 — volatility multiplier by ATR percentile (over the last 100 bars).
VOL_BANDS: tuple[tuple[float, float], ...] = (
    (0.10, 0.70),
    (0.30, 0.85),
    (0.70, 1.00),
    (0.90, 1.15),
    (1.01, 1.30),  # above the 90th
)
VOL_CLAMP = (0.70, 1.30)

# §3.5 — regime multiplier.
REGIME_MULT: dict[Regime, float] = {
    Regime.STRONG_TREND: 1.20,
    Regime.WEAK_TREND: 1.00,
    Regime.RANGE: 0.85,
    Regime.CHOPPY: 0.70,  # choppy → 0.70 (the "or SKIP" path is RiskService's job)
}

# §3.6 — performance multiplier from recent expectancy (last 50 trades).
PERF_ABOVE, PERF_POSITIVE, PERF_NEUTRAL, PERF_NEGATIVE = 1.10, 1.00, 0.95, 0.80

MARGIN_OF_SAFETY = 0.05  # §3.9 — hist_wr must beat break-even by 5pp

# The §3.9 break-even check compares a target win rate against the strategy's
# HISTORICAL win rate.  With a handful of closed trades that historical rate is
# noise (one loss ⇒ hist_wr = 0 ⇒ every future signal is rejected forever).  The
# spec assumes 200+ trades of history; here we gate the check behind a minimum
# sample so it only bites once the rate is statistically meaningful.  Below the
# threshold the check is skipped (the structural cap and min-viable still apply).
MIN_SAMPLES_FOR_BREAKEVEN = 30


@dataclass
class RRCalculation:
    """Full RR chain for one signal (trade.md §3.4/§6 — journal RR section)."""

    accepted: bool
    base_rr: float
    volatility_mult: float
    regime_mult: float
    performance_mult: float
    structural_rr: float
    final_rr: float
    break_even_wr: float
    historical_wr: float
    margin_of_safety: float
    reason: str = ""
    rr_reductions: list[float] = field(default_factory=list)  # rr adjustments made

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "base_rr": round(self.base_rr, 4),
            "volatility_mult": self.volatility_mult,
            "regime_mult": self.regime_mult,
            "performance_mult": self.performance_mult,
            "structural_rr": round(self.structural_rr, 4),
            "final_rr": round(self.final_rr, 4),
            "break_even_wr": round(self.break_even_wr, 4),
            "historical_wr": round(self.historical_wr, 4),
            "margin_of_safety": round(self.margin_of_safety, 4),
            "reason": self.reason,
            "rr_adjustments": [round(x, 4) for x in self.rr_reductions],
        }


def volatility_multiplier(atr_percentile: float) -> float:
    """§3.4 — multiplier from ATR percentile (0..1); clamped 0.70..1.30."""
    for edge, mult in VOL_BANDS:
        if atr_percentile < edge:
            return max(VOL_CLAMP[0], min(VOL_CLAMP[1], mult))
    return VOL_CLAMP[1]


def performance_multiplier(recent_expectancy_r: Optional[float]) -> float:
    """§3.6 — multiplier from recent expectancy in R over the last 50 trades."""
    if recent_expectancy_r is None:
        return PERF_NEUTRAL
    if recent_expectancy_r > 0.15:      # "positive above threshold"
        return PERF_ABOVE
    if recent_expectancy_r > 0.0:       # "positive below threshold"
        return PERF_POSITIVE
    if recent_expectancy_r == 0.0:
        return PERF_NEUTRAL
    return PERF_NEGATIVE                # negative (PAUSE handled by RiskService)


class RRService:
    """Deterministic dynamic-RR engine (trade.md §3)."""

    def calculate(
        self,
        *,
        strength: float,
        strategy_type: StrategyType,
        atr_percentile: float = 0.5,
        regime: Regime = Regime.WEAK_TREND,
        recent_expectancy_r: Optional[float] = None,
        historical_wr: Optional[float] = None,
        structural_rr: Optional[float] = None,
        samples: Optional[int] = None,
    ) -> RRCalculation:
        """
        Compute the RR chain.  ``structural_rr`` is the RR the market structure
        allows (next swing/level distance over stop distance); when omitted the
        scaled RR stands uncapped.  ``historical_wr`` (0..1) enables the §3.9
        break-even validation; ``samples`` is the closed-trade count behind it —
        the check is skipped until it reaches ``MIN_SAMPLES_FOR_BREAKEVEN`` so a
        tiny, noisy history cannot reject every signal.
        """
        strength = max(0.0, min(1.0, strength))
        min_rr, max_rr = STRATEGY_RR[strategy_type]

        # §3.3 — power curve rewards strong signals disproportionately.
        base_rr = min_rr + (max_rr - min_rr) * (strength ** 1.3)
        vol = volatility_multiplier(atr_percentile)
        reg = REGIME_MULT[regime]
        perf = performance_multiplier(recent_expectancy_r)

        scaled = base_rr * vol * reg * perf
        cap = structural_rr if structural_rr is not None else scaled
        final_rr = min(scaled, cap)

        adjustments: list[float] = []

        # §3.7 — if structure allows less than the strategy minimum, reject.
        if structural_rr is not None and structural_rr < min_rr:
            return RRCalculation(
                accepted=False, base_rr=base_rr, volatility_mult=vol,
                regime_mult=reg, performance_mult=perf, structural_rr=structural_rr,
                final_rr=final_rr, break_even_wr=0.0,
                historical_wr=historical_wr or 0.0, margin_of_safety=0.0,
                reason=(
                    f"structural RR {structural_rr:.2f} below min viable "
                    f"{min_rr:.2f} for {strategy_type.value}"
                ),
            )

        if historical_wr is None or (samples is not None and samples < MIN_SAMPLES_FOR_BREAKEVEN):
            # No (or not-yet-meaningful) performance history → skip §3.9.
            reason = ("no historical win rate — break-even check skipped"
                      if historical_wr is None
                      else f"only {samples} closed trades — break-even check skipped")
            return RRCalculation(
                accepted=True, base_rr=base_rr, volatility_mult=vol,
                regime_mult=reg, performance_mult=perf, structural_rr=cap,
                final_rr=final_rr, break_even_wr=1.0 / (1.0 + final_rr),
                historical_wr=historical_wr or 0.0, margin_of_safety=0.0,
                reason=reason,
            )

        historical_wr = max(0.0, min(1.0, historical_wr))

        def margin(rr: float) -> tuple[float, float]:
            be = 1.0 / (1.0 + rr) if rr > 0 else 1.0
            return be, historical_wr - be

        be, mos = margin(final_rr)
        if mos >= MARGIN_OF_SAFETY:
            return RRCalculation(
                accepted=True, base_rr=base_rr, volatility_mult=vol,
                regime_mult=reg, performance_mult=perf, structural_rr=cap,
                final_rr=final_rr, break_even_wr=be, historical_wr=historical_wr,
                margin_of_safety=mos, rr_reductions=adjustments, reason="ok",
            )

        # §3.9 (corrected) — margin not met. Raising rr lowers break-even, so
        # find the smallest rr <= structural cap that clears the margin.
        if historical_wr - MARGIN_OF_SAFETY <= 0:
            return RRCalculation(
                accepted=False, base_rr=base_rr, volatility_mult=vol,
                regime_mult=reg, performance_mult=perf, structural_rr=cap,
                final_rr=final_rr, break_even_wr=be, historical_wr=historical_wr,
                margin_of_safety=mos,
                reason="historical win rate too low to clear break-even + 5pp",
            )
        needed_rr = 1.0 / (historical_wr - MARGIN_OF_SAFETY) - 1.0
        if needed_rr <= cap + 1e-9:
            new_rr = min(max(final_rr, needed_rr), cap)
            adjustments.append(new_rr)
            be, mos = margin(new_rr)
            return RRCalculation(
                accepted=True, base_rr=base_rr, volatility_mult=vol,
                regime_mult=reg, performance_mult=perf, structural_rr=cap,
                final_rr=new_rr, break_even_wr=be, historical_wr=historical_wr,
                margin_of_safety=mos, rr_reductions=adjustments,
                reason=f"RR raised to {new_rr:.2f} to clear break-even margin",
            )
        return RRCalculation(
            accepted=False, base_rr=base_rr, volatility_mult=vol,
            regime_mult=reg, performance_mult=perf, structural_rr=cap,
            final_rr=final_rr, break_even_wr=be, historical_wr=historical_wr,
            margin_of_safety=mos,
            reason=(
                f"cannot clear break-even margin within structure "
                f"(need RR {needed_rr:.2f} > cap {cap:.2f})"
            ),
        )
