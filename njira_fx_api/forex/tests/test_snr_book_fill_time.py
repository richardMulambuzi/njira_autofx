"""
Fill-time and fill-window contract for the book SNR analyzer
(``_analyze_snr_book``).

A 4H rejection validates on the CLOSE of its candle, so the fill belongs to the
NEXT 4H bar.  When the validation sits on the newest bar that bar has not opened
yet: the fill is its close_time.  Stamping the validation bar's own open_time
instead backdates the entry by a full 4H, which made every card born expired.

Run: python manage.py test forex.tests.test_snr_book_fill_time
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine

T = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
H4 = 14400
SYMBOL = "AUDJPY"
FILL = T + timedelta(seconds=H4)  # close of the validating bar == next bar's open
LEVEL = {"kind": "resistance", "index": 5, "t1": 1.3100, "t2": 1.3098,
         "y1": 1.3100, "y2": 1.3098, "fresh": True}
ROWS = [(1.3120, 1.3114), (1.3100, 1.3095), (1.3095, 1.3090), (1.3090, 1.3092),
        (1.3092, 1.3100), (1.3100, 1.3075), (1.3075, 1.3068), (1.3085, 1.3035)]


def _h4(extra=0):
    """Eight 4H bars plus ``extra`` later ones; bar 7 is the validation candle."""
    rows = ROWS + [(1.3035, 1.3050)] * extra
    return [
        {"symbol": SYMBOL, "timeframe": H4, "open": o, "high": 1.3094, "low": 1.3030,
         "close": c, "open_time": T - timedelta(seconds=(7 - i) * H4),
         "close_time": T - timedelta(seconds=(6 - i) * H4)}
        for i, (o, c) in enumerate(rows)
    ]


def _engine(extra=0, validation_offset=0):
    h4 = _h4(extra)
    cache = HotCache()
    cache.set_candles(SYMBOL, H4, h4)
    eng = SignalEngine(cache=cache)
    eng._snr_levels = lambda candles, kind: [dict(LEVEL, kind=kind)]
    eng._snr_book_validation = lambda candles, lv, d: {
        "candle_index": len(candles) - 1 - validation_offset,
        "clean": True,
        "price": 1.3094,
        "open_time": candles[len(candles) - 1 - validation_offset]["open_time"],
    }
    eng._calculate_proper_tp = lambda *a, **k: (1.3000, 0.0035, {})
    return eng


class BookFillTimeTests(SimpleTestCase):
    def test_fill_is_the_validating_bars_close_not_its_open(self):
        draft = _engine()._analyze_snr_book(SYMBOL)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.entry_time, FILL.timestamp())

    def test_setup_is_dropped_once_the_fill_window_closed(self):
        self.assertIsNone(_engine(extra=1, validation_offset=1)._analyze_snr_book(SYMBOL))
