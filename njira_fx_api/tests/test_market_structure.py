"""Phase 6.2 — Unit tests for MarketStructureEngine."""
import unittest
from forex.analysis.market_structure import (
    MarketStructureEngine, Bias, MarketRegime,
)


def _make_candles(prices, base_time=0):
    """Build candle list from close prices (open=close, small wicks)."""
    candles = []
    for i, c in enumerate(prices):
        candles.append({
            "symbol": "EURUSD", "timeframe": 3600,
            "open": c, "high": c + 0.001, "low": c - 0.001,
            "close": c, "open_time": base_time + i * 3600,
            "volume_ticks": 10, "is_closed": True,
        })
    return candles


class TestMarketStructure(unittest.TestCase):

    def setUp(self):
        self.engine = MarketStructureEngine()

    def _zigzag_prices(self, n=60, amp=0.005):
        """Zigzag pattern that guarantees swing highs and lows."""
        prices = []
        for i in range(n):
            prices.append(1.10 + (i % 3) * amp - amp)
        return prices

    def test_swing_high_detection(self):
        prices = self._zigzag_prices(60, 0.005)
        candles = _make_candles(prices)
        self.engine.analyze(candles, 3600)
        state = self.engine.get_state(3600)
        self.assertIsNotNone(state)
        self.assertGreater(len(state.swing_highs), 0)

    def test_swing_low_detection(self):
        prices = self._zigzag_prices(60, 0.005)
        candles = _make_candles(prices)
        self.engine.analyze(candles, 3600)
        state = self.engine.get_state(3600)
        self.assertIsNotNone(state)
        self.assertGreater(len(state.swing_lows), 0)

    def test_bias_default_neutral(self):
        prices = [1.1000] * 60
        candles = _make_candles(prices)
        self.engine.analyze(candles, 3600)
        state = self.engine.get_state(3600)
        self.assertEqual(state.bias, Bias.NEUTRAL)

    def test_insufficient_data(self):
        prices = [1.1000] * 3
        candles = _make_candles(prices)
        events = self.engine.analyze(candles, 3600)
        self.assertEqual(len(events), 0)

    def test_analyze_returns_events_list(self):
        prices = self._zigzag_prices(60, 0.005)
        candles = _make_candles(prices)
        events = self.engine.analyze(candles, 3600)
        self.assertIsInstance(events, list)

    def test_reset(self):
        candles = _make_candles(self._zigzag_prices(60))
        self.engine.analyze(candles, 3600)
        self.engine.reset()
        self.assertIsNone(self.engine.get_state(3600))

    def test_regime_is_valid_enum(self):
        candles = _make_candles(self._zigzag_prices(60))
        self.engine.analyze(candles, 3600)
        regime = self.engine.get_regime(3600)
        self.assertIsInstance(regime, MarketRegime)


if __name__ == "__main__":
    unittest.main()
