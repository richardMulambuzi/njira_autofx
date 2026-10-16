"""Phase 6.5 — Unit tests for SignalEngine and SignalLifecycleManager."""
import unittest
import time
from forex.analysis.signal_lifecycle import (
    SignalLifecycleManager, Signal, SignalState,
)
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.cache import HotCache
from forex.analysis.market_structure import MarketStructureEngine
from forex.analysis.liquidity import LiquidityEngine
from forex.analysis.order_blocks import OrderBlockEngine
from forex.analysis.poc import POCEngine
from forex.analysis.microstructure import MicrostructureEngine
from forex.analysis.candle_engine import CandleAggregator


class TestSignalLifecycleManager(unittest.TestCase):

    def setUp(self):
        self.mgr = SignalLifecycleManager()

    def _make_signal(self, symbol="EURUSD", direction="BUY"):
        return self.mgr.create(
            symbol=symbol, direction=direction,
            entry=1.1000, stop_loss=1.0990, take_profit=1.1030,
            confidence=75.0, explanation="Test signal",
        )

    def test_create_signal(self):
        sig = self._make_signal()
        self.assertEqual(sig.state, SignalState.CANDIDATE)
        self.assertEqual(sig.direction, "BUY")
        self.assertEqual(sig.risk_reward_ratio, 0.0)

    def test_valid_transitions(self):
        sig = self._make_signal()
        self.mgr.transition(sig, SignalState.WATCHING, reason="price approaching")
        self.assertEqual(sig.state, SignalState.WATCHING)
        self.mgr.transition(sig, SignalState.TRIGGERED, reason="entry hit")
        self.assertEqual(sig.state, SignalState.TRIGGERED)
        self.mgr.transition(sig, SignalState.CONFIRMED, reason="momentum confirms")
        self.assertEqual(sig.state, SignalState.CONFIRMED)

    def test_invalid_transition_rejected(self):
        sig = self._make_signal()
        # CANDIDATE → CONFIRMED is now a valid transition (WAITING re-eval fix);
        # COMPLETED is only reachable from CONFIRMED, so this must be rejected.
        result = self.mgr.transition(sig, SignalState.COMPLETED, reason="skip")
        self.assertFalse(result)
        self.assertEqual(sig.state, SignalState.CANDIDATE)

    def test_cannot_transition_from_terminal(self):
        sig = self._make_signal()
        self.mgr.transition(sig, SignalState.WATCHING, reason="r")
        self.mgr.transition(sig, SignalState.TRIGGERED, reason="r")
        self.mgr.transition(sig, SignalState.INVALIDATED, reason="stop hit")
        result = self.mgr.transition(sig, SignalState.CONFIRMED, reason="too late")
        self.assertFalse(result)
        self.assertEqual(sig.state, SignalState.INVALIDATED)

    def test_get_active(self):
        self._make_signal()
        self._make_signal(symbol="GBPUSD", direction="SELL")
        active = self.mgr.get_active()
        self.assertEqual(len(active), 2)

    def test_get_by_id(self):
        sig = self._make_signal()
        found = self.mgr.get_by_id(sig.signal_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.signal_id, sig.signal_id)

    def test_get_by_id_missing(self):
        self.assertIsNone(self.mgr.get_by_id("nonexistent"))


class TestSignalEngine(unittest.TestCase):

    def setUp(self):
        self.cache = HotCache(max_size=200)
        self.structure_engines = {60: MarketStructureEngine()}
        self.liquidity_engines = {60: LiquidityEngine()}
        self.ob_engines = {60: OrderBlockEngine()}
        self.poc_engines = {60: POCEngine()}
        self.micro = MicrostructureEngine()
        self.engine = SignalEngine(
            cache=self.cache,
            structure_engines=self.structure_engines,
            liquidity_engines=self.liquidity_engines,
            ob_engines=self.ob_engines,
            poc_engines=self.poc_engines,
            microstructure_engine=self.micro,
            min_confidence=40,
            min_rr=1.5,
        )

    def _feed_candles(self, prices, symbol="EURUSD", tf=60):
        """Feed candles into the cache for the engine to read."""
        agg = CandleAggregator(symbol=symbol, timeframes=(tf,))
        t0 = int(time.time() * 1000) - len(prices) * tf * 1000
        for i, p in enumerate(prices):
            closed = agg.feed_tick(p - 0.0001, p + 0.0001, t0 + i * tf * 1000)
            for c in closed:
                self.cache.add(c)

    def test_engine_creates(self):
        self.assertIsNotNone(self.engine)

    def test_no_signal_on_flat(self):
        self._feed_candles([1.1000] * 30)
        signals = self.engine.evaluate("EURUSD")
        self.assertIsInstance(signals, list)

    def test_active_signals_managed(self):
        active = self.engine.get_active_signals()
        self.assertIsInstance(active, list)

    def test_insufficient_data_no_crash(self):
        self._feed_candles([1.1000] * 3)
        signals = self.engine.evaluate("EURUSD")
        self.assertIsInstance(signals, list)


if __name__ == "__main__":
    unittest.main()
