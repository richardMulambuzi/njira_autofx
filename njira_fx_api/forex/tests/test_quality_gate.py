"""
Signal Quality Gate tests (improve.md §37–§38 — audit sequencing item 8).

Run:
    myvenv/bin/python manage.py test forex.tests.test_quality_gate
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.context import MarketContextState
from forex.analysis.quality_gate import QualityGate, QualityGateReport
from forex.analysis.retest import RetestEvent
from forex.analysis.signal_engine import SignalEngine, SignalDraft
from forex.analysis.signal_lifecycle import SignalState


# ── Fakes ──────────────────────────────────────────────────────────────────
class FakeReport:
    """Minimal stand-in for ContradictionReport."""

    def __init__(self, recommendation: str, severity: str = "NONE", net_confidence: float = 80.0):
        self.recommendation = recommendation
        self.severity = severity
        self.net_confidence = net_confidence
        self.reasons: list[str] = []

    def to_dict(self) -> dict:
        return {
            "recommendation": self.recommendation,
            "severity": self.severity,
            "net_confidence": self.net_confidence,
            "reasons": self.reasons,
        }


class FakeContradiction:
    def __init__(self, recommendation: str):
        self._rec = recommendation

    def analyze(self, symbol, direction, evidence, market_state):
        return FakeReport(self._rec)


class FakeContext:
    def __init__(self, state: MarketContextState):
        self._state = state

    def get(self, symbol):
        return self._state


class FakeRetest:
    def __init__(self, events):
        self._events = events

    def get_recent(self, symbol, limit=10):
        return self._events


class FakeLifecycle:
    def __init__(self, active=None):
        self._active = active or []

    def get_active(self, symbol=None):
        return self._active


# ── Builders ────────────────────────────────────────────────────────────────
def _full_state() -> MarketContextState:
    """A MarketContextState where every quality-gate question can be answered."""
    s = MarketContextState(symbol="EURUSD")
    s.current_price = 1.1000
    s.data_freshness = 0.0
    s.daily_high = 1.1050
    s.daily_low = 1.0950
    s.session = "LONDON"
    s.market_structure = "bullish"
    s.structure_state = "bullish"
    s.spread_regime = "NORMAL"
    s.volatility_regime = "NORMAL"
    s.multi_timeframe_context = {
        60: "bullish", 300: "bullish", 900: "bullish",
        3600: "bullish", 14400: "bullish", 86400: "bullish",
    }
    s.major_order_blocks = [{"direction": "BULLISH", "price": 1.0980, "state": "VALID"}]
    s.order_blocks = [{"direction": "BULLISH", "price": 1.0980, "state": "VALID"}]
    s.fvgs = [{"direction": "BULLISH", "status": "OPEN"}]
    s.imbalances = [{"direction": "BULLISH"}]
    s.liquidity_levels = [{"pool_type": "BUY", "price": 1.0950, "state": "ACTIVE"}]
    s.liquidity_sweeps = [{"pool_type": "SELL", "price": 1.0950, "state": "SWEPT"}]
    s.poc = 1.0990
    s.vah = 1.1020
    s.val = 1.0970
    s.signal_invalidation = "price closes below 1.0970"
    return s


# 2026-01-01 13:00 UTC — inside london/newyork, so EURUSD clears the session
# gate.  The gate fails closed on a missing entry_time, so a synthetic draft
# without one would be dropped before it ever reached the quality gate.
ENTRY_TIME = 1767272400.0


def _make_draft(direction: str) -> SignalDraft:
    d = SignalDraft(direction=direction, entry=1.1000, stop_loss=1.0970, take_profit=1.1060,
                    entry_time=ENTRY_TIME)
    d.confidence = 80.0
    d.evidence = {"analyzer": "snr", "htf_bias": 18.0}
    d.explanation_parts = ["test setup"]
    d.risk_reward = 2.5
    d.entry_zone_low = 1.0980
    d.entry_zone_high = 1.1020
    return d


def _make_engine(quality_gate: QualityGate) -> SignalEngine:
    eng = SignalEngine(cache=HotCache(), quality_gate=quality_gate)
    # Neutral market state that still carries an invalidation string (Q15).
    eng._build_market_state = lambda symbol, direction, evidence: {
        "invalidation": "price beyond 1.0970",
        "htf_bias": "bullish", "ltf_bias": "bullish",
        "spread_regime": "NORMAL", "volatility_regime": "NORMAL",
        "session": "LONDON", "has_liquidity_sweep": True,
    }
    eng._contradiction.analyze = lambda *a, **k: FakeReport("TRADE")
    return eng


# ── (a) All 15 questions answered ───────────────────────────────────────────
class QualityGateAnswersAllQuestionsTests(SimpleTestCase):
    def test_all_fifteen_questions_answered_and_pass(self):
        ctx = FakeContext(_full_state())
        contra = FakeContradiction("TRADE")
        retest = FakeRetest([RetestEvent(
            zone_type="order_block", direction="BULLISH", approach="from_below",
            zone_top=1.0990, zone_bottom=1.0970, reaction="REJECTED",
        )])
        lifecycle = FakeLifecycle([])
        qg = QualityGate(
            context_engine=ctx, contradiction_engine=contra,
            retest_engine=retest, lifecycle=lifecycle,
        )

        report = qg.evaluate("EURUSD", "BUY", {"htf_bias": 18.0})

        self.assertIsInstance(report, QualityGateReport)
        self.assertEqual(len(report.answers), 15)
        self.assertEqual([a.question_id for a in report.answers], list(range(1, 16)))
        self.assertTrue(report.passed)
        self.assertEqual(report.critical_failures, [])
        # Every question answered with a concrete (non-UNKNOWN) answer.
        for a in report.answers:
            self.assertNotEqual(a.answer, "UNKNOWN")
            self.assertTrue(a.passed)
        # Serializes cleanly.
        d = report.to_dict()
        self.assertEqual(len(d["answers"]), 15)
        self.assertTrue(d["passed"])


# ── (b) Critical failure (contradiction INVALIDATE) → WAITING ───────────────
class QualityGateCriticalFailureTests(SimpleTestCase):
    def test_invalidating_contradiction_holds_signal_waiting(self):
        ctx = FakeContext(_full_state())
        # Q14 contradiction engine says INVALIDATE → critical failure.
        qg = QualityGate(
            context_engine=ctx, contradiction_engine=FakeContradiction("INVALIDATE"),
            retest_engine=FakeRetest([]), lifecycle=FakeLifecycle([]),
        )
        eng = _make_engine(qg)
        eng._build_snr_draft = (
            lambda symbol: _make_draft("BUY")
        )

        signals = eng.evaluate("EURUSD", "malysian")

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig.state, SignalState.WAITING)
        self.assertIn("quality_gate", sig.confidence_evidence)
        self.assertFalse(sig.confidence_evidence["quality_gate"]["passed"])
        self.assertTrue(sig.confidence_evidence["quality_gate"]["critical_failures"])


# ── (c) Data quality: stale data → NO SIGNAL ────────────────────────────────
class DataQualityGateTests(SimpleTestCase):
    def test_stale_data_returns_no_signal(self):
        stale = _full_state()
        stale.data_freshness = 999.0  # > 5 minutes
        qg = QualityGate(
            context_engine=FakeContext(stale), contradiction_engine=FakeContradiction("TRADE"),
            retest_engine=FakeRetest([]), lifecycle=FakeLifecycle([]),
        )
        eng = _make_engine(qg)
        eng._build_snr_draft = (
            lambda symbol: _make_draft("BUY")
        )

        signals = eng.evaluate("EURUSD", "malysian")
        self.assertEqual(signals, [])

    def test_missing_timeframes_returns_no_signal(self):
        bad = _full_state()
        bad.multi_timeframe_context = {60: "bullish"}  # missing 300/900/3600
        qg = QualityGate(
            context_engine=FakeContext(bad), contradiction_engine=FakeContradiction("TRADE"),
            retest_engine=FakeRetest([]), lifecycle=FakeLifecycle([]),
        )
        ok, issues = qg.check_data_quality("EURUSD", None)
        self.assertFalse(ok)
        self.assertTrue(any("timeframes" in i for i in issues))


# ── (d) Passed gate → signal proceeds to CONFIRMED ──────────────────────────
class QualityGatePassedTests(SimpleTestCase):
    def test_passed_gate_proceeds_to_confirmed(self):
        ctx = FakeContext(_full_state())
        qg = QualityGate(
            context_engine=ctx, contradiction_engine=FakeContradiction("TRADE"),
            retest_engine=FakeRetest([]), lifecycle=FakeLifecycle([]),
        )
        eng = _make_engine(qg)
        eng._build_snr_draft = (
            lambda symbol: _make_draft("BUY")
        )

        signals = eng.evaluate("EURUSD", "malysian")

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig.state, SignalState.CONFIRMED)
        self.assertIn("quality_gate", sig.confidence_evidence)
        self.assertTrue(sig.confidence_evidence["quality_gate"]["passed"])
