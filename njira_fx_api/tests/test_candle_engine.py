"""Phase 6.1 — Unit tests for CandleAggregator and CandleRegistry."""
import unittest
from forex.analysis.candle_engine import CandleAggregator, CandleRegistry


class TestCandleAggregator(unittest.TestCase):

    def setUp(self):
        self.agg = CandleAggregator(symbol="EURUSD", timeframes=(60,))

    def test_single_tick_forms_candle(self):
        closed = self.agg.feed_tick(1.1000, 1.1002, 1000000000000)
        self.assertEqual(len(closed), 0)
        forming = self.agg.get_forming(60)
        self.assertIsNotNone(forming)
        self.assertEqual(forming["open"], 1.1001)  # mid = (bid+ask)/2
        self.assertEqual(forming["timeframe"], 60)

    def test_candle_closes_on_boundary(self):
        t0 = 1000000000000
        self.agg.feed_tick(1.1000, 1.1002, t0)
        self.agg.feed_tick(1.1010, 1.1012, t0 + 10000)
        closed = self.agg.feed_tick(1.1020, 1.1022, t0 + 60000)
        self.assertEqual(len(closed), 1)
        c = closed[0]
        self.assertEqual(c["symbol"], "EURUSD")
        self.assertEqual(c["timeframe"], 60)
        self.assertAlmostEqual(c["open"], 1.1001, places=5)
        self.assertAlmostEqual(c["high"], 1.1011, places=5)
        self.assertAlmostEqual(c["low"], 1.1001, places=5)
        self.assertAlmostEqual(c["close"], 1.1011, places=5)
        self.assertTrue(c["is_closed"])

    def test_ohlc_deterministic(self):
        t0 = 1000000000000
        agg1 = CandleAggregator(symbol="EURUSD", timeframes=(60,))
        agg2 = CandleAggregator(symbol="EURUSD", timeframes=(60,))
        ticks = [
            (1.1000, 1.1002, t0),
            (1.1010, 1.1012, t0 + 5000),
            (1.0990, 1.0992, t0 + 10000),
        ]
        for bid, ask, ts in ticks:
            agg1.feed_tick(bid, ask, ts)
            agg2.feed_tick(bid, ask, ts)
        closed1 = agg1.feed_tick(1.1005, 1.1007, t0 + 60000)
        closed2 = agg2.feed_tick(1.1005, 1.1007, t0 + 60000)
        self.assertEqual(len(closed1), 1)
        for key in ("open", "high", "low", "close"):
            self.assertAlmostEqual(closed1[0][key], closed2[0][key], places=6)

    def test_no_lookahead_bias(self):
        t0 = 1000000000000
        self.agg.feed_tick(1.1000, 1.1002, t0)
        forming = self.agg.get_forming(60)
        self.assertAlmostEqual(forming["high"], 1.1001, places=5)
        self.agg.feed_tick(1.1100, 1.1102, t0 + 5000)
        forming = self.agg.get_forming(60)
        self.assertAlmostEqual(forming["high"], 1.1101, places=5)

    def test_feed_batch(self):
        t0 = 1000000000000
        ticks = [
            ("EURUSD", 1.1000, 1.1002, 0.2, 5, t0),
            ("EURUSD", 1.1010, 1.1012, 0.2, 5, t0 + 5000),
            ("EURUSD", 1.1020, 1.1022, 0.2, 5, t0 + 60000),
        ]
        closed = self.agg.feed_batch(ticks)
        self.assertEqual(len(closed), 1)

    def test_reset_clears_candles(self):
        """Reset clears forming + closed but keeps aggregator alive."""
        self.agg.feed_tick(1.1000, 1.1002, 1000000000000)
        self.agg.feed_tick(1.1010, 1.1012, 1060000000000)  # close
        self.assertGreater(len(self.agg.get_closed(60)), 0)
        self.agg.reset()
        self.assertEqual(self.agg.get_closed(60), [])
        self.assertIsNone(self.agg.get_forming(60))


class TestCandleRegistry(unittest.TestCase):

    def test_multi_symbol(self):
        reg = CandleRegistry()
        reg.feed_tick("EURUSD", 1.1000, 1.1002, 1000000000000)
        reg.feed_tick("GBPUSD", 1.3000, 1.3002, 1000000000000)
        self.assertIsNotNone(reg.get_aggregator("EURUSD"))
        self.assertIsNotNone(reg.get_aggregator("GBPUSD"))
        self.assertIsNone(reg.get_aggregator("USDJPY"))

    def test_reset_clears_candles_not_aggregators(self):
        """Registry reset clears candle data but keeps aggregator objects."""
        reg = CandleRegistry()
        reg.feed_tick("EURUSD", 1.1000, 1.1002, 1000000000000)
        self.assertIsNotNone(reg.get_aggregator("EURUSD"))
        reg.reset()
        # Aggregator still exists but has no candles
        agg = reg.get_aggregator("EURUSD")
        self.assertIsNotNone(agg)
        self.assertIsNone(agg.get_forming(60))


if __name__ == "__main__":
    unittest.main()
