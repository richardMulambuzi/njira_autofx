"""
Order Flow Engine tests (ICT/SMC real order flow) + SignalEngine wiring.

Run: python manage.py test forex.tests.test_order_flow
"""
from __future__ import annotations

from datetime import datetime, timezone

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.order_flow import OrderFlowEngine, OrderFlowEvent
from forex.analysis.order_blocks import OBDirection
from forex.analysis.signal_engine import SignalEngine


def _candle(open_, high, low, close, volume=100, tf=60):
    return {
        "symbol": "EURUSD",
        "timeframe": tf,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume_ticks": volume,
        "open_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "close_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


class OrderFlowDeltaTests(SimpleTestCase):
    """(a) delta computation on a known candle."""

    def test_delta_split(self):
        eng = OrderFlowEngine()
        evt = eng.update("EURUSD", _candle(100, 120, 90, 110, volume=100), 60)
        self.assertIsNotNone(evt)
        # buy = 100*(110-90)/(120-90) = 66.67 ; sell = 33.33 ; delta = +33.33
        self.assertAlmostEqual(evt.buy_volume, 66.6667, places=3)
        self.assertAlmostEqual(evt.sell_volume, 33.3333, places=3)
        self.assertAlmostEqual(evt.delta, 33.3333, places=3)
        self.assertGreater(evt.delta, 0)


class OrderFlowCumulativeTests(SimpleTestCase):
    """(b) cumulative delta accumulates across candles."""

    def test_cumulative(self):
        eng = OrderFlowEngine()
        e1 = eng.update("EURUSD", _candle(100, 120, 90, 110, volume=100), 60)
        e2 = eng.update("EURUSD", _candle(110, 120, 90, 95, volume=100), 60)
        self.assertAlmostEqual(e1.delta, 33.3333, places=3)
        # second candle: buy=(95-90)/30=0.1667, sell=(120-95)/30=0.8333 → delta=-66.67
        self.assertAlmostEqual(e2.delta, -66.6667, places=3)
        self.assertAlmostEqual(
            e2.cumulative_delta, e1.delta + e2.delta, places=3
        )


class OrderFlowImbalanceTests(SimpleTestCase):
    """(c) imbalance regime detected (ratio > 1.5)."""

    def test_imbalance(self):
        eng = OrderFlowEngine()
        # close near the high → strong buy imbalance
        evt = eng.update("EURUSD", _candle(100, 120, 90, 115, volume=100), 60)
        self.assertEqual(evt.regime, "IMBALANCE")
        self.assertEqual(evt.direction, "BULLISH")
        self.assertGreater(evt.imbalance_ratio, 1.5)


class OrderFlowExhaustionTests(SimpleTestCase):
    """(d) exhaustion regime detected (price up, delta down)."""

    def test_exhaustion(self):
        eng = OrderFlowEngine()
        # open 90, close 100 (price up) but close in lower half of range → delta down
        evt = eng.update("EURUSD", _candle(90, 120, 90, 100, volume=100), 60)
        self.assertEqual(evt.regime, "EXHAUSTION")
        self.assertLess(evt.delta, 0)


class OrderFlowMissingVolumeTests(SimpleTestCase):
    """best-effort: no volume key → range-based estimate, never crashes."""

    def test_no_volume(self):
        eng = OrderFlowEngine()
        c = _candle(100, 120, 90, 110)
        c.pop("volume_ticks", None)
        evt = eng.update("EURUSD", c, 60)
        self.assertIsNotNone(evt)
        # No volume → range-based estimate (vol == range == 30):
        # buy = 30*(110-90)/30 = 20, sell = 30*(120-110)/30 = 10 → delta = 10
        self.assertAlmostEqual(evt.delta, 10.0, places=3)


class SignalEngineOrderFlowTests(SimpleTestCase):
    """(e) SignalEngine applies order-flow evidence (mocked engine)."""

    class _FakeOB:
        direction = OBDirection.BULLISH
        high = 1.1050
        low = 1.1000
        quality_score = 80

        def get_active(self, limit=3):
            return [self]

    class _FakeOrderFlow:
        def __init__(self, state):
            self._state = state

        def get_timeframes(self, symbol):
            return [60]

        def get_state(self, symbol, tf):
            return self._state

    def _engine(self, of_state):
        eng = SignalEngine(cache=HotCache())
        eng._obs = {14400: self._FakeOB()}
        eng._order_flow = self._FakeOrderFlow(of_state)
        return eng

    def test_imbalance_evidence_added(self):
        eng = self._engine(
            {"regime": "IMBALANCE", "direction": "BULLISH", "imbalance_ratio": 2.0}
        )
        draft = eng._build_draft("EURUSD", "BUY")
        self.assertIsNotNone(draft)
        self.assertIn("order_flow_imbalance", draft.evidence)
        self.assertEqual(draft.evidence["order_flow_imbalance"], 8.0)

    def test_exhaustion_evidence_negative(self):
        eng = self._engine(
            {"regime": "EXHAUSTION", "direction": "BEARISH", "imbalance_ratio": 0.4}
        )
        draft = eng._build_draft("EURUSD", "BUY")
        self.assertIsNotNone(draft)
        self.assertIn("order_flow_exhaustion", draft.evidence)
        self.assertLess(draft.evidence["order_flow_exhaustion"], 0)

    def test_no_order_flow_engine_no_evidence(self):
        eng = SignalEngine(cache=HotCache())
        eng._obs = {14400: self._FakeOB()}
        draft = eng._build_draft("EURUSD", "BUY")
        self.assertIsNotNone(draft)
        self.assertNotIn("order_flow_imbalance", draft.evidence)
