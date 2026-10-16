"""
Signal Polisher unit tests.

Run: python manage.py test forex.tests.test_polisher
"""
from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from forex.analysis.polisher import PolishResult, SignalPolisher


class SignalPolisherTests(SimpleTestCase):
    """Test the SignalPolisher filtering pipeline."""

    def _make_polisher(
        self,
        min_confidence: float = 60.0,
        min_rr: float = 1.5,
        noise_floor: float = 20.0,
        quality_gate: MagicMock | None = None,
        contradiction: MagicMock | None = None,
    ) -> SignalPolisher:
        return SignalPolisher(
            min_confidence=min_confidence,
            min_rr=min_rr,
            noise_floor=noise_floor,
            quality_gate=quality_gate,
            contradiction=contradiction,
        )

    def _base_args(self) -> dict:
        return {
            "symbol": "EURUSD",
            "direction": "BUY",
            "confidence": 75.0,
            "risk_reward": 2.0,
            "evidence": {"htf_bias": 18.0, "ob_quality": 16.0},
            "market_state": {"htf_bias": "bullish", "ltf_bias": "bullish"},
        }

    # ── PASS when all thresholds met ──────────────────────────────────────
    def test_pass_all_thresholds_met(self):
        polisher = self._make_polisher()
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "PASS")
        self.assertEqual(result.reason_code, "all_filters_passed")
        self.assertEqual(len(result.trace), 5)
        for entry in result.trace:
            self.assertTrue(entry.passed)
            self.assertIn(entry.filter_name, [
                "min_confidence", "min_rr", "noise_floor",
                "quality_gate", "contradiction"
            ])

    # ── DROP on confidence below min_confidence ───────────────────────────
    def test_drop_confidence_below_min(self):
        polisher = self._make_polisher(min_confidence=60.0)
        args = self._base_args()
        args["confidence"] = 50.0  # below min_confidence
        result = polisher.polish(**args, apply_min_confidence=True)

        self.assertEqual(result.action, "DROP")
        self.assertEqual(result.reason_code, "confidence_below_min")
        self.assertEqual(len(result.trace), 1)
        self.assertEqual(result.trace[0].filter_name, "min_confidence")
        self.assertFalse(result.trace[0].passed)
        self.assertEqual(result.trace[0].reason, "confidence_below_min")

    # ── DROP on rr below min_rr ───────────────────────────────────────────
    def test_drop_rr_below_min(self):
        polisher = self._make_polisher(min_rr=1.5)
        args = self._base_args()
        args["risk_reward"] = 1.0  # below min_rr
        result = polisher.polish(**args)

        self.assertEqual(result.action, "DROP")
        self.assertEqual(result.reason_code, "rr_below_min")
        self.assertEqual(len(result.trace), 2)  # min_confidence passed, min_rr failed
        self.assertEqual(result.trace[1].filter_name, "min_rr")
        self.assertFalse(result.trace[1].passed)
        self.assertEqual(result.trace[1].reason, "rr_below_min")

    # ── DROP on noise floor ───────────────────────────────────────────────
    def test_drop_noise_floor(self):
        # noise_floor (20) < min_confidence (60) in defaults, so set min_confidence lower
        polisher = self._make_polisher(min_confidence=10.0, noise_floor=20.0)
        args = self._base_args()
        args["confidence"] = 15.0  # above min_confidence (10) but below noise_floor (20)
        args["risk_reward"] = 2.0
        result = polisher.polish(**args)

        self.assertEqual(result.action, "DROP")
        self.assertEqual(result.reason_code, "noise_floor")
        self.assertEqual(len(result.trace), 3)  # min_confidence, min_rr passed, noise_floor failed
        self.assertEqual(result.trace[2].filter_name, "noise_floor")
        self.assertFalse(result.trace[2].passed)
        self.assertEqual(result.trace[2].reason, "noise_floor")

    # ── HOLD on quality gate failure ──────────────────────────────────────
    def test_hold_quality_gate_failure(self):
        qg_mock = MagicMock()
        qg_report = MagicMock()
        qg_report.passed = False
        qg_report.critical_failures = ["Q1: no structure"]
        qg_mock.evaluate.return_value = qg_report

        polisher = self._make_polisher(quality_gate=qg_mock)
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "HOLD")
        self.assertEqual(result.reason_code, "quality_gate_failed")
        self.assertEqual(len(result.trace), 4)  # first 3 passed, quality_gate failed
        self.assertEqual(result.trace[3].filter_name, "quality_gate")
        self.assertFalse(result.trace[3].passed)
        self.assertEqual(result.trace[3].reason, "quality_gate_failed")
        self.assertIs(result.quality_gate_report, qg_report)

    # ── DROP on contradiction INVALIDATE ──────────────────────────────────
    def test_drop_contradiction_invalidate(self):
        contra_mock = MagicMock()
        contra_report = MagicMock()
        contra_report.recommendation = "INVALIDATE"
        contra_report.net_confidence = 10.0
        contra_report.severity = "SEVERE"
        contra_report.reasons = ["HTF/LTF conflict"]
        contra_mock.analyze.return_value = contra_report

        polisher = self._make_polisher(contradiction=contra_mock)
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "DROP")
        self.assertEqual(result.reason_code, "contradiction_invalidate")
        self.assertEqual(len(result.trace), 5)  # all 5 filters ran, contradiction failed
        self.assertEqual(result.trace[4].filter_name, "contradiction")
        self.assertFalse(result.trace[4].passed)
        self.assertEqual(result.trace[4].reason, "contradiction_invalidate")
        self.assertIs(result.contradiction_report, contra_report)

    # ── HOLD on contradiction WAIT ────────────────────────────────────────
    def test_hold_contradiction_wait(self):
        contra_mock = MagicMock()
        contra_report = MagicMock()
        contra_report.recommendation = "WAIT"
        contra_report.net_confidence = 30.0
        contra_report.severity = "MODERATE"
        contra_report.reasons = ["No liquidity sweep"]
        contra_mock.analyze.return_value = contra_report

        polisher = self._make_polisher(contradiction=contra_mock)
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "HOLD")
        self.assertEqual(result.reason_code, "contradiction_wait")
        self.assertEqual(len(result.trace), 5)
        self.assertEqual(result.trace[4].filter_name, "contradiction")
        self.assertFalse(result.trace[4].passed)
        self.assertEqual(result.trace[4].reason, "contradiction_wait")
        self.assertIs(result.contradiction_report, contra_report)

    # ── Every trace entry has filter_name/passed/reason ───────────────────
    def test_trace_entries_structure(self):
        polisher = self._make_polisher()
        result = polisher.polish(**self._base_args())

        for entry in result.trace:
            self.assertTrue(hasattr(entry, "filter_name"))
            self.assertTrue(hasattr(entry, "passed"))
            self.assertTrue(hasattr(entry, "reason"))
            self.assertIsInstance(entry.filter_name, str)
            self.assertIsInstance(entry.passed, bool)
            self.assertIsInstance(entry.reason, str)
            self.assertTrue(len(entry.filter_name) > 0)
            self.assertTrue(len(entry.reason) > 0)

    # ── Quality gate passed attaches report ───────────────────────────────
    def test_quality_gate_passed_attaches_report(self):
        qg_mock = MagicMock()
        qg_report = MagicMock()
        qg_report.passed = True
        qg_mock.evaluate.return_value = qg_report

        polisher = self._make_polisher(quality_gate=qg_mock)
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "PASS")
        self.assertIs(result.quality_gate_report, qg_report)
        # quality_gate trace entry should show passed
        qg_entry = next(e for e in result.trace if e.filter_name == "quality_gate")
        self.assertTrue(qg_entry.passed)
        self.assertEqual(qg_entry.reason, "quality_gate_passed")

    # ── Contradiction TRADE attaches report ───────────────────────────────
    def test_contradiction_trade_attaches_report(self):
        contra_mock = MagicMock()
        contra_report = MagicMock()
        contra_report.recommendation = "TRADE"
        contra_report.net_confidence = 65.0
        contra_report.severity = "NONE"
        contra_report.reasons = []
        contra_mock.analyze.return_value = contra_report

        polisher = self._make_polisher(contradiction=contra_mock)
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "PASS")
        self.assertIs(result.contradiction_report, contra_report)
        # contradiction trace entry should show passed
        contra_entry = next(e for e in result.trace if e.filter_name == "contradiction")
        self.assertTrue(contra_entry.passed)
        self.assertIn("contradiction_trade", contra_entry.reason)

    # ── No quality gate configured ────────────────────────────────────────
    def test_no_quality_gate_configured(self):
        polisher = self._make_polisher(quality_gate=None)
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "PASS")
        qg_entry = next(e for e in result.trace if e.filter_name == "quality_gate")
        self.assertTrue(qg_entry.passed)
        self.assertEqual(qg_entry.reason, "no_quality_gate_configured")

    # ── No contradiction engine configured ────────────────────────────────
    def test_no_contradiction_engine_configured(self):
        polisher = self._make_polisher(contradiction=None)
        result = polisher.polish(**self._base_args())

        self.assertEqual(result.action, "PASS")
        contra_entry = next(e for e in result.trace if e.filter_name == "contradiction")
        self.assertTrue(contra_entry.passed)
        self.assertEqual(contra_entry.reason, "no_contradiction_engine_configured")