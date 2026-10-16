"""
Engulfing pattern unit tests — Malaysian SNR strategy.

Course spec under test:
    "The second candlestick completely engulfs or covers up the first candlestick."
    "When HTF Gap has within a LTF Engulfing ... H4 GAP SNR REFINED INTO LTF (H1)
     ENGULFING: Scalping for Maximum Profits!"

open_time is epoch SECONDS throughout these fixtures.

Run: python manage.py test forex.tests.test_engulfing
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.analysis.engulfing import (
    detect_engulfing,
    detect_gap,
    engulfing_entry,
    find_engulfing_in_gap,
)

H4 = 14400
H1 = 3600
G0 = 1_000_000.0  # arbitrary epoch seconds: the H4 gap candle's open_time


def _c(o: float, h: float, l: float, c: float, ot: float) -> dict:
    return {"open_time": ot, "open": o, "high": h, "low": l, "close": c}


class EngulfingDetectionTests(SimpleTestCase):
    def test_bullish_engulfing_detected(self):
        # prev: small bearish body 1.3000 → 1.2990.  cur: bullish body
        # 1.2988 → 1.3012, opening below prev close and closing above prev open.
        candles = [
            _c(1.3000, 1.3010, 1.2985, 1.2990, G0),
            _c(1.2988, 1.3015, 1.2985, 1.3012, G0 + H1),
        ]
        self.assertEqual(detect_engulfing(candles, 1), "bullish")

    def test_bearish_engulfing_detected(self):
        # Mirror: prev small bullish 1.2990 → 1.3000, cur bearish 1.3002 → 1.2980.
        candles = [
            _c(1.2990, 1.3005, 1.2985, 1.3000, G0),
            _c(1.3002, 1.3005, 1.2980, 1.2980, G0 + H1),
        ]
        self.assertEqual(detect_engulfing(candles, 1), "bearish")

    def test_no_engulfing_when_body_does_not_cover(self):
        # Geometric non-coverage: the bullish close stops short of prev's open.
        short = [
            _c(1.3000, 1.3010, 1.2985, 1.2995, G0),
            _c(1.2990, 1.2995, 1.2980, 1.2992, G0 + H1),
        ]
        self.assertIsNone(detect_engulfing(short, 1))

        # Covers geometrically but with an EQUAL body size — the cover must be
        # strict, so an identical body is not an engulfing.
        equal = [
            _c(1.3000, 1.3010, 1.2990, 1.3010, G0),   # bullish body 0.0010
            _c(1.2995, 1.3010, 1.2990, 1.3005, G0 + H1),  # bullish body 0.0010
        ]
        self.assertIsNone(detect_engulfing(equal, 1))

    def test_no_engulfing_when_same_direction(self):
        # Two consecutive bullish candles — a continuation, not an engulfing:
        # the second opens above the first's close, so it covers no body.
        candles = [
            _c(1.3000, 1.3012, 1.2998, 1.3010, G0),
            _c(1.3012, 1.3022, 1.3010, 1.3020, G0 + H1),
        ]
        self.assertIsNone(detect_engulfing(candles, 1))

    def test_engulfing_requires_previous_candle(self):
        candles = [_c(1.3000, 1.3010, 1.2990, 1.2995, G0)]
        self.assertIsNone(detect_engulfing(candles, 0))
        self.assertIsNone(detect_engulfing([], 1))


class GapDetectionTests(SimpleTestCase):
    """Book p.13 GAP SNR: the open-close zone between two candles.

    Wicks are ignored — the gap is where the current candle OPENS relative to
    the previous candle's CLOSE, not a conventional tick gap to the high/low.
    """

    def test_bullish_gap_detected(self):
        # cur open 1.3015 sits above prev CLOSE 1.3005.
        candles = [
            _c(1.2990, 1.3010, 1.2980, 1.3005, G0),
            _c(1.3015, 1.3030, 1.3010, 1.3025, G0 + H4),
        ]
        self.assertEqual(detect_gap(candles, 1), "bullish")

    def test_bearish_gap_detected(self):
        # cur open 1.2985 sits below prev CLOSE 1.2995.
        candles = [
            _c(1.3000, 1.3010, 1.2990, 1.2995, G0),
            _c(1.2985, 1.2995, 1.2980, 1.2990, G0 + H4),
        ]
        self.assertEqual(detect_gap(candles, 1), "bearish")

    def test_no_gap(self):
        # Open sits exactly at the previous close — no open-close zone.
        candles = [
            _c(1.2990, 1.3010, 1.2980, 1.3005, G0),
            _c(1.3005, 1.3020, 1.2990, 1.3015, G0 + H4),
        ]
        self.assertIsNone(detect_gap(candles, 1))


def _h4_gap_bullish() -> list[dict]:
    """idx1 opens at G0 gapping above idx0's high → bullish H4 gap."""
    return [
        _c(1.2980, 1.3010, 1.2970, 1.3005, G0 - H4),
        _c(1.3020, 1.3060, 1.3015, 1.3050, G0),
    ]


def _h1_into_bullish_gap() -> list[dict]:
    """
    H1 bars bracketing the H4 gap candle.  Only idx4 (at G0+3h) is a bullish
    engulfing inside the 4h zone; idx5 (at G0+4h) is a much larger engulfing
    that falls OUTSIDE the zone and must not be picked up.
    """
    return [
        _c(1.3000, 1.3010, 1.2990, 1.3005, G0 - H1),   # before the zone
        _c(1.3010, 1.3020, 1.3000, 1.3015, G0),
        _c(1.3000, 1.3010, 1.2995, 1.2990, G0 + H1),   # small bearish
        _c(1.2988, 1.2995, 1.2980, 1.2985, G0 + 2 * H1),  # small bearish
        _c(1.2982, 1.3000, 1.2980, 1.2998, G0 + 3 * H1),  # BULLISH ENGULFING
        _c(1.2990, 1.3050, 1.2985, 1.3040, G0 + 4 * H1),  # engulfing, out of zone
    ]


class FindEngulfingInGapTests(SimpleTestCase):
    def test_find_engulfing_in_gap(self):
        found = find_engulfing_in_gap(_h4_gap_bullish(), 1, _h1_into_bullish_gap(), H1)
        self.assertIsNotNone(found)
        self.assertEqual(found["direction"], "bullish")
        self.assertEqual(found["index"], 4)
        self.assertEqual(found["open_time"], G0 + 3 * H1)
        self.assertAlmostEqual(found["price"], 1.2998, places=5)
        # The larger engulfing at idx5 sits at G0+4h — the zone is exclusive.
        self.assertGreaterEqual(_h1_into_bullish_gap()[5]["open_time"], G0 + H4)

    def test_find_engulfing_in_gap_returns_none_when_no_engulfing(self):
        # Flatten the in-zone engulfing; only the out-of-zone one remains.
        ltf = _h1_into_bullish_gap()
        ltf[4] = _c(1.2985, 1.2990, 1.2980, 1.2986, G0 + 3 * H1)
        self.assertIsNone(find_engulfing_in_gap(_h4_gap_bullish(), 1, ltf, H1))

    def test_find_engulfing_in_gap_requires_a_gap(self):
        h4 = _h4_gap_bullish()
        h4[1] = _c(1.3000, 1.3030, 1.2995, 1.3025, G0)  # open inside prior range
        self.assertIsNone(find_engulfing_in_gap(h4, 1, _h1_into_bullish_gap(), H1))


class EngulfingEntryTests(SimpleTestCase):
    def test_engulfing_entry_math(self):
        bullish = [
            _c(1.3000, 1.3010, 1.2985, 1.2990, G0),          # prev bearish
            _c(1.2988, 1.3015, 1.2985, 1.3012, G0 + H1),    # engulfing
            _c(1.3015, 1.3020, 1.3010, 1.3018, G0 + 2 * H1),  # next → entry
        ]
        trade = engulfing_entry(bullish, 1)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["direction"], "bullish")
        self.assertEqual(trade["candle_index"], 1)
        self.assertEqual(trade["open_time"], G0 + H1)
        self.assertAlmostEqual(trade["entry"], 1.3015, places=5)   # next open
        self.assertAlmostEqual(trade["sl"], 1.2985, places=5)      # engulfing low
        risk = trade["entry"] - trade["sl"]
        self.assertAlmostEqual(trade["tp"], trade["entry"] + 2.0 * risk, places=6)

        bearish = [
            _c(1.2990, 1.3005, 1.2985, 1.3000, G0),          # prev bullish
            _c(1.3002, 1.3005, 1.2980, 1.2980, G0 + H1),    # engulfing
            _c(1.2978, 1.2985, 1.2975, 1.2982, G0 + 2 * H1),  # next → entry
        ]
        trade = engulfing_entry(bearish, 1)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["direction"], "bearish")
        self.assertAlmostEqual(trade["entry"], 1.2978, places=5)
        self.assertAlmostEqual(trade["sl"], 1.3005, places=5)      # engulfing high
        risk = trade["sl"] - trade["entry"]
        self.assertAlmostEqual(trade["tp"], trade["entry"] - 2.0 * risk, places=6)

    def test_engulfing_entry_needs_a_next_candle(self):
        bullish = [
            _c(1.3000, 1.3010, 1.2985, 1.2990, G0),
            _c(1.2988, 1.3015, 1.2985, 1.3012, G0 + H1),
        ]
        self.assertIsNone(engulfing_entry(bullish, 1))  # no index+1
        self.assertIsNone(engulfing_entry(bullish, 0))  # no engulfing at index
