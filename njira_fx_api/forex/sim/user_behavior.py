"""
Seeded user-behaviour simulation (trade.md §12, §5 step "User Behavior Layer").

The engine is "not a strategy" — it models a real human who hesitates, skips,
chases and breaks rules.  Every deviation is drawn from the run's seeded RNG and
emitted as a :class:`DeviationEvent` with its PnL impact, so the journal can show
what the trader cost themselves.  Deterministic: same seed → same deviations.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional


class DeviationType(str):
    REACTION_DELAY = "reaction_delay"
    CONFIRMATION_DELAY = "confirmation_delay"
    SKIP_SIGNAL = "skip_signal"
    EARLY_EXIT = "early_exit"
    LATE_EXIT = "late_exit"
    MOVE_SL = "move_sl"
    SIZE_OVERRIDE = "size_override"
    REVENGE_TRADE = "revenge_trade"
    HESITATION = "hesitation"
    OVERTRADE = "overtrade"


@dataclass
class DeviationConfig:
    """Per-behaviour probability + severity knobs (trade.md §12)."""

    skip_probability: float = 0.02
    early_exit_probability: float = 0.05
    late_exit_probability: float = 0.03
    move_sl_probability: float = 0.02
    size_override_probability: float = 0.02
    revenge_probability: float = 0.05      # after a loss
    overtrade_probability: float = 0.03    # after a win streak
    early_exit_fraction: float = 0.5       # take ~50% of the way to TP
    enabled: bool = True


@dataclass
class DeviationEvent:
    """One logged deviation with its PnL impact (trade.md §6 User deviations)."""

    deviation_type: str
    probability_used: float
    severity: str = "low"
    pnl_impact_usd: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "deviation_type": self.deviation_type,
            "probability_used": round(self.probability_used, 4),
            "severity": self.severity,
            "pnl_impact_usd": round(self.pnl_impact_usd, 4),
            "reason": self.reason,
        }


class UserBehaviorService:
    """
    Decides whether the simulated trader deviates on a given signal/trade.

    All draws come from ``rng`` (seeded per run) so replays are identical.  The
    service returns :class:`DeviationEvent` objects (possibly several, or none);
    the broker applies them to the position.
    """

    def __init__(self, rng: random.Random, config: Optional[DeviationConfig] = None) -> None:
        self._rng = rng
        self.config = config or DeviationConfig()

    def _fire(self, probability: float) -> bool:
        return self.config.enabled and probability > 0 and self._rng.random() < probability

    def on_signal(
        self, *, last_result: Optional[str] = None, loss_streak: int = 0, win_streak: int = 0
    ) -> list[DeviationEvent]:
        """
        Deviations applied BEFORE the order is sent (trade.md §5, between 3 and 19).

        ``last_result`` is "win"/"loss"/None; streaks feed revenge/overtrade.
        A ``skip`` deviation means the signal is NOT taken.
        """
        events: list[DeviationEvent] = []
        cfg = self.config

        if self._fire(cfg.skip_probability):
            events.append(DeviationEvent(
                DeviationType.SKIP_SIGNAL, cfg.skip_probability,
                severity="low", reason="fatigue / skipped signal",
            ))
            return events  # skip short-circuits the rest

        # Revenge trade after a loss; hesitation on a losing streak.
        if loss_streak >= 2 and self._fire(cfg.revenge_probability):
            events.append(DeviationEvent(
                DeviationType.REVENGE_TRADE, cfg.revenge_probability,
                severity="high", reason=f"revenge after {loss_streak} losses",
            ))
        elif loss_streak >= 3 and self._fire(cfg.overtrade_probability):
            events.append(DeviationEvent(
                DeviationType.HESITATION, cfg.overtrade_probability,
                severity="medium", reason="hesitation after losing streak",
            ))

        if win_streak >= 3 and self._fire(cfg.overtrade_probability):
            events.append(DeviationEvent(
                DeviationType.OVERTRADE, cfg.overtrade_probability,
                severity="medium", reason=f"overtrade after {win_streak} wins",
            ))

        return events

    def on_open(self) -> list[DeviationEvent]:
        """Deviations applied at/after fill (stop move, size override)."""
        events: list[DeviationEvent] = []
        cfg = self.config
        if self._fire(cfg.move_sl_probability):
            events.append(DeviationEvent(
                DeviationType.MOVE_SL, cfg.move_sl_probability,
                severity="high", reason="moved stop away from price",
            ))
        if self._fire(cfg.size_override_probability):
            events.append(DeviationEvent(
                DeviationType.SIZE_OVERRIDE, cfg.size_override_probability,
                severity="medium", reason="manual size override",
            ))
        return events

    def early_exit_price(self, entry: float, tp: float, sl: float) -> Optional[float]:
        """If the trader bails early, return the (worse) exit price; else None."""
        if not self._fire(self.config.early_exit_probability):
            return None
        frac = self.config.early_exit_fraction
        return entry + (tp - entry) * frac
