"""
Signal Quality Gate (improve.md §37–§38 — audit sequencing item 8).

Two gates:

1. ``check_data_quality`` (§37) — a pre-check run before ANY signal is
   generated. If critical market data is missing/stale the engine must emit
   NO SIGNAL.

2. ``QualityGate.evaluate`` (§38) — a structured 15-question gate every signal
   must pass before it is classified. Each question is answered best-effort
   from the shared :class:`MarketContextState` plus the contradiction / retest
   / lifecycle engines. Critical questions that fail force the signal into
   WAITING instead of BUY/SELL.

Best-effort by design: every question is wrapped so a missing field or a
raising engine yields ``UNKNOWN`` rather than breaking the pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from forex.analysis.context import MarketContextState
from forex.analysis.contradiction import ContradictionEngine
from forex.analysis.retest import RetestEngine
from forex.analysis.signal_lifecycle import SignalLifecycleManager

logger = logging.getLogger("forex.analysis.quality_gate")


# Question text for the 15-question signal quality gate (improve.md §38).
QUESTIONS: dict[int, str] = {
    1: "What is the market structure?",
    2: "What is the higher-timeframe direction?",
    3: "Where is price relative to major liquidity?",
    4: "Where is price relative to the daily high/low?",
    5: "What session are we in?",
    6: "Is price at a meaningful location?",
    7: "Is there a meaningful Order Block?",
    8: "Is there an FVG or displacement?",
    9: "What does Volume Profile say?",
    10: "Did price react?",
    11: "Was there a retest?",
    12: "Do lower timeframes confirm?",
    13: "Do the selected strategies agree?",
    14: "What evidence contradicts the setup?",
    15: "What invalidates it?",
}

# Critical questions block classification (→ WAITING) when they fail.
CRITICAL_QUESTIONS: frozenset[int] = frozenset({1, 2, 4, 6, 14, 15})

# Required timeframes for the data-quality gate (§37).
_REQUIRED_TFS: frozenset[int] = frozenset({60, 300, 900, 3600})
_STALE_SECONDS: float = 300.0  # 5 minutes


@dataclass
class QualityGateAnswer:
    """One answered question of the signal quality gate."""

    question_id: int
    question: str
    answer: str
    passed: bool
    severity: str  # "critical" | "warning" | "info"

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "answer": self.answer,
            "passed": self.passed,
            "severity": self.severity,
        }


@dataclass
class QualityGateReport:
    """Result of running the 15-question signal quality gate."""

    answers: list[QualityGateAnswer] = field(default_factory=list)
    passed: bool = True
    critical_failures: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "critical_failures": self.critical_failures,
            "summary": self.summary,
            "answers": [a.to_dict() for a in self.answers],
        }


class QualityGate:
    """
    Structured signal quality gate (improve.md §37–§38).

    Constructed with the shared engines so it can answer every question from
    real, already-computed state instead of re-querying the hot path.
    """

    def __init__(
        self,
        context_engine: Any | None = None,
        contradiction_engine: ContradictionEngine | None = None,
        retest_engine: RetestEngine | None = None,
        lifecycle: SignalLifecycleManager | None = None,
    ) -> None:
        self._context = context_engine
        self._contradiction = contradiction_engine or ContradictionEngine()
        self._retest = retest_engine
        self._lifecycle = lifecycle

    # ── Public API ──────────────────────────────────────────────────────

    def check_data_quality(
        self, symbol: str, market_state: Optional[dict] = None
    ) -> tuple[bool, list[str]]:
        """
        Data quality gate (improve.md §37).

        Returns ``(ok, issues)``. ``ok`` is False only on CRITICAL data
        problems (stale data, missing required timeframes, invalid price).
        A WIDE spread is recorded as a non-blocking warning.

        Uses the shared context engine; if it is unavailable the check is
        skipped (non-blocking) rather than falsely suppressing every signal.
        """
        if self._context is None:
            return (True, ["context engine unavailable — data quality not enforced"])
        try:
            ctx = self._context.get(symbol)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("[QUALITY] context.get failed for %s: %s", symbol, exc)
            return (True, ["context unavailable — data quality not enforced"])

        issues: list[str] = []
        critical = False

        # Data freshness
        try:
            freshness = float(ctx.data_freshness or 0.0)
        except (TypeError, ValueError):
            freshness = 0.0
        if freshness > _STALE_SECONDS:
            issues.append(
                f"stale data (freshness={freshness:.0f}s > {_STALE_SECONDS:.0f}s)"
            )
            critical = True

        # Required timeframes present in the multi-timeframe context
        mtf = ctx.multi_timeframe_context if isinstance(
            ctx.multi_timeframe_context, dict
        ) else {}
        missing = sorted(_REQUIRED_TFS - set(mtf.keys()))
        if missing:
            issues.append(f"missing required timeframes: {missing}")
            critical = True

        # Current price valid
        try:
            price_ok = isinstance(ctx.current_price, (int, float)) and ctx.current_price > 0
        except Exception:  # noqa: BLE001
            price_ok = False
        if not price_ok:
            issues.append("current price invalid (<=0)")
            critical = True

        # Spread reasonable — WIDE is a warning, not critical (§37)
        if getattr(ctx, "spread_regime", "NORMAL") == "WIDE":
            issues.append("spread WIDE (warning — not blocking)")

        if not issues:
            return (True, [])
        return (not critical, issues)

    def evaluate(
        self,
        symbol: str,
        direction: str,
        evidence: dict[str, float],
        market_state: Optional[dict] = None,
    ) -> QualityGateReport:
        """
        Answer all 15 signal-quality questions (improve.md §38).

        Each question is best-effort: any exception yields ``UNKNOWN`` with
        ``passed=False`` and ``severity="warning"``. Critical questions that
        fail on missing data yield ``severity="critical"``.
        """
        ctx: Optional[MarketContextState] = None
        try:
            if self._context is not None:
                ctx = self._context.get(symbol)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("[QUALITY] context.get failed for %s: %s", symbol, exc)
            ctx = None

        # Market state for the contradiction engine (Q14). Prefer the supplied
        # one; otherwise derive a minimal vocabulary from the context state.
        mstate = market_state
        if mstate is None and ctx is not None:
            mstate = self._market_state_from_context(ctx, direction, evidence)

        handlers = [
            self._q1, self._q2, self._q3, self._q4, self._q5,
            self._q6, self._q7, self._q8, self._q9, self._q10,
            self._q11, self._q12, self._q13, self._q14, self._q15,
        ]

        answers: list[QualityGateAnswer] = []
        for qid, handler in zip(range(1, 16), handlers):
            try:
                ans = handler(ctx, direction, evidence, mstate)
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("[QUALITY] Q%d failed for %s: %s", qid, symbol, exc)
                ans = QualityGateAnswer(
                    qid, QUESTIONS[qid], "UNKNOWN", False, "warning"
                )
            answers.append(ans)

        critical_failures = [
            f"Q{a.question_id}: {a.answer}"
            for a in answers
            if not a.passed and a.severity == "critical"
        ]
        passed = len(critical_failures) == 0
        answered = sum(1 for a in answers if a.answer != "UNKNOWN")
        summary = (
            "PASSED" if passed else "FAILED"
        ) + f" ({answered}/15 answered, {len(critical_failures)} critical failures)"
        return QualityGateReport(
            answers=answers,
            passed=passed,
            critical_failures=critical_failures,
            summary=summary,
        )

    # ── Question handlers ───────────────────────────────────────────────

    def _q1(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        structure = (ctx.market_structure if ctx else "") or (
            ctx.structure_state if ctx else ""
        )
        if structure:
            return QualityGateAnswer(1, QUESTIONS[1], f"structure: {structure}", True, "critical")
        return QualityGateAnswer(1, QUESTIONS[1], "UNKNOWN — no market structure", False, "critical")

    def _q2(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(2, QUESTIONS[2], "UNKNOWN — no context", False, "critical")
        mtf = ctx.multi_timeframe_context if isinstance(ctx.multi_timeframe_context, dict) else {}
        htf = mtf.get(14400) or mtf.get(86400)
        if htf:
            tf = 14400 if 14400 in mtf else 86400
            return QualityGateAnswer(2, QUESTIONS[2], f"{tf // 3600}H {htf}", True, "critical")
        return QualityGateAnswer(2, QUESTIONS[2], "UNKNOWN — no HTF bias", False, "critical")

    def _q3(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(3, QUESTIONS[3], "UNKNOWN — no context", False, "warning")
        sweeps = ctx.liquidity_sweeps or []
        levels = ctx.liquidity_levels or []
        if sweeps:
            types = ", ".join(sorted({s.get("pool_type", "?") for s in sweeps}))
            return QualityGateAnswer(3, QUESTIONS[3], f"liquidity swept: {types}", True, "warning")
        if levels:
            return QualityGateAnswer(
                3, QUESTIONS[3], f"{len(levels)} liquidity pool(s) present", True, "warning"
            )
        return QualityGateAnswer(3, QUESTIONS[3], "N/A — no liquidity pools", False, "warning")

    def _q4(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(4, QUESTIONS[4], "UNKNOWN — no context", False, "critical")
        price = ctx.current_price
        dh, dl = ctx.daily_high, ctx.daily_low
        if price > 0 and dh > 0 and dl > 0:
            if price > dh:
                rel = "above daily high"
            elif price < dl:
                rel = "below daily low"
            else:
                rel = "within daily range"
            return QualityGateAnswer(4, QUESTIONS[4], f"price {rel}", True, "critical")
        return QualityGateAnswer(
            4, QUESTIONS[4], "UNKNOWN — daily range/price missing", False, "critical"
        )

    def _q5(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        session = ctx.session if ctx else ""
        if session:
            return QualityGateAnswer(5, QUESTIONS[5], f"session: {session}", True, "warning")
        return QualityGateAnswer(5, QUESTIONS[5], "UNKNOWN — no session", False, "warning")

    def _q6(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(6, QUESTIONS[6], "UNKNOWN — no context", False, "critical")
        zones: list[str] = []
        if ctx.major_order_blocks:
            zones.append(f"{len(ctx.major_order_blocks)} OB")
        if ctx.fvgs:
            zones.append(f"{len(ctx.fvgs)} FVG")
        if ctx.liquidity_levels:
            zones.append(f"{len(ctx.liquidity_levels)} liquidity")
        if ctx.poc and ctx.poc > 0:
            zones.append("VP")
        if zones:
            return QualityGateAnswer(6, QUESTIONS[6], f"at {'+'.join(zones)}", True, "critical")
        return QualityGateAnswer(6, QUESTIONS[6], "N/A — no meaningful location", False, "critical")

    def _q7(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(7, QUESTIONS[7], "UNKNOWN — no context", False, "warning")
        ob_dir = "BULLISH" if direction == "BUY" else "BEARISH"
        obs = [
            o for o in (ctx.major_order_blocks or [])
            if str(o.get("direction", "")).upper() == ob_dir
        ] or [
            o for o in (ctx.order_blocks or [])
            if str(o.get("direction", "")).upper() == ob_dir
        ]
        if obs:
            price = obs[0].get("price") or obs[0].get("zone_top") or obs[0].get("high")
            return QualityGateAnswer(7, QUESTIONS[7], f"{ob_dir} OB @ {price}", True, "warning")
        return QualityGateAnswer(7, QUESTIONS[7], "N/A — no active OB", False, "warning")

    def _q8(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(8, QUESTIONS[8], "UNKNOWN — no context", False, "warning")
        fvg_dir = "BULLISH" if direction == "BUY" else "BEARISH"
        fvgs = [f for f in (ctx.fvgs or []) if str(f.get("direction", "")).upper() == fvg_dir]
        if fvgs:
            return QualityGateAnswer(8, QUESTIONS[8], f"{fvg_dir} FVG present", True, "warning")
        if ctx.imbalances:
            return QualityGateAnswer(8, QUESTIONS[8], "imbalance present (no directional FVG)", True, "warning")
        return QualityGateAnswer(8, QUESTIONS[8], "N/A — no FVG/displacement", False, "warning")

    def _q9(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(9, QUESTIONS[9], "UNKNOWN — no context", False, "warning")
        if ctx.poc and ctx.poc > 0:
            return QualityGateAnswer(
                9, QUESTIONS[9],
                f"POC={ctx.poc:.5f} VAH={ctx.vah:.5f} VAL={ctx.val:.5f}",
                True, "warning",
            )
        return QualityGateAnswer(9, QUESTIONS[9], "N/A — no volume profile", False, "warning")

    def _q10(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        events = self._retest.get_recent(symbol=ctx.symbol if ctx else "", limit=10) if self._retest else []
        reacted = [e for e in events if e.reaction in ("REJECTED", "ACCEPTED")]
        if reacted:
            e = reacted[0]
            return QualityGateAnswer(
                10, QUESTIONS[10], f"{e.zone_type} reaction {e.reaction}", True, "warning"
            )
        return QualityGateAnswer(10, QUESTIONS[10], "no recent price reaction", False, "warning")

    def _q11(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        events = self._retest.get_recent(symbol=ctx.symbol if ctx else "", limit=10) if self._retest else []
        if events:
            e = events[-1]
            return QualityGateAnswer(
                11, QUESTIONS[11], f"retest {e.reaction} on {e.zone_type}", True, "warning"
            )
        return QualityGateAnswer(11, QUESTIONS[11], "no retest event", False, "warning")

    def _q12(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if ctx is None:
            return QualityGateAnswer(12, QUESTIONS[12], "UNKNOWN — no context", False, "warning")
        mtf = ctx.multi_timeframe_context if isinstance(ctx.multi_timeframe_context, dict) else {}
        ltf = mtf.get(900) or mtf.get(300) or mtf.get(60)
        expected = "bullish" if direction == "BUY" else "bearish"
        if ltf:
            aligned = ltf == expected
            suffix = " (aligned)" if aligned else " (not aligned)"
            return QualityGateAnswer(12, QUESTIONS[12], f"LTF {ltf}{suffix}", aligned, "warning")
        return QualityGateAnswer(12, QUESTIONS[12], "no LTF confirmation", False, "warning")

    def _q13(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        if self._lifecycle is None:
            return QualityGateAnswer(13, QUESTIONS[13], "N/A — no lifecycle", True, "warning")
        try:
            active = self._lifecycle.get_active(symbol=ctx.symbol if ctx else "")
        except Exception:  # noqa: BLE001
            active = []
        opposite = "SELL" if direction == "BUY" else "BUY"
        conflicts = [s for s in active if s.direction == opposite]
        if conflicts:
            c = conflicts[0]
            return QualityGateAnswer(
                13, QUESTIONS[13],
                f"conflict: {opposite} active ({c.strategy})", False, "warning"
            )
        return QualityGateAnswer(13, QUESTIONS[13], "no conflicting active signals", True, "warning")

    def _q14(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        try:
            report = self._contradiction.analyze(
                symbol=ctx.symbol if ctx else "", direction=direction,
                evidence=evidence, market_state=mstate or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[QUALITY] contradiction.analyze failed: %s", exc)
            return QualityGateAnswer(14, QUESTIONS[14], "UNKNOWN — contradiction engine error", False, "critical")
        rec = getattr(report, "recommendation", "WAIT")
        sev = getattr(report, "severity", "NONE")
        reasons = getattr(report, "reasons", []) or []
        if rec == "INVALIDATE":
            detail = f"INVALIDATE ({sev}): {'; '.join(reasons) or 'contradiction'}"
            return QualityGateAnswer(14, QUESTIONS[14], detail, False, "critical")
        if rec == "WAIT":
            return QualityGateAnswer(14, QUESTIONS[14], f"WAIT ({sev})", True, "warning")
        net = getattr(report, "net_confidence", 0.0)
        return QualityGateAnswer(14, QUESTIONS[14], f"TRADE ({sev}, net={net:.0f})", True, "warning")

    def _q15(self, ctx, direction, evidence, mstate) -> QualityGateAnswer:
        inv = None
        if isinstance(mstate, dict):
            inv = mstate.get("invalidation")
        if not inv and ctx is not None:
            inv = ctx.signal_invalidation
        if inv:
            return QualityGateAnswer(15, QUESTIONS[15], f"invalidation: {inv}", True, "critical")
        return QualityGateAnswer(15, QUESTIONS[15], "no invalidation defined", False, "critical")

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _market_state_from_context(
        ctx: MarketContextState, direction: str, evidence: dict
    ) -> dict:
        """Build a minimal contradiction-engine vocabulary from context state."""
        mtf = ctx.multi_timeframe_context if isinstance(ctx.multi_timeframe_context, dict) else {}
        htf_tf = 14400 if 14400 in mtf else (86400 if 86400 in mtf else None)
        ltf_tf = 900 if 900 in mtf else (300 if 300 in mtf else (60 if 60 in mtf else None))
        fvg_dir = "BULLISH" if direction == "BUY" else "BEARISH"
        ob_dir = "BULLISH" if direction == "BUY" else "BEARISH"
        out: dict = {
            "htf_bias": mtf.get(htf_tf, "") if htf_tf else "",
            "ltf_bias": mtf.get(ltf_tf, "") if ltf_tf else "",
            "spread_regime": ctx.spread_regime,
            "has_displacement": "displacement" in evidence,
            "has_fvg": False,
            "fvg_status": "",
            "has_ob": False,
            "ob_status": "",
            "session": ctx.session,
            "volatility_regime": ctx.volatility_regime,
            "has_liquidity_sweep": bool(ctx.liquidity_sweeps),
        }
        for fvg in ctx.fvgs or []:
            if str(fvg.get("direction", "")).upper() == fvg_dir:
                out["has_fvg"] = True
                out["fvg_status"] = fvg.get("status", "")
                break
        for ob in ctx.order_blocks or []:
            if str(ob.get("direction", "")).upper() == ob_dir:
                out["has_ob"] = True
                out["ob_status"] = ob.get("state", "")
                break
        return out
