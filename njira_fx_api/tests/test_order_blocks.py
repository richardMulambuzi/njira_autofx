"""Phase 6.4 — Unit tests for OrderBlockEngine."""
import unittest
from forex.analysis.order_blocks import OrderBlockEngine, OBState, OBDirection


def _make_candles(data, base_time=0):
    """Build candle list from (open, high, low, close) tuples."""
    candles = []
    for i, d in enumerate(data):
        if isinstance(d, (int, float)):
            o = h = l = c = d
        else:
            o, h, l, c = d
        candles.append({
            "symbol": "EURUSD", "timeframe": 3600,
            "open": o, "high": h, "low": l, "close": c,
            "open_time": base_time + i * 3600,
            "volume_ticks": 10, "is_closed": True,
        })
    return candles


class TestOrderBlocks(unittest.TestCase):

    def setUp(self):
        self.engine = OrderBlockEngine(
            displacement_threshold=1.5,
            max_obs=30,
            max_age_candles=100,
        )

    def test_engine_creates(self):
        self.assertIsNotNone(self.engine)

    def test_analyze_returns_events_list(self):
        data = [
            (1.1000, 1.1005, 1.0995, 1.0998),
            (1.0998, 1.1015, 1.0997, 1.1012),
        ]
        candles = _make_candles(data)
        events = self.engine.analyze(candles, 3600)
        self.assertIsInstance(events, list)

    def test_get_all_returns_list(self):
        data = [
            (1.1000, 1.1005, 1.0995, 1.0998),
            (1.0998, 1.1015, 1.0997, 1.1012),
        ]
        candles = _make_candles(data)
        self.engine.analyze(candles, 3600)
        obs = self.engine.get_all()
        self.assertIsInstance(obs, list)

    def test_ob_has_required_fields(self):
        data = [
            (1.1000, 1.1005, 1.0995, 1.0998),
            (1.0998, 1.1020, 1.0997, 1.1015),
        ]
        candles = _make_candles(data)
        self.engine.analyze(candles, 3600)
        obs = self.engine.get_all()
        for ob in obs:
            self.assertTrue(hasattr(ob, 'direction'))
            self.assertTrue(hasattr(ob, 'state'))
            self.assertTrue(hasattr(ob, 'quality_score'))

    def test_quality_scoring_range(self):
        """OB quality is 0-100 range."""
        data = [
            (1.1000, 1.1005, 1.0995, 1.0998),
            (1.0998, 1.1020, 1.0997, 1.1015),
        ]
        candles = _make_candles(data)
        self.engine.analyze(candles, 3600)
        obs = self.engine.get_all()
        for ob in obs:
            self.assertGreaterEqual(ob.quality_score, 0)
            self.assertLessEqual(ob.quality_score, 100)

    def test_ob_states_valid(self):
        data = [
            (1.1000, 1.1005, 1.0995, 1.0998),
            (1.0998, 1.1015, 1.0997, 1.1012),
        ]
        candles = _make_candles(data)
        self.engine.analyze(candles, 3600)
        obs = self.engine.get_all()
        valid_states = {OBState.CANDIDATE, OBState.VALID, OBState.PARTIALLY_MITIGATED,
                        OBState.MITIGATED, OBState.INVALIDATED, OBState.EXPIRED}
        for ob in obs:
            self.assertIn(ob.state, valid_states)

    def test_no_ob_in_flat_market(self):
        data = [1.1000] * 10
        candles = _make_candles(data)
        events = self.engine.analyze(candles, 3600)
        self.assertEqual(len(events), 0)

    def test_reset(self):
        data = [
            (1.1000, 1.1005, 1.0995, 1.0998),
            (1.0998, 1.1015, 1.0997, 1.1012),
        ]
        candles = _make_candles(data)
        self.engine.analyze(candles, 3600)
        self.engine.reset()
        self.assertEqual(self.engine.get_all(), [])


if __name__ == "__main__":
    unittest.main()
