"""Phase 6.3 — Unit tests for LiquidityEngine."""
import unittest
from forex.analysis.liquidity import LiquidityEngine, LiquidityType, PoolState


def _make_candles(prices, base_time=0):
    """Build candle list from close prices."""
    candles = []
    for i, c in enumerate(prices):
        candles.append({
            "symbol": "EURUSD", "timeframe": 3600,
            "open": c, "high": c + 0.001, "low": c - 0.001,
            "close": c, "open_time": base_time + i * 3600,
            "volume_ticks": 10, "is_closed": True,
        })
    return candles


class TestLiquidity(unittest.TestCase):

    def setUp(self):
        self.engine = LiquidityEngine(
            equal_level_tolerance_pips=0.5,
            sweep_confirmation_candles=3,
            max_pools=50,
        )

    def test_engine_creates(self):
        """Engine instantiates without errors."""
        self.assertIsNotNone(self.engine)

    def test_analyze_returns_events_list(self):
        """analyze() returns a list of events."""
        prices = [1.105, 1.107, 1.110, 1.110, 1.107, 1.105]
        candles = _make_candles(prices)
        events = self.engine.analyze(candles)
        self.assertIsInstance(events, list)

    def test_get_pools_returns_list(self):
        """get_pools() returns a list."""
        prices = [1.105, 1.107, 1.110, 1.110, 1.107, 1.105]
        candles = _make_candles(prices)
        self.engine.analyze(candles)
        pools = self.engine.get_pools(3600)
        self.assertIsInstance(pools, list)

    def test_pool_has_required_fields(self):
        """Pools have pool_type, price, state fields."""
        prices = [1.105, 1.107, 1.110, 1.110, 1.107, 1.105]
        candles = _make_candles(prices)
        self.engine.analyze(candles)
        pools = self.engine.get_pools(3600)
        for p in pools:
            self.assertTrue(hasattr(p, 'pool_type'))
            self.assertTrue(hasattr(p, 'price'))
            self.assertTrue(hasattr(p, 'state'))

    def test_pool_state_valid(self):
        """Pool states are valid enum values."""
        prices = [1.105, 1.107, 1.110, 1.110, 1.107, 1.105]
        candles = _make_candles(prices)
        self.engine.analyze(candles)
        pools = self.engine.get_pools(3600)
        valid_states = {PoolState.POOL, PoolState.SWEPT, PoolState.CONFIRMED, PoolState.INVALIDATED}
        for p in pools:
            self.assertIn(p.state, valid_states)

    def test_no_pools_in_flat_market(self):
        """Flat market → no pools."""
        prices = [1.1000] * 10
        candles = _make_candles(prices)
        self.engine.analyze(candles)
        pools = self.engine.get_pools(3600)
        self.assertEqual(len(pools), 0)

    def test_reset(self):
        """Reset clears all pools."""
        prices = [1.105, 1.107, 1.110, 1.110, 1.107, 1.105]
        candles = _make_candles(prices)
        self.engine.analyze(candles)
        self.engine.reset()
        self.assertEqual(self.engine.get_pools(3600), [])

    def test_insufficient_data(self):
        """Too few candles → no pools."""
        candles = _make_candles([1.1000] * 2)
        events = self.engine.analyze(candles)
        self.assertEqual(len(events), 0)


if __name__ == "__main__":
    unittest.main()
