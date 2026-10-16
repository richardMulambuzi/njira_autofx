"""
Storyline / MTF multi-timeframe structure unit tests.

Covers the four pillars of the Malaysian SNR storyline read: timeframe
direction, weekly resampling from d1, internal roadblocks (structural swings),
and the change-of-storyline gate between weekly and daily.

Run: python manage.py test forex.tests.test_storyline
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.analysis.storyline import (
    analyze_storyline,
    resample_weekly,
    roadblocks,
    storyline_allows,
    tf_direction,
)

D1 = 86400
BASE_T = 1_700_000_000.0


def _cand(o: float, h: float, l: float, c: float, ot: float) -> dict:
    return {"open_time": ot, "open": o, "high": h, "low": l, "close": c}


def _uptrend(n: int = 20, start: float = 1.3000, step: float = 0.0010) -> list[dict]:
    """n candles, every one bullish, closing steadily higher."""
    return [_cand(p, p + 2 * step, p - 0.5 * step, p + step, BASE_T + i * D1)
            for i, p in enumerate(start + step * i for i in range(n))]


def _downtrend(n: int = 20, start: float = 1.3500, step: float = 0.0010) -> list[dict]:
    """n candles, every one bearish, closing steadily lower."""
    return [_cand(p, p + 0.5 * step, p - 2 * step, p - step, BASE_T + i * D1)
            for i, p in enumerate(start - step * i for i in range(n))]


def _zigzag(n: int = 20) -> list[dict]:
    """Alternating closes around a flat level — no net direction, 50/50 bodies."""
    base = 1.3000
    return [_cand(base, base + 0.0020, base - 0.0020,
                  base + (0.0010 if i % 2 == 0 else -0.0010), BASE_T + i * D1)
            for i in range(n)]


def _d1_ten() -> list[dict]:
    """10 clean d1 candles: open 1.3000 + i*0.0010, high +0.0020, low -0.0010."""
    return [_cand(p, p + 0.0020, p - 0.0010, p + 0.0010, BASE_T + i * D1)
            for i, p in enumerate(1.3000 + 0.0010 * i for i in range(10))]


def _zigzag_swings() -> list[dict]:
    """9 candles with one clear peak (idx2), one clear trough (idx4), one
    smaller late peak (idx6).  Everything else is inside its neighbours."""
    highs = [1.3100, 1.3150, 1.3200, 1.3120, 1.3050, 1.3100, 1.3120, 1.3080, 1.3100]
    lows = [1.3000, 1.3050, 1.3100, 1.3050, 1.3000, 1.3020, 1.3080, 1.3040, 1.3050]
    closes = [1.3050, 1.3100, 1.3150, 1.3070, 1.3010, 1.3080, 1.3090, 1.3060, 1.3080]
    return [_cand(c, h, lo, c, BASE_T + i * D1)
            for i, (h, lo, c) in enumerate(zip(highs, lows, closes))]


def _d1_forty_up_twenty_down() -> list[dict]:
    """60 d1 candles: 40 rising (1.3000 -> 1.3395) then 20 falling (-> 1.3200).

    Weekly reads bullish (8 of 12 weekly bodies up, last close above the
    first) while the daily window -- the last 20 d1 -- is all bearish.
    """
    up = [_cand(1.3000 + 0.0010 * i, 1.3000 + 0.0010 * i + 0.0008,
                1.3000 + 0.0010 * i - 0.0003, 1.3000 + 0.0010 * i + 0.0005,
                BASE_T + i * D1) for i in range(40)]
    down = [_cand(1.3395 - 0.0010 * j, 1.3395 - 0.0010 * j + 0.0003,
                  1.3395 - 0.0010 * j - 0.0008, 1.3395 - 0.0010 * j - 0.0005,
                  BASE_T + (40 + j) * D1) for j in range(20)]
    return up + down


class TfDirectionTests(SimpleTestCase):
    def test_tf_direction_bullish(self):
        candles = _uptrend()
        self.assertEqual(tf_direction(candles), "bullish")
        # honouring the lookback window
        self.assertEqual(tf_direction(candles, lookback=5), "bullish")

    def test_tf_direction_bearish(self):
        candles = _downtrend()
        self.assertEqual(tf_direction(candles), "bearish")
        self.assertEqual(tf_direction(candles, lookback=5), "bearish")

    def test_tf_direction_sideways(self):
        # Net-down closes but only half the bodies bullish -> not bullish;
        # net down yet bearish bodies only half -> not bearish.  Sideways.
        self.assertEqual(tf_direction(_zigzag()), "sideways")

        # Net up but only 3 of 20 bodies bullish -> the AND in tf_direction
        # fails, so still sideways.
        candles = [
            _cand(1.2950 + 0.0020 * i, 1.3000 + 0.0020 * i, 1.2950 + 0.0020 * i - 0.0005,
                  1.3000 + 0.0002 * i, BASE_T + i * D1)
            for i in range(20)
        ]
        self.assertGreater(candles[-1]["close"], candles[0]["close"])  # net up
        self.assertEqual(tf_direction(candles), "sideways")

        # Not enough data to read a storyline.
        self.assertEqual(tf_direction([]), "sideways")
        self.assertEqual(tf_direction([{"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
                                        "open_time": BASE_T}]), "sideways")


class ResampleWeeklyTests(SimpleTestCase):
    def test_resample_weekly_groups_five(self):
        d1 = _d1_ten()
        weekly = resample_weekly(d1)
        self.assertEqual(len(weekly), 2)

        # Week 1 = d1[0:5]
        self.assertAlmostEqual(weekly[0]["open_time"], d1[0]["open_time"], places=6)
        self.assertAlmostEqual(weekly[0]["open"], 1.3000, places=6)
        self.assertAlmostEqual(weekly[0]["high"], 1.3060, places=6)   # d1[4].high
        self.assertAlmostEqual(weekly[0]["low"], 1.2990, places=6)    # d1[0].low
        self.assertAlmostEqual(weekly[0]["close"], 1.3050, places=6)  # d1[4].close

        # Week 2 = d1[5:10]
        self.assertAlmostEqual(weekly[1]["open_time"], d1[5]["open_time"], places=6)
        self.assertAlmostEqual(weekly[1]["open"], 1.3050, places=6)
        self.assertAlmostEqual(weekly[1]["high"], 1.3110, places=6)   # d1[9].high
        self.assertAlmostEqual(weekly[1]["low"], 1.3040, places=6)    # d1[5].low
        self.assertAlmostEqual(weekly[1]["close"], 1.3100, places=6)  # d1[9].close

        # A partial trailing week is still emitted, and empty input is empty.
        self.assertEqual(len(resample_weekly(d1[:7])), 2)
        self.assertEqual(resample_weekly([]), [])


class RoadblockTests(SimpleTestCase):
    def test_roadblocks_finds_swings(self):
        candles = _zigzag_swings()
        found = roadblocks(candles)

        # Most recent first.
        self.assertEqual([(r["type"], r["price"]) for r in found],
                         [("high", 1.3120), ("low", 1.3000), ("high", 1.3200)])
        self.assertAlmostEqual(found[0]["open_time"], candles[6]["open_time"], places=6)
        self.assertAlmostEqual(found[2]["open_time"], candles[2]["open_time"], places=6)

        # Flat series has no swings; so does one too short to have a window.
        self.assertEqual(roadblocks([_cand(1.3, 1.3, 1.3, 1.3, BASE_T + i * D1)
                                     for i in range(9)]), [])
        self.assertEqual(roadblocks(candles[:4]), [])

        # Cap at 10, most recent kept.
        long_series = []
        for i in range(40):
            peak = 1.30 + (i % 2) * 0.01
            long_series.append(_cand(peak - 0.001, peak + 0.001, peak - 0.002,
                                     peak - 0.0005, BASE_T + i * D1))
        self.assertLessEqual(len(roadblocks(long_series)), 10)


class StorylineAllowsTests(SimpleTestCase):
    def test_allows_weekly_bullish_daily_bullish(self):
        # Daily agrees with weekly -> no change of storyline, both sides pass.
        # The gate blocks opposition only; it is not an alignment filter.
        self.assertTrue(storyline_allows("bullish", "bullish", "bullish"))
        self.assertTrue(storyline_allows("bullish", "bullish", "bearish"))

    def test_allows_weekly_bullish_daily_bearish(self):
        # Change of storyline: the weekly bullish run cannot continue.
        self.assertFalse(storyline_allows("bullish", "bearish", "bullish"))
        # The counter-trend side is untouched by the gate.
        self.assertTrue(storyline_allows("bullish", "bearish", "bearish"))

    def test_allows_weekly_bearish_daily_bullish(self):
        self.assertFalse(storyline_allows("bearish", "bullish", "bearish"))
        self.assertTrue(storyline_allows("bearish", "bullish", "bullish"))

    def test_allows_sideways_weekly(self):
        for daily in ("bullish", "bearish", "sideways"):
            self.assertTrue(storyline_allows("sideways", daily, "bullish"))
            self.assertTrue(storyline_allows("sideways", daily, "bearish"))

    def test_daily_sideways_never_opposes(self):
        self.assertTrue(storyline_allows("bullish", "sideways", "bullish"))
        self.assertTrue(storyline_allows("bearish", "sideways", "bearish"))


class AnalyzeStorylineTests(SimpleTestCase):
    def test_analyze_storyline_shape(self):
        # Aligned: 60 rising d1 -> 12 rising weekly candles, daily bullish too.
        result = analyze_storyline(_uptrend(60), _zigzag_swings(), _downtrend(30))

        for key in ("weekly", "daily", "h4", "h1"):
            self.assertIn(result[key], ("bullish", "bearish", "sideways"), key)
        self.assertEqual(result["weekly"], "bullish")
        self.assertEqual(result["daily"], "bullish")
        self.assertEqual(result["h1"], "bearish")

        for tf in ("daily", "h4", "h1"):
            self.assertIsInstance(result["roadblocks"][tf], list)
            for r in result["roadblocks"][tf]:
                self.assertIn(r["type"], ("high", "low"))
                self.assertIsInstance(r["price"], float)
                self.assertIsInstance(r["open_time"], float)

        # The zigzagged H4 actually carries roadblocks; the strictly trending
        # daily/h1 series have no internal swings at all.
        self.assertTrue(result["roadblocks"]["h4"])
        self.assertEqual(result["roadblocks"]["daily"], [])
        self.assertEqual(result["roadblocks"]["h1"], [])

        self.assertIs(result["weekly_allows_buy"], True)
        self.assertIs(result["weekly_allows_sell"], True)

        # Empty inputs stay safe.
        empty = analyze_storyline([], [], [])
        self.assertEqual(empty["weekly"], "sideways")
        self.assertTrue(empty["weekly_allows_buy"])
        self.assertTrue(empty["weekly_allows_sell"])

    def test_analyze_storyline_blocks_on_opposition(self):
        # 40 d1 rising then 20 d1 falling: the weekly storyline is still
        # bullish (8 of 12 weekly bodies up, net up) while the last 20 d1 —
        # the daily window — are all bearish and net down.
        d1 = _d1_forty_up_twenty_down()
        result = analyze_storyline(d1, _zigzag_swings(), _zigzag_swings())
        self.assertEqual(result["weekly"], "bullish")
        self.assertEqual(result["daily"], "bearish")
        self.assertIs(result["weekly_allows_buy"], False)   # blocked
        self.assertIs(result["weekly_allows_sell"], True)
