"""
Strategy-registry expansion tests (improve.md §20 — Phase 7).

Adds 4 first-class ICT/SMC strategies (order_block, fvg, liquidity, structure)
plus wires the legacy Malaysian module into the registry, and verifies the
backtester-facing status reports.

Run: python manage.py test forex.tests.test_strategy_expansion
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.context import MarketContextState
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.strategy import (
    FVGStrategy,
    LiquidityStrategy,
    MalysianStrategy,
    MovingAveragesStrategy,
    OrderBlockStrategy,
    StrategyRegistry,
    StructureStrategy,
    StrategyReport,
)


def _ctx(**kwargs) -> MarketContextState:
    """Build a MarketContextState with overrides (safe defaults otherwise)."""
    return MarketContextState(symbol="EURUSD", **kwargs)


# ── (a) Registry now has 6 strategies ───────────────────────────────────────
class RegistryExpansionTests(SimpleTestCase):
    def test_six_strategies_registered(self):
        names = StrategyRegistry.names()
        self.assertEqual(len(names), 6)
        for expected in ("malysian", "Moving_averages", "order_block", "fvg", "liquidity", "structure"):
            self.assertIn(expected, names)

    def test_all_strategy_classes_resolvable(self):
        for name in StrategyRegistry.names():
            self.assertIsNotNone(StrategyRegistry.get(name))


# ── (b) OrderBlockStrategy ─────────────────────────────────────────────────
class OrderBlockStrategyTests(SimpleTestCase):
    def test_bullish_active_ob_reports_buy(self):
        ctx = _ctx(order_blocks=[{
            "direction": "BULLISH_OB", "state": "VALID",
            "high": 1.10, "low": 1.09, "quality_score": 82.0,
        }])
        rep = OrderBlockStrategy().evaluate("EURUSD", ctx, None)
        self.assertEqual(rep.direction, "BULLISH")
        self.assertTrue(rep.applicable)
        self.assertGreater(rep.confidence, 0.0)

    def test_no_order_blocks_reports_inactive(self):
        rep = OrderBlockStrategy().evaluate("EURUSD", _ctx(), None)
        self.assertEqual(rep.direction, "NEUTRAL")
        self.assertFalse(rep.applicable)
        self.assertTrue(rep.missing_confirmations)

    def test_invalidated_ob_is_invalidation(self):
        ctx = _ctx(order_blocks=[{
            "direction": "BEARISH_OB", "state": "INVALIDATED",
            "high": 1.10, "low": 1.09, "quality_score": 10.0,
        }])
        rep = OrderBlockStrategy().evaluate("EURUSD", ctx, None)
        self.assertTrue(rep.invalidations)


# ── (c) FVGStrategy ────────────────────────────────────────────────────────
class FVGStrategyTests(SimpleTestCase):
    def test_bearish_fvg_reports_sell(self):
        ctx = _ctx(fvgs=[{
            "direction": "BEARISH", "status": "FORMED",
            "lower_boundary": 1.09, "upper_boundary": 1.10,
            "filled_percentage": 0.0, "fully_filled": False,
        }])
        rep = FVGStrategy().evaluate("EURUSD", ctx, None)
        self.assertEqual(rep.direction, "BEARISH")
        self.assertTrue(rep.applicable)

    def test_filled_fvg_is_invalidation(self):
        ctx = _ctx(fvgs=[{
            "direction": "BULLISH", "status": "FILLED",
            "lower_boundary": 1.09, "upper_boundary": 1.10,
            "filled_percentage": 1.0, "fully_filled": True,
        }])
        rep = FVGStrategy().evaluate("EURUSD", ctx, None)
        self.assertTrue(rep.invalidations)


# ── (d) LiquidityStrategy ──────────────────────────────────────────────────
class LiquidityStrategyTests(SimpleTestCase):
    def test_sell_side_sweep_reports_buy(self):
        ctx = _ctx(liquidity_sweeps=[{"pool_type": "SWING_LOW", "price": 1.05, "state": "SWEPT"}])
        rep = LiquidityStrategy().evaluate("EURUSD", ctx, None)
        self.assertEqual(rep.direction, "BULLISH")
        self.assertTrue(rep.applicable)

    def test_buy_side_sweep_reports_sell(self):
        ctx = _ctx(liquidity_sweeps=[{"pool_type": "PDH", "price": 1.20, "state": "SWEPT"}])
        rep = LiquidityStrategy().evaluate("EURUSD", ctx, None)
        self.assertEqual(rep.direction, "BEARISH")


# ── (e) StructureStrategy ──────────────────────────────────────────────────
class StructureStrategyTests(SimpleTestCase):
    def test_bullish_structure_reports_buy(self):
        ctx = _ctx(structure_state="bullish", market_structure="bullish", trend_strength=0.8)
        rep = StructureStrategy().evaluate("EURUSD", ctx, None)
        self.assertEqual(rep.direction, "BULLISH")
        self.assertTrue(rep.applicable)
        self.assertGreater(rep.confidence, 0.0)

    def test_no_structure_reports_inactive(self):
        rep = StructureStrategy().evaluate("EURUSD", _ctx(), None)
        self.assertEqual(rep.direction, "NEUTRAL")
        self.assertFalse(rep.applicable)


# ── (f) MalysianStrategy delegates to the engine's SNR analysis ─────────────
class MalaysianStrategyTests(SimpleTestCase):
    def test_delegates_to_engine_snr(self):
        eng = MagicMock()
        eng._analyze_snr.return_value = StrategyReport(
            name="malysian", active=True, applicable=True, direction="BULLISH",
            confidence=72.0, reason="bullish SNR",
        )
        rep = MalysianStrategy().evaluate("EURUSD", None, eng, for_signals=True)
        eng._analyze_snr.assert_called_once_with("EURUSD", for_signals=True)
        self.assertEqual(rep.direction, "BULLISH")
        self.assertEqual(rep.confidence, 72.0)


# ── (g) get_strategy_reports returns all 6 ─────────────────────────────────
class GetStrategyReportsTests(SimpleTestCase):
    def test_returns_all_six(self):
        eng = SignalEngine(cache=HotCache())
        # Populate a context so strategies report meaningfully (best-effort).
        ctx = _ctx(
            order_blocks=[{"direction": "BULLISH_OB", "state": "VALID", "high": 1.10, "low": 1.09, "quality_score": 70.0}],
            fvgs=[{"direction": "BULLISH", "status": "FORMED", "lower_boundary": 1.09, "upper_boundary": 1.10, "filled_percentage": 0.0, "fully_filled": False}],
            liquidity_sweeps=[{"pool_type": "SWING_LOW", "price": 1.05, "state": "SWEPT"}],
            structure_state="bullish", market_structure="bullish", trend_strength=0.6,
        )
        context_engine = MagicMock()
        context_engine.get.return_value = ctx
        eng._context_engine = context_engine

        reports = eng.get_strategy_reports("EURUSD")

        self.assertEqual(len(reports), 6)
        names = {r.name for r in reports}
        self.assertEqual(names, {"malysian", "Moving_averages", "order_block", "fvg", "liquidity", "structure"})
        # Every report is a fully-shaped §20 report.
        for r in reports:
            self.assertIsInstance(r, StrategyReport)
            self.assertIn(r.direction, ("BULLISH", "BEARISH", "NEUTRAL"))
