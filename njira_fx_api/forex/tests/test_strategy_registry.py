"""
Strategy registry + interface tests (improve.md §20 — Phase 7).

Run: python manage.py test forex.tests.test_strategy_registry
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.strategy import (
    MalysianStrategy,
    MovingAveragesStrategy,
    StrategyRegistry,
    StrategyReport,
)


def _make_engine() -> SignalEngine:
    """SignalEngine with no real sub-engines; market state is neutral."""
    eng = SignalEngine(cache=HotCache())
    eng._build_market_state = lambda symbol, direction, evidence: {}
    return eng


# ── (a) Registry registers both strategies; get/all/names work ────────────
class RegistryTests(SimpleTestCase):
    def test_both_strategies_registered(self):
        names = StrategyRegistry.names()
        self.assertIn("malysian", names)
        self.assertIn("Moving_averages", names)
        self.assertEqual(len(StrategyRegistry.all()), 10)

    def test_get_returns_class(self):
        self.assertIs(StrategyRegistry.get("malysian"), MalysianStrategy)
        self.assertIs(StrategyRegistry.get("Moving_averages"), MovingAveragesStrategy)

    def test_unknown_name_returns_none(self):
        self.assertIsNone(StrategyRegistry.get("does_not_exist"))


# ── (b) evaluate("both") runs both strategy classes (mocked) ──────────────
class EvaluateBothRunsBothTests(SimpleTestCase):
    def test_both_modes_invoke_strategy_evaluate(self):
        eng = _make_engine()
        # _evaluate_mtf is stubbed because evaluate() also runs the legacy
        # signal builders and the MTF one calls get_clickhouse_client()
        # directly — this test asserts strategy invocation, not signal output.
        with patch.object(MalysianStrategy, "evaluate") as mal, \
             patch.object(MovingAveragesStrategy, "evaluate") as mtf, \
             patch.object(SignalEngine, "_evaluate_mtf", return_value=[]):
            # Non-actionable reports so no signal creation is attempted.
            mal.return_value = StrategyReport(
                name="malysian", active=True, applicable=False,
                direction="NEUTRAL", reason="test",
            )
            mtf.return_value = StrategyReport(
                name="Moving_averages", active=True, applicable=False,
                direction="NEUTRAL", reason="test",
            )
            eng.evaluate("EURUSD", "both")
            mal.assert_called_once()
            mtf.assert_called_once()


# ── (c) Unknown mode → no crash, returns [] ───────────────────────────────
class UnknownModeTests(SimpleTestCase):
    def test_unknown_mode_returns_empty_and_skips(self):
        eng = _make_engine()
        with patch.object(MalysianStrategy, "evaluate") as mal, \
             patch.object(MovingAveragesStrategy, "evaluate") as mtf:
            signals = eng.evaluate("EURUSD", "bogus_mode")
            self.assertEqual(signals, [])
            mal.assert_not_called()
            mtf.assert_not_called()


# ── (d) get_strategy_reports returns fully-populated §20 reports ──────────
class StrategyReportsTests(SimpleTestCase):
    def test_reports_populated_all_fields(self):
        eng = _make_engine()
        mal_report = StrategyReport(
            name="malysian", active=True, applicable=True, direction="BULLISH",
            preconditions=["p1"], confirmations=["c1"], missing_confirmations=["m1"],
            invalidations=["i1"], confidence=72.0,
            candidate_setup={"entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12},
            reason="bullish confluence",
        )
        mtf_report = StrategyReport(
            name="Moving_averages", active=True, applicable=True, direction="BEARISH",
            preconditions=["p2"], confirmations=["c2"], missing_confirmations=["m2"],
            invalidations=[], confidence=64.0,
            candidate_setup={"entry": 1.1, "stop_loss": 1.11, "take_profit": 1.09},
            reason="bearish alignment",
        )
        with patch.object(MalysianStrategy, "evaluate", return_value=mal_report), \
             patch.object(MovingAveragesStrategy, "evaluate", return_value=mtf_report):
            reports = eng.get_strategy_reports("EURUSD")

        self.assertEqual(len(reports), 10)
        for r in reports:
            # This test validates the two original strategies it mocks; the 8
            # new ICT/SMC strategies run for real (inactive, no context) and are
            # covered by test_strategy_expansion.
            if r.name not in ("malysian", "Moving_averages"):
                continue
            d = r.to_dict()
            self.assertIn(d["name"], ("malysian", "Moving_averages"))
            self.assertTrue(d["active"])
            self.assertTrue(d["applicable"])
            self.assertIn(d["direction"], ("BULLISH", "BEARISH", "NEUTRAL"))
            self.assertTrue(d["preconditions"])
            self.assertTrue(d["confirmations"])
            self.assertIsInstance(d["missing_confirmations"], list)
            self.assertIsInstance(d["invalidations"], list)
            self.assertGreaterEqual(d["confidence"], 0.0)
            self.assertLessEqual(d["confidence"], 100.0)
            self.assertIsNotNone(d["candidate_setup"])
            self.assertTrue(d["reason"])
