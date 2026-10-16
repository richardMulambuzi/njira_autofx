"""
MARRIAGE CONCEPT (SNR + Trendline) unit tests — Malaysian SNR course.

The two SNRs are married on their second anchors (body close → next open),
giving a sloped line.  Point #3 is that line extended past the newer SNR;
the trade happens only where a candle wicks into the line and rejects it.

Run: python manage.py test forex.tests.test_marriage
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.analysis.marriage import (
    entry_at_point3,
    line_value,
    marriage_trendline,
)


def _level(t2: float, y2: float, kind: str = "support") -> dict:
    """SNR level dict shaped like SignalEngine._snr_trendline's output."""
    return {"t1": t2 - 100.0, "y1": y2, "t2": t2, "y2": y2,
            "index": 1, "kind": kind}


def _series(specs: list[tuple[float, float, float, float, float]]) -> list[dict]:
    """Build candles from (open, high, low, close, open_time_seconds)."""
    return [{"open_time": float(t), "open": o, "high": h, "low": l, "close": c}
            for o, h, l, c, t in specs]


# Two support SNRs forming at t=100 (y=1.2000) and t=500 (y=1.1100):
# the married line falls 0.000225 per second through both anchors.
def _support_line() -> dict:
    line = marriage_trendline([_level(100.0, 1.2000), _level(500.0, 1.1100)],
                             "support")
    assert line is not None
    return line


# Two resistance SNRs forming at t=100 (y=1.1000) and t=500 (y=1.1900):
# the married line RISES 0.000225 per second.
def _resistance_line() -> dict:
    line = marriage_trendline(
        [_level(100.0, 1.1000, "resistance"), _level(500.0, 1.1900, "resistance")],
        "resistance")
    assert line is not None
    return line


class MarriageTrendlineTests(SimpleTestCase):
    def test_marriage_trendline_two_levels(self):
        line = _support_line()
        self.assertEqual(line["kind"], "support")
        self.assertEqual(line["levels_used"], 2)
        # Anchors are the SECOND anchor of each SNR, oldest first.
        self.assertAlmostEqual(line["t1"], 100.0, places=6)
        self.assertAlmostEqual(line["y1"], 1.2000, places=5)
        self.assertAlmostEqual(line["t2"], 500.0, places=6)
        self.assertAlmostEqual(line["y2"], 1.1100, places=5)
        self.assertLess(line["t1"], line["t2"])  # point #3 must be reachable
        self.assertAlmostEqual(line["slope"], (1.1100 - 1.2000) / 400.0, places=9)

        # The two most recent are used even when a third, older SNR is present.
        extra = marriage_trendline(
            [_level(100.0, 1.2000), _level(500.0, 1.1100), _level(900.0, 1.3000)],
            "support")
        self.assertAlmostEqual(extra["y2"], 1.1100, places=5)

        # Levels of the other kind are never married into this line.
        mixed = marriage_trendline(
            [_level(100.0, 1.2000), _level(500.0, 1.1100, kind="resistance")],
            "support")
        self.assertIsNone(mixed)

    def test_marriage_trendline_requires_two(self):
        self.assertIsNone(marriage_trendline([], "support"))
        self.assertIsNone(marriage_trendline([_level(500.0, 1.1100)], "support"))
        # A level dict without "kind" is accepted as being of the asked kind.
        bare = {"t1": 0.0, "y1": 1.2000, "t2": 100.0, "y2": 1.2000, "index": 1}
        self.assertIsNone(marriage_trendline([bare], "support"))

    def test_marriage_trendline_degenerate(self):
        # Two SNRs forming at the same instant have no slope.
        line = marriage_trendline([_level(100.0, 1.2000), _level(100.0, 1.2100)],
                                 "support")
        self.assertIsNone(line)


class LineValueTests(SimpleTestCase):
    def test_line_value_extrapolation(self):
        line = {"kind": "support", "t1": 0.0, "y1": 100.0, "t2": 10.0,
                "y2": 110.0, "slope": 1.0, "levels_used": 2}
        self.assertAlmostEqual(line_value(line, 5.0), 105.0, places=6)
        self.assertAlmostEqual(line_value(line, 25.0), 125.0, places=6)

        # The married support line: falls through both anchors, keeps falling.
        support = _support_line()
        self.assertAlmostEqual(line_value(support, 100.0), 1.2000, places=5)
        self.assertAlmostEqual(line_value(support, 500.0), 1.1100, places=5)
        self.assertAlmostEqual(line_value(support, 700.0), 1.0650, places=5)
        self.assertAlmostEqual(line_value(support, 900.0), 1.0200, places=5)

        resistance = _resistance_line()
        self.assertAlmostEqual(line_value(resistance, 700.0), 1.2350, places=5)


class EntryAtPoint3Tests(SimpleTestCase):
    def test_entry_at_point3_buy(self):
        line = _support_line()
        candles = _series([
            (1.2400, 1.2450, 1.1900, 1.2000, 100.0),   # point #1 anchor
            (1.1500, 1.1600, 1.1050, 1.1100, 500.0),   # point #2 anchor
            (1.1200, 1.1250, 1.1100, 1.1180, 600.0),   # holds, no touch
            (1.1100, 1.1150, 1.0640, 1.1050, 700.0),   # wick 1.0640 < 1.0650, rejects
            (1.1060, 1.1200, 1.1050, 1.1180, 800.0),   # entry candle
        ])
        entry = entry_at_point3(line, candles, 0)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["direction"], "buy")
        self.assertEqual(entry["touch_index"], 3)
        self.assertAlmostEqual(entry["open_time"], 700.0, places=6)
        self.assertAlmostEqual(entry["line_value"], 1.0650, places=5)
        self.assertAlmostEqual(entry["entry"], 1.1060, places=5)  # next open
        self.assertAlmostEqual(entry["sl"], 1.0640, places=5)     # touch low
        self.assertAlmostEqual(entry["tp"], 1.1900, places=4)     # 2x risk
        self.assertAlmostEqual(
            entry["tp"] - entry["entry"], 2.0 * (entry["entry"] - entry["sl"]),
            places=4)

    def test_entry_at_point3_sell(self):
        line = _resistance_line()
        candles = _series([
            (1.0800, 1.1100, 1.0750, 1.1000, 100.0),   # point #1 anchor
            (1.1600, 1.1950, 1.1550, 1.1900, 500.0),   # point #2 anchor
            (1.1900, 1.1950, 1.1850, 1.1880, 600.0),   # holds, no touch
            (1.2000, 1.2360, 1.1980, 1.2050, 700.0),   # wick 1.2360 > 1.2350, rejects
            (1.2040, 1.2060, 1.1990, 1.2020, 800.0),   # entry candle
        ])
        entry = entry_at_point3(line, candles, 0)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["direction"], "sell")
        self.assertEqual(entry["touch_index"], 3)
        self.assertAlmostEqual(entry["line_value"], 1.2350, places=5)
        self.assertAlmostEqual(entry["entry"], 1.2040, places=5)
        self.assertAlmostEqual(entry["sl"], 1.2360, places=5)     # touch high
        self.assertAlmostEqual(entry["tp"], 1.1400, places=4)     # 2x risk

    def test_no_entry_when_close_breaks_line(self):
        line = _support_line()
        candles = _series([
            (1.2400, 1.2450, 1.1900, 1.2000, 100.0),
            (1.1500, 1.1600, 1.1050, 1.1100, 500.0),
            (1.1000, 1.1100, 1.0800, 1.0850, 600.0),   # closes BELOW line 1.0875
            (1.1100, 1.1150, 1.0640, 1.1050, 700.0),   # would otherwise reject
            (1.1060, 1.1200, 1.1050, 1.1180, 800.0),
        ])
        self.assertIsNone(entry_at_point3(line, candles, 0))

    def test_no_entry_at_point2(self):
        line = _support_line()
        candles = _series([
            (1.2400, 1.2450, 1.1900, 1.2000, 100.0),
            (1.1300, 1.1350, 1.1050, 1.1280, 500.0),   # wick touches + rejects
            (1.1200, 1.1250, 1.1100, 1.1180, 600.0),   # line 1.0875, no touch
            (1.1150, 1.1180, 1.1000, 1.1100, 700.0),   # line 1.0650, no touch
            (1.1100, 1.1150, 1.0950, 1.1050, 800.0),   # line 1.0425, no touch
        ])
        # A textbook rejection, but at point #2 — rule 7 forbids the entry.
        self.assertIsNone(entry_at_point3(line, candles, 0))

    def test_no_entry_without_touch(self):
        line = _support_line()
        candles = _series([
            (1.2400, 1.2450, 1.1900, 1.2000, 100.0),
            (1.1500, 1.1600, 1.1050, 1.1100, 500.0),
            (1.1200, 1.1250, 1.1100, 1.1180, 600.0),   # line 1.0875, low 1.1100
            (1.1150, 1.1180, 1.1000, 1.1100, 700.0),   # line 1.0650, low 1.1000
            (1.1100, 1.1150, 1.0950, 1.1050, 800.0),   # line 1.0425, low 1.0950
        ])
        self.assertIsNone(entry_at_point3(line, candles, 0))
