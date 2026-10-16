"""
Signal dedup / setup identity + WAITING re-evaluation tests (improve.md §29, §32, §33).

Run: python manage.py test forex.tests.test_signal_dedup
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine, SignalDraft
from forex.analysis.signal_lifecycle import SignalLifecycleManager, SignalState, Signal


# 2026-01-01 13:00 UTC — inside london/newyork, so EURUSD clears the session
# gate.  The gate fails closed on a missing entry_time, so a synthetic draft
# without one would be dropped before it ever reached the lifecycle manager.
ENTRY_TIME = 1767272400.0


# ── Helpers ────────────────────────────────────────────────────────────────
def _make_draft(direction: str) -> SignalDraft:
    """A strong BUY/SELL draft that clears min confidence + R:R."""
    d = SignalDraft(
        direction=direction,
        entry=1.1000,
        stop_loss=1.0970,
        take_profit=1.1060,
        entry_time=ENTRY_TIME,
    )
    d.confidence = 80.0
    d.evidence = {"analyzer": "snr", "htf_bias": 18.0}
    d.explanation_parts = ["test setup"]
    d.risk_reward = 2.5
    d.entry_zone_low = 1.0980
    d.entry_zone_high = 1.1020
    return d


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


def _make_engine() -> SignalEngine:
    """SignalEngine with no real sub-engines; market state is neutral."""
    eng = SignalEngine(cache=HotCache())
    # Neutral market state — the draft is supplied by the test.
    eng._build_market_state = lambda symbol, direction, evidence: {}
    return eng


# ── (a) Dedup: same setup evaluated twice → only ONE active signal ──────────
class DedupSameSetupTests(SimpleTestCase):
    def test_evaluate_twice_same_setup_single_active(self):
        eng = _make_engine()
        # SNR path: single-arg _build_snr_draft; the draft carries its own direction.
        eng._build_snr_draft = lambda symbol: _make_draft("BUY")
        eng._contradiction.analyze = lambda *a, **k: FakeReport("TRADE")

        first = eng.evaluate("EURUSD", "malysian")
        second = eng.evaluate("EURUSD", "malysian")

        active = eng.get_active_signals("EURUSD")
        self.assertEqual(len(active), 1, f"expected 1 active signal, got {len(active)}")
        # Second call must have UPDATED the existing signal, not created a new one.
        self.assertEqual(active[0].signal_id, first[0].signal_id)
        self.assertEqual(active[0].signal_id, second[0].signal_id)


# ── (b)/(c) WAITING re-evaluation ───────────────────────────────────────────
class WaitingReevaluationTests(SimpleTestCase):
    # The SNR family now treats the ICT contradiction engine as ADVISORY, so it
    # no longer creates WAITING signals itself.  These tests exercise the
    # WAITING re-evaluation feature (improve.md §33) directly by seeding a
    # WAITING signal in the lifecycle and letting evaluate() re-check it with
    # fresh market state (the contradiction report drives the transition).

    @staticmethod
    def _seed_waiting(eng, direction="BUY") -> Signal:
        sig = eng.get_lifecycle().create(
            symbol="EURUSD", direction=direction, entry=1.1000, stop_loss=1.0970,
            take_profit=1.1060, confidence=80.0, strategy="malysian",
        )
        eng.get_lifecycle().transition(sig, SignalState.WAITING, reason="test hold")
        return sig

    def test_waiting_reevaluates_to_confirmed(self):
        eng = _make_engine()
        sig = self._seed_waiting(eng)
        eng._contradiction.analyze = lambda *a, **k: FakeReport("TRADE")

        eng.evaluate("EURUSD", "malysian")  # re-eval → TRADE → CONFIRMED
        active = eng.get_active_signals("EURUSD")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].signal_id, sig.signal_id)
        self.assertEqual(active[0].state, SignalState.CONFIRMED)

    def test_waiting_reevaluates_to_invalidated(self):
        eng = _make_engine()
        sig = self._seed_waiting(eng)
        eng._contradiction.analyze = lambda *a, **k: FakeReport("INVALIDATE")

        eng.evaluate("EURUSD", "malysian")  # re-eval → INVALIDATE
        orig = eng.get_lifecycle().get_by_id(sig.signal_id)
        self.assertEqual(orig.state, SignalState.INVALIDATED)


# ── (d) find_active_setup returns None after terminal state ────────────────
class LifecycleSetupIdentityTests(SimpleTestCase):
    def test_find_active_setup_none_after_invalidated(self):
        mgr = SignalLifecycleManager()
        sig = mgr.create(symbol="EURUSD", direction="BUY", strategy="malysian", confidence=80)
        mgr.transition(sig, SignalState.WAITING, reason="x")
        self.assertIs(mgr.find_active_setup("EURUSD", "BUY", "malysian"), sig)

        mgr.transition(sig, SignalState.INVALIDATED, reason="x")
        self.assertIsNone(mgr.find_active_setup("EURUSD", "BUY", "malysian"))

    def test_setup_key_normalization(self):
        self.assertEqual(
            SignalLifecycleManager.setup_key("EURUSD", "BUY", "Malysian"),
            "EURUSD:BUY:malysian",
        )
        self.assertEqual(
            SignalLifecycleManager.setup_key("EURUSD", "BUY", ""),
            "EURUSD:BUY:unknown",
        )

    def test_different_strategy_is_different_setup(self):
        mgr = SignalLifecycleManager()
        s1 = mgr.create(symbol="EURUSD", direction="BUY", strategy="malysian")
        s2 = mgr.create(symbol="EURUSD", direction="BUY", strategy="moving_averages")
        self.assertIs(mgr.find_active_setup("EURUSD", "BUY", "malysian"), s1)
        self.assertIs(mgr.find_active_setup("EURUSD", "BUY", "moving_averages"), s2)
