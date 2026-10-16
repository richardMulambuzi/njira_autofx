"""
Position sizing with a small account (trade.md §2).

Pure service — no Django, no I/O, deterministic.  Given a risk budget, equity and
a stop distance it returns the (broker-legal) position size, the ACTUAL risk that
size implies after rounding, and the accept/flag/reject decision.

Rounding rule (trade.md §2.3): always round DOWN to the broker lot step — never
up, because rounding up silently increases risk.  If rounding down lands below the
broker minimum, the minimum is used, which can push the ACTUAL risk above the
intended 1%; that is caught by the over-risk reject, not hidden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .instruments import ContractSpec, get_spec


# trade.md §8 — max risk per trade tolerated after rounding.
MAX_RISK_PCT = 0.0125   # 1.25%  → reject above this
MIN_RISK_PCT = 0.005    # 0.5%   → accept but FLAG undersized below this


@dataclass
class SizingResult:
    """Outcome of one sizing calculation (trade.md §2.4 ``SizingResult``)."""

    accepted: bool
    ideal_size: float          # unrounded lots
    rounded_size: float        # broker-legal lots
    size_step_used: float
    intended_risk: float       # USD we set out to risk
    actual_risk: float         # USD actually at risk after rounding
    actual_risk_pct: float     # actual_risk / equity
    stop_pips: float
    undersized: bool = False
    over_risk_reject: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "ideal_size": self.ideal_size,
            "rounded_size": self.rounded_size,
            "size_step_used": self.size_step_used,
            "intended_risk": self.intended_risk,
            "actual_risk": self.actual_risk,
            "actual_risk_pct": self.actual_risk_pct,
            "stop_pips": self.stop_pips,
            "undersized": self.undersized,
            "over_risk_reject": self.over_risk_reject,
            "reason": self.reason,
        }


class PositionSizingService:
    """Deterministic position sizing (trade.md §2.1 formula)."""

    def __init__(self, spec: Optional[ContractSpec] = None) -> None:
        self._spec = spec

    def _spec_for(self, symbol: str) -> ContractSpec:
        return self._spec or get_spec(symbol)

    def size(
        self,
        *,
        symbol: str,
        equity: float,
        risk_amount: float,
        entry_price: float,
        stop_price: float,
    ) -> SizingResult:
        """
        Size a trade.

        ``risk_amount`` is the USD budget (1% of equity per trade.md §1).
        ``entry_price``/``stop_price`` define the stop distance.  Returns a
        :class:`SizingResult` whose ``accepted`` is False when the broker
        minimum forces a position whose actual risk exceeds ``MAX_RISK_PCT``.
        """
        spec = self._spec_for(symbol)
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return SizingResult(
                accepted=False, ideal_size=0.0, rounded_size=0.0,
                size_step_used=spec.lot_step, intended_risk=risk_amount,
                actual_risk=0.0, actual_risk_pct=0.0, stop_pips=0.0,
                over_risk_reject=False, reason="zero stop distance",
            )

        stop_pips = spec.pips(stop_distance)
        pip_value = spec.pip_value_per_lot
        if pip_value <= 0:
            return SizingResult(
                accepted=False, ideal_size=0.0, rounded_size=0.0,
                size_step_used=spec.lot_step, intended_risk=risk_amount,
                actual_risk=0.0, actual_risk_pct=0.0, stop_pips=stop_pips,
                over_risk_reject=False, reason="zero pip value",
            )

        # §2.1 — position_size = risk_amount / (stop_distance * value_per_point)
        ideal = risk_amount / (stop_pips * pip_value)

        # §2.3 — round DOWN to the broker step (never up).
        rounded = spec.round_lots(ideal)
        # The broker will not accept less than min_lot; using it can over-risk.
        if rounded < spec.min_lot:
            rounded = spec.min_lot

        actual_risk = rounded * stop_pips * pip_value
        actual_pct = (actual_risk / equity) if equity > 0 else float("inf")

        if actual_pct > MAX_RISK_PCT:
            return SizingResult(
                accepted=False, ideal_size=ideal, rounded_size=rounded,
                size_step_used=spec.lot_step, intended_risk=risk_amount,
                actual_risk=actual_risk, actual_risk_pct=actual_pct,
                stop_pips=stop_pips, over_risk_reject=True,
                reason=(
                    f"broker min size forces {actual_pct:.2%} risk "
                    f"(> {MAX_RISK_PCT:.2%} max)"
                ),
            )

        undersized = actual_pct < MIN_RISK_PCT
        return SizingResult(
            accepted=True, ideal_size=ideal, rounded_size=rounded,
            size_step_used=spec.lot_step, intended_risk=risk_amount,
            actual_risk=actual_risk, actual_risk_pct=actual_pct,
            stop_pips=stop_pips, undersized=undersized,
            reason="undersized (rounding) — flagged" if undersized else "",
        )
