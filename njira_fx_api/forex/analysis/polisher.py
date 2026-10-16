"""
Signal Polisher — pure, stateless-ish module for signal filtering.

Extracts the inline threshold checks from SignalEngine into a dedicated,
testable module that returns a decision + audit trace. No lifecycle calls,
no broker/DB/clock access.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class PolishTraceEntry:
    """Single filter trace entry for auditability."""
    filter_name: str
    passed: bool
    reason: str


@dataclass
class PolishResult:
    """Result of the polishing pipeline."""
    action: str  # "PASS" | "DROP" | "HOLD"
    reason_code: str
    trace: list[PolishTraceEntry]
    contradiction_report: Any = None
    quality_gate_report: Any = None


class SignalPolisher:
    """
    Stateless signal polishing pipeline.

    Filter order (each appends a trace entry):
    1. min_confidence — DROP if confidence < min_confidence
    2. min_rr — DROP if risk_reward < min_rr
    3. noise_floor — DROP if confidence < noise_floor
    4. quality_gate — if gate present and not passed → HOLD
    5. contradiction — if engine present → run analyze(); INVALIDATE → DROP, WAIT → HOLD

    PASS when all filters pass. Every filter appends a trace entry.
    """

    def __init__(
        self,
        min_confidence: float,
        min_rr: float,
        noise_floor: float,
        quality_gate: Optional[Any] = None,
        contradiction: Optional[Any] = None,
    ) -> None:
        self._min_confidence = min_confidence
        self._min_rr = min_rr
        self._noise_floor = noise_floor
        self._quality_gate = quality_gate
        self._contradiction = contradiction

    def polish(
        self,
        symbol: str,
        direction: str,
        confidence: float,
        risk_reward: float,
        evidence: dict,
        market_state: dict,
        *,
        apply_min_confidence: bool = False,
        hold_on_contradiction: bool = True,
    ) -> PolishResult:
        """Run the full polishing pipeline and return a decision + trace.

        Args:
            apply_min_confidence: If True, enforce the min_confidence filter.
                Default False — the confluence path only enforces noise_floor.
                The MTF path explicitly passes True to enforce min_confidence.
        """
        trace: list[PolishTraceEntry] = []

        # 1. min_confidence (optional — confluence path skips this)
        if apply_min_confidence:
            if confidence < self._min_confidence:
                trace.append(PolishTraceEntry(
                    filter_name="min_confidence",
                    passed=False,
                    reason="confidence_below_min",
                ))
                return PolishResult(
                    action="DROP",
                    reason_code="confidence_below_min",
                    trace=trace,
                )
            trace.append(PolishTraceEntry(
                filter_name="min_confidence",
                passed=True,
                reason=f"confidence {confidence:.1f} >= {self._min_confidence:.1f}",
            ))
        else:
            trace.append(PolishTraceEntry(
                filter_name="min_confidence",
                passed=True,
                reason="skipped_for_confluence_path",
            ))

        # 2. min_rr
        if risk_reward < self._min_rr:
            trace.append(PolishTraceEntry(
                filter_name="min_rr",
                passed=False,
                reason="rr_below_min",
            ))
            return PolishResult(
                action="DROP",
                reason_code="rr_below_min",
                trace=trace,
            )
        trace.append(PolishTraceEntry(
            filter_name="min_rr",
            passed=True,
            reason=f"risk_reward {risk_reward:.2f} >= {self._min_rr:.2f}",
        ))

        # 3. noise_floor
        if confidence < self._noise_floor:
            trace.append(PolishTraceEntry(
                filter_name="noise_floor",
                passed=False,
                reason="noise_floor",
            ))
            return PolishResult(
                action="DROP",
                reason_code="noise_floor",
                trace=trace,
            )
        trace.append(PolishTraceEntry(
            filter_name="noise_floor",
            passed=True,
            reason=f"confidence {confidence:.1f} >= {self._noise_floor:.1f}",
        ))

        # 4. quality_gate
        qg_report = None
        if self._quality_gate is not None:
            qg_report = self._quality_gate.evaluate(
                symbol, direction, evidence, market_state
            )
            if not qg_report.passed:
                trace.append(PolishTraceEntry(
                    filter_name="quality_gate",
                    passed=False,
                    reason="quality_gate_failed",
                ))
                return PolishResult(
                    action="HOLD",
                    reason_code="quality_gate_failed",
                    trace=trace,
                    quality_gate_report=qg_report,
                )
            trace.append(PolishTraceEntry(
                filter_name="quality_gate",
                passed=True,
                reason="quality_gate_passed",
            ))
        else:
            trace.append(PolishTraceEntry(
                filter_name="quality_gate",
                passed=True,
                reason="no_quality_gate_configured",
            ))

        # 5. contradiction
        contra_report = None
        if self._contradiction is not None:
            contra_report = self._contradiction.analyze(
                symbol, direction, evidence, market_state
            )
            if contra_report.recommendation == "INVALIDATE":
                trace.append(PolishTraceEntry(
                    filter_name="contradiction",
                    passed=False,
                    reason="contradiction_invalidate",
                ))
                return PolishResult(
                    action="DROP",
                    reason_code="contradiction_invalidate",
                    trace=trace,
                    contradiction_report=contra_report,
                )
            if contra_report.recommendation == "WAIT" and hold_on_contradiction:
                trace.append(PolishTraceEntry(
                    filter_name="contradiction",
                    passed=False,
                    reason="contradiction_wait",
                ))
                return PolishResult(
                    action="HOLD",
                    reason_code="contradiction_wait",
                    trace=trace,
                    contradiction_report=contra_report,
                )
            if contra_report.recommendation in ("TRADE", "WAIT"):
                # WAIT here is advisory (hold_on_contradiction=False): the ICT
                # contradiction engine checks for FVG/OB/displacement the book
                # SNR setups don't produce, so it flags them MODERATE. The book's
                # OWN validation (NO VALIDATION NO TRADE, confluence, session) is
                # the real filter for the SNR family; the report is still attached
                # as evidence.
                trace.append(PolishTraceEntry(
                    filter_name="contradiction",
                    passed=True,
                    reason=(f"contradiction_{contra_report.recommendation.lower()} "
                            f"(net={contra_report.net_confidence:.0f})"),
                ))
        else:
            trace.append(PolishTraceEntry(
                filter_name="contradiction",
                passed=True,
                reason="no_contradiction_engine_configured",
            ))

        # All filters passed
        return PolishResult(
            action="PASS",
            reason_code="all_filters_passed",
            trace=trace,
            contradiction_report=contra_report,
            quality_gate_report=qg_report,
        )