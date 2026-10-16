"""
Retest Engine tests (improve.md §34–§35 — audit sequencing item 7).

Run:
    myvenv/bin/python manage.py test forex.tests.test_retest

Also keep green:
    forex.pepperstone  (58)
    forex.test_mtf_strategy  (17)
    forex.tests.test_signal_dedup  (6)
    forex.tests.test_event_driven  (4)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.order_blocks import OBDirection
from forex.analysis.retest import RetestEngine, RetestEvent
from forex.analysis.signal_engine import SignalEngine, SignalDraft


def _candle(o, h, l, c, ts):
    return {"open": o, "high": h, "low": l, "close": c, "open_time": ts}


class RetestEngineTests(SimpleTestCase):
    def setUp(self):
        self.engine = RetestEngine()

    def test_a_bullish_ob_rejected(self):
        """Price dips into a bullish OB then closes back above → REJECTED."""
        zone = {"zone_top": 1.1000, "zone_bottom": 1.0950}
        candles = [
            _candle(1.1020, 1.1025, 1.1015, 1.1020, 1),
            _candle(1.1020, 1.1022, 1.1018, 1.1010, 2),
            _candle(1.1010, 1.1010, 1.0960, 1.0970, 3),  # dip into zone
            _candle(1.0970, 1.1030, 1.0965, 1.1025, 4),  # close back above → reject
        ]
        ev = self.engine.evaluate("EURUSD", "order_block", zone, "BULLISH", candles)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.reaction, "REJECTED")
        self.assertEqual(ev.direction, "BULLISH")
        self.assertGreater(ev.reaction_strength, 0.0)
        self.assertEqual(ev.zone_type, "order_block")

    def test_b_bearish_fvg_rejected(self):
        """Price rises into a bearish FVG then closes back below → REJECTED."""
        zone = {"zone_top": 1.1050, "zone_bottom": 1.1000}
        candles = [
            _candle(1.0980, 1.0985, 1.0975, 1.0980, 1),
            _candle(1.0990, 1.0995, 1.0985, 1.0990, 2),
            _candle(1.0990, 1.1040, 1.0995, 1.1005, 3),  # rise into zone
            _candle(1.1005, 1.1005, 1.0970, 1.0980, 4),  # close back below → reject
        ]
        ev = self.engine.evaluate("EURUSD", "fvg", zone, "BEARISH", candles)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.reaction, "REJECTED")
        self.assertEqual(ev.direction, "BEARISH")
        self.assertEqual(ev.zone_type, "fvg")

    def test_c_penetration_beyond_tolerance_failed(self):
        """Price penetrates the zone beyond tolerance and closes beyond → FAILED."""
        zone = {"zone_top": 1.1000, "zone_bottom": 1.0950}  # tol = 0.5 * 0.005 = 0.0025
        candles = [
            _candle(1.1020, 1.1025, 1.1015, 1.1020, 1),
            _candle(1.1010, 1.1015, 1.1005, 1.1010, 2),
            _candle(1.1010, 1.1010, 1.0900, 1.0910, 3),  # deep penetration
            _candle(1.0910, 1.0910, 1.0900, 1.0905, 4),  # continues below
        ]
        ev = self.engine.evaluate("EURUSD", "order_block", zone, "BULLISH", candles)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.reaction, "FAILED")
        self.assertEqual(ev.invalidation_reason, "penetrated beyond tolerance — zone broken")

    def test_d_no_touch_returns_none(self):
        """No candle overlaps the zone within the window → None."""
        zone = {"zone_top": 1.1000, "zone_bottom": 1.0950}
        candles = [
            _candle(1.1100, 1.1110, 1.1090, 1.1100, 1),
            _candle(1.1100, 1.1110, 1.1090, 1.1105, 2),
            _candle(1.1105, 1.1115, 1.1095, 1.1100, 3),
        ]
        ev = self.engine.evaluate("EURUSD", "order_block", zone, "BULLISH", candles)
        self.assertIsNone(ev)

    def test_e_signal_engine_adds_ob_retest_evidence(self):
        """SignalEngine with a retest engine folds ob_retest evidence into a draft."""
        cache = HotCache()
        # Provide candles for the OB timeframe so the retest loop runs.
        cache.add({
            "symbol": "EURUSD", "timeframe": 3600,
            "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.10, "open_time": 1,
        })

        fake_ob = SimpleNamespace(direction=OBDirection.BULLISH, high=1.1000, low=1.0950)
        fake_ob_engine = MagicMock()
        fake_ob_engine.get_active.return_value = [fake_ob]

        # Mock the retest engine: always reports a REJECTED bullish retest.
        fake_retest = MagicMock()
        fake_retest.evaluate.return_value = RetestEvent(
            zone_type="order_block", direction="BULLISH",
            zone_top=1.1000, zone_bottom=1.0950, approach="from_above",
            reaction="REJECTED", reaction_strength=0.8,
        )
        fake_retest.get_recent.return_value = []

        engine = SignalEngine(
            cache=cache,
            ob_engines={3600: fake_ob_engine},
            retest_engine=fake_retest,
        )

        draft = SignalDraft(direction="BUY", entry=1.0975, stop_loss=1.0950, take_profit=1.1040)
        draft.confidence = 70.0
        draft.evidence = {}
        draft.explanation_parts = []
        draft.risk_reward = 2.5

        engine._apply_retest_evidence("EURUSD", draft)

        self.assertIn("ob_retest", draft.evidence)
        self.assertEqual(len(draft.evidence["ob_retest"]), 1)
        self.assertEqual(draft.evidence["ob_retest"][0]["reaction"], "REJECTED")
        # Fixed +5 confidence bump for a REJECTED retest in the trade direction.
        self.assertAlmostEqual(draft.confidence, 75.0, places=1)
