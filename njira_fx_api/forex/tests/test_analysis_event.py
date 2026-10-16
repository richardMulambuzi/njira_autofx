"""
Tests for _build_analysis_event §45 observability extension.

Verifies the live "analysis" broadcast payload now carries:
  - current_price / day_high / day_low / data_freshness (from MarketContextState)
  - strategy_reports (StrategyReport.to_dict() list from SignalEngine)
  - strategy_agreement (counts only active+applicable reports)
  - invalidation (first non-empty invalidations list across reports)

All enrichment is best-effort: a missing context engine or a raising signal
engine must never break the payload.
"""
from unittest import mock

from django.test import TestCase

from forex.analysis.context import MarketContextState
from forex.analysis.strategy import StrategyReport
from forex.pepperstone.ticks_engine import TicksEngine


class AnalysisEventObservabilityTest(TestCase):
    def setUp(self):
        self.engine = TicksEngine(symbols=["EURUSD"])

    def _set_context(self, **kwargs) -> MarketContextState:
        st = MarketContextState(symbol="EURUSD")
        for key, val in kwargs.items():
            setattr(st, key, val)
        ctx_engine = mock.MagicMock()
        ctx_engine.get.return_value = st
        self.engine._context_engine = ctx_engine
        return st

    def _set_signal_reports(self, reports) -> mock.MagicMock:
        sig = mock.MagicMock()
        sig.get_strategy_reports.return_value = reports
        self.engine._signal_engine = sig
        return sig

    def test_observability_fields_present(self):
        self._set_context(
            current_price=1.08432,
            daily_high=1.0851,
            daily_low=1.0832,
            data_freshness=42.0,
        )
        bull = StrategyReport(
            name="malysian",
            active=True,
            applicable=True,
            direction="BULLISH",
            confidence=62.5,
            invalidations=[],
        )
        neutral = StrategyReport(
            name="Moving_averages",
            active=False,  # inactive → excluded from agreement
            applicable=True,
            direction="NEUTRAL",
            invalidations=["stale context"],
        )
        self._set_signal_reports([bull, neutral])

        payload = self.engine._build_analysis_event("EURUSD")
        self.assertIsNotNone(payload)

        # Context-derived fields.
        self.assertEqual(payload["current_price"], 1.08432)
        self.assertEqual(payload["day_high"], 1.0851)
        self.assertEqual(payload["day_low"], 1.0832)
        self.assertEqual(payload["data_freshness"], 42.0)

        # Agreement counts only active+applicable reports.
        self.assertEqual(
            payload["strategy_agreement"],
            {"bullish": 1, "bearish": 0, "neutral": 0, "total": 1},
        )

        # Reports are serialized dicts (to_dict()).
        self.assertEqual(len(payload["strategy_reports"]), 2)
        self.assertEqual(payload["strategy_reports"][0]["name"], "malysian")
        self.assertTrue(payload["strategy_reports"][0]["active"])
        self.assertEqual(payload["strategy_reports"][1]["direction"], "NEUTRAL")

        # First non-empty invalidations list across reports.
        self.assertEqual(payload["invalidation"], ["stale context"])

    def test_no_context_engine(self):
        self.engine._context_engine = None
        self._set_signal_reports([])

        payload = self.engine._build_analysis_event("EURUSD")
        self.assertIsNotNone(payload)

        self.assertIsNone(payload["data_freshness"])
        self.assertIsNone(payload["day_high"])
        self.assertIsNone(payload["day_low"])
        # current_price falls back to last_close (None — no candles in test).
        self.assertIsNone(payload["current_price"])
        self.assertEqual(payload["strategy_reports"], [])
        self.assertEqual(
            payload["strategy_agreement"],
            {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0},
        )
        self.assertEqual(payload["invalidation"], [])

    def test_signal_engine_failure_yields_empty_reports(self):
        self._set_context(current_price=1.08, daily_high=1.09, daily_low=1.07, data_freshness=10.0)
        sig = mock.MagicMock()
        sig.get_strategy_reports.side_effect = RuntimeError("boom")
        self.engine._signal_engine = sig

        payload = self.engine._build_analysis_event("EURUSD")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["strategy_reports"], [])
        self.assertEqual(
            payload["strategy_agreement"],
            {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0},
        )
        self.assertEqual(payload["invalidation"], [])
        # Context fields still present despite signal engine failure.
        self.assertEqual(payload["current_price"], 1.08)
        self.assertEqual(payload["data_freshness"], 10.0)
