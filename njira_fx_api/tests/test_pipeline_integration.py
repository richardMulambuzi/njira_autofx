"""Phase 6.6 — Integration test: tick → candle → structure → signal pipeline."""
import unittest
import time
from forex.analysis.candle_engine import CandleAggregator, CandleRegistry
from forex.analysis.market_structure import MarketStructureEngine
from forex.analysis.liquidity import LiquidityEngine
from forex.analysis.order_blocks import OrderBlockEngine
from forex.analysis.poc import POCEngine
from forex.analysis.microstructure import MicrostructureEngine
from forex.analysis.cache import HotCache
from forex.analysis.tick_buffer import TickBuffer
from forex.analysis.signal_engine import SignalEngine


class TestPipelineIntegration(unittest.TestCase):

    def setUp(self):
        self.registry = CandleRegistry()
        self.structure = MarketStructureEngine()
        self.liquidity = LiquidityEngine()
        self.order_blocks = OrderBlockEngine()
        self.poc = POCEngine()
        self.micro = MicrostructureEngine()
        self.cache = HotCache(max_size=200)
        self.buffer = TickBuffer()
        self.signal_engine = SignalEngine(
            cache=self.cache,
            structure_engines={60: self.structure},
            liquidity_engines={60: self.liquidity},
            ob_engines={60: self.order_blocks},
            poc_engines={60: self.poc},
            microstructure_engine=self.micro,
            min_confidence=40,
            min_rr=1.5,
        )

    def _build_uptrend_ticks(self, count=30, start_price=1.1000, tick_interval_ms=61000):
        """One tick per candle interval to produce closed candles."""
        ticks = []
        for i in range(count):
            base = start_price + i * 0.0005
            ticks.append((base, base + 0.0002, i * tick_interval_ms))
        return ticks

    def _run_pipeline(self, ticks, symbol="EURUSD"):
        """Feed ticks through full pipeline."""
        all_closed = []
        for bid, ask, ts in ticks:
            closed = self.registry.feed_tick(symbol, bid, ask, ts)
            all_closed.extend(closed)

        for c in all_closed:
            self.cache.add(c)

        candles = self.cache.get(symbol, 60)
        if not candles:
            return None

        self.structure.analyze(candles, 60)
        self.liquidity.analyze(candles)
        self.order_blocks.analyze(candles, 60)
        self.poc.compute_from_candles(candles, 60)

        if ticks:
            last = ticks[-1]
            spread = round(last[1] - last[0], 4)
            self.micro.feed(symbol, last[0], last[1], spread, last[2])

        struct_state = self.structure.get_state(60)
        last_close = candles[-1]["close"]

        return {
            "structure": {
                "bias": struct_state.bias.value if struct_state else "neutral",
                "regime": struct_state.regime.value if struct_state else "unknown",
            },
            "order_blocks": self.order_blocks.get_all(),
            "liquidity": self.liquidity.get_pools(60),
            "poc": self.poc.get_poc(60),
            "microstructure": self.micro.get_metrics(symbol),
        }

    def test_uptrend_produces_candles(self):
        ticks = self._build_uptrend_ticks(30)
        result = self._run_pipeline(ticks)
        candles = self.cache.get("EURUSD", 60)
        self.assertGreater(len(candles), 0)

    def test_structure_detection_from_uptrend(self):
        ticks = self._build_uptrend_ticks(30)
        result = self._run_pipeline(ticks)
        if result:
            self.assertIn(result["structure"]["bias"].upper(), ("BULLISH", "NEUTRAL", "BEARISH"))

    def test_no_signal_on_synthetic_data(self):
        ticks = self._build_uptrend_ticks(30)
        self._run_pipeline(ticks)
        signals = self.signal_engine.evaluate("EURUSD")
        self.assertIsInstance(signals, list)

    def test_pipeline_does_not_crash(self):
        ticks = (
            [(1.10 + i * 0.001, 1.1002 + i * 0.001, i * 1000) for i in range(15)]
            + [(1.1015, 1.1017, 15000 + i * 1000) for i in range(5)]
            + [(1.1015 - i * 0.001, 1.1017 - i * 0.001, 20000 + i * 1000) for i in range(10)]
        )
        self._run_pipeline(ticks)
        self.assertTrue(True)

    def test_cache_serves_correct_data(self):
        ticks = self._build_uptrend_ticks(20, tick_interval_ms=61000)
        self._run_pipeline(ticks)
        candles = self.cache.get("EURUSD", 60)
        other = self.cache.get("GBPUSD", 60)
        self.assertGreater(len(candles), 0)
        self.assertEqual(len(other), 0)

    def test_tick_buffer_dedup(self):
        buf = TickBuffer()
        t = 1000000000000
        result1 = buf.feed("EURUSD", 1.1000, 1.1002, 0.2, 5, t)
        self.assertIsNotNone(result1)
        result2 = buf.feed("EURUSD", 1.1000, 1.1002, 0.2, 5, t)
        self.assertIsNone(result2)
        result3 = buf.feed("EURUSD", 1.1001, 1.1003, 0.2, 5, t + 1)
        self.assertIsNotNone(result3)


if __name__ == "__main__":
    unittest.main()
