"""
QA Gate 2 — MultiTimeframeStrategy unit tests.

Covers forex/mtf_strategy.py:

* calculate_simple_moving_average — math + insufficient-data -> None.
* _rolling_alignment — BUY / SELL / HOLD / touch / insufficient bars.
* check_market_alignment — empty bars, insufficient bars, all-BUY, all-SELL,
  mixed -> HOLD.

ClickHouse reads are mocked (get_ohlc_bars) — no live server is contacted.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from forex.mtf_strategy import (
    BARS_PER_TIMEFRAME,
    SMA_PERIOD,
    TIMEFRAMES,
    MultiTimeframeStrategy,
)


def _bars(closes: list[float]) -> list[dict]:
    """Build OHLC bar dicts from a close sequence (open/high/low = close)."""
    return [
        {"time": f"2026-08-17T10:{i:02d}:00Z", "open": c, "high": c, "low": c, "close": c}
        for i, c in enumerate(closes)
    ]


class SmaMathTests(SimpleTestCase):
    """calculate_simple_moving_average — pure math."""

    def setUp(self):
        self.strategy = MultiTimeframeStrategy()

    def test_sma_of_last_period_closes(self):
        bars = _bars([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 20.0])
        # period=10 -> mean of the LAST 10 closes (2..20), not the first 10.
        self.assertEqual(self.strategy.calculate_simple_moving_average(bars, 10), 7.4)

    def test_sma_with_exactly_period_bars(self):
        bars = _bars([10.0] * 10)
        self.assertEqual(self.strategy.calculate_simple_moving_average(bars, 10), 10.0)

    def test_sma_with_fewer_than_period_bars_returns_none(self):
        bars = _bars([1.0, 2.0, 3.0])
        self.assertIsNone(self.strategy.calculate_simple_moving_average(bars, 10))

    def test_sma_with_empty_bars_returns_none(self):
        self.assertIsNone(self.strategy.calculate_simple_moving_average([], 10))


class RollingAlignmentTests(SimpleTestCase):
    """_rolling_alignment — per-bar rolling SMA comparison."""

    def setUp(self):
        self.strategy = MultiTimeframeStrategy()

    def test_strictly_rising_closes_align_buy(self):
        # Every close from the 10th bar onward sits above its rolling 10-SMA.
        bars = _bars([float(i) for i in range(1, 21)])
        self.assertEqual(self.strategy._rolling_alignment(bars), "BUY")

    def test_strictly_falling_closes_align_sell(self):
        bars = _bars([float(i) for i in range(20, 0, -1)])
        self.assertEqual(self.strategy._rolling_alignment(bars), "SELL")

    def test_mixed_closes_hold(self):
        # Up-then-down: some closes above, some below their rolling SMA.
        bars = _bars([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 1.0, 2.0])
        self.assertEqual(self.strategy._rolling_alignment(bars), "HOLD")

    def test_close_touching_sma_holds(self):
        # A close exactly equal to its rolling SMA is a touch -> HOLD.
        bars = _bars([5.0] * 20)
        self.assertEqual(self.strategy._rolling_alignment(bars), "HOLD")

    def test_fewer_than_period_bars_returns_none(self):
        bars = _bars([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertIsNone(self.strategy._rolling_alignment(bars))

    def test_empty_bars_returns_none(self):
        self.assertIsNone(self.strategy._rolling_alignment([]))


class MarketAlignmentTests(SimpleTestCase):
    """check_market_alignment — all timeframes must agree."""

    def setUp(self):
        self.strategy = MultiTimeframeStrategy()

    def _mock_bars(self, closes_by_timeframe: dict[str, list[float]]):
        """Patch get_ohlc_bars to return per-timeframe close sequences."""
        def _fake_get_ohlc_bars(client, symbol, minutes, limit=BARS_PER_TIMEFRAME):
            for label, window in TIMEFRAMES.items():
                if window == minutes:
                    closes = closes_by_timeframe.get(label, [])
                    return _bars(closes)
            return []

        return mock.patch(
            "forex.mtf_strategy.get_ohlc_bars", side_effect=_fake_get_ohlc_bars
        )

    def test_all_timeframes_buy(self):
        rising = [float(i) for i in range(1, 21)]
        with (
            mock.patch("forex.mtf_strategy.get_clickhouse_client"),
            self._mock_bars({label: rising for label in TIMEFRAMES}),
        ):
            self.assertEqual(self.strategy.check_market_alignment("EURUSD"), "BUY")

    def test_all_timeframes_sell(self):
        falling = [float(i) for i in range(20, 0, -1)]
        with (
            mock.patch("forex.mtf_strategy.get_clickhouse_client"),
            self._mock_bars({label: falling for label in TIMEFRAMES}),
        ):
            self.assertEqual(self.strategy.check_market_alignment("EURUSD"), "SELL")

    def test_mixed_alignment_holds(self):
        rising = [float(i) for i in range(1, 21)]
        falling = [float(i) for i in range(20, 0, -1)]
        closes = {label: rising for label in TIMEFRAMES}
        closes["1H"] = falling  # one timeframe disagrees -> HOLD
        with (
            mock.patch("forex.mtf_strategy.get_clickhouse_client"),
            self._mock_bars(closes),
        ):
            self.assertEqual(self.strategy.check_market_alignment("EURUSD"), "HOLD")

    def test_empty_bars_short_circuit_to_hold(self):
        # First timeframe returns no bars -> immediate HOLD (no further reads).
        with (
            mock.patch("forex.mtf_strategy.get_clickhouse_client"),
            mock.patch(
                "forex.mtf_strategy.get_ohlc_bars", return_value=[]
            ) as m_bars,
        ):
            self.assertEqual(self.strategy.check_market_alignment("EURUSD"), "HOLD")
        m_bars.assert_called_once()  # short-circuits on the first timeframe

    def test_insufficient_bars_short_circuit_to_hold(self):
        with (
            mock.patch("forex.mtf_strategy.get_clickhouse_client"),
            mock.patch(
                "forex.mtf_strategy.get_ohlc_bars", return_value=_bars([1.0, 2.0])
            ) as m_bars,
        ):
            self.assertEqual(self.strategy.check_market_alignment("EURUSD"), "HOLD")
        m_bars.assert_called_once()

    def test_ohlc_read_failure_maps_to_hold(self):
        # get_ohlc_bars returns [] on ClickHouse failure (best-effort) -> HOLD.
        with (
            mock.patch("forex.mtf_strategy.get_clickhouse_client"),
            mock.patch("forex.mtf_strategy.get_ohlc_bars", return_value=[]),
        ):
            self.assertEqual(self.strategy.check_market_alignment("EURUSD"), "HOLD")

    def test_requests_bars_per_timeframe(self):
        rising = [float(i) for i in range(1, 21)]
        with (
            mock.patch("forex.mtf_strategy.get_clickhouse_client"),
            mock.patch(
                "forex.mtf_strategy.get_ohlc_bars", return_value=_bars(rising)
            ) as m_bars,
        ):
            self.strategy.check_market_alignment("EURUSD")
        # One call per timeframe, each requesting BARS_PER_TIMEFRAME bars.
        self.assertEqual(m_bars.call_count, len(TIMEFRAMES))
        for call in m_bars.call_args_list:
            self.assertEqual(call.kwargs["limit"], BARS_PER_TIMEFRAME)