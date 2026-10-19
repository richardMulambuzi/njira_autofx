"""
Fill-time contract for the book SNR analyzer (``_analyze_snr_book``).

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
M15 = 900
SYMBOL = "AUDJPY"
FILL = T + timedelta(seconds=H4)  # close of the validating bar == next bar's open
LEVEL = {"kind": "resistance", "index": 5, "t1": 1.3100, "t2": 1.3098,
         "y1": 1.3100, "y2": 1.3098, "fresh": True}


def _h4():
    """Eight 4H bars; the last one is the validation candle."""
    rows = [(1.3120, 1.3114), (1.3100, 1.3095), (1.3095, 1.3090), (1.3090, 1.3092),
            (1.3092, 1.3100), (1.3100, 1.3075), (1.3075, 1.3068), (1.3085, 1.3035)]
    return [
        {"symbol": SYMBOL, "timeframe": H4, "open": o, "high": 1.3094, "low": 1.3030,
         "close": c, "open_time": T - timedelta(seconds=(7 - i) * H4),
         "close_time": T - timedelta(seconds=(6 - i) * H4)}
        for i, (o, c) in enumerate(rows)
    ]


def _m15_tail(newest):
    return [
        {"symbol": SYMBOL, "timeframe": M15, "open": 1.307, "high": 1.3075,
         "low": 1.306, "close": 1.3065,
         "open_time": newest - timedelta(seconds=(3 - i) * M15),
         "close_time": newest - timedelta(seconds=(2 - i) * M15)}
        for i in range(4)
    ]


def _engine(m15_tail):
    cache = HotCache()
    cache.set_candles(SYMBOL, H4, _h4())
    cache.set_candles(SYMBOL, M15, m15_tail)
    eng = SignalEngine(cache=cache)
    eng._snr_levels = lambda h4, kind: [dict(LEVEL, kind=kind)]
    eng._snr_book_validation = lambda h4, lv, d: {
        "candle_index": len(h4) - 1, "clean": True, "price": 1.3094,
        "open_time": h4[-1]["open_time"],
    }
    eng._calculate_proper_tp = lambda *a, **k: (1.3000, 0.0035, {})
    return eng


class BookFillTimeTests(SimpleTestCase):
    def test_fill_is_the_validating_bars_close_not_its_open(self):
        draft = _engine(_m15_tail(FILL + timedelta(seconds=M15)))._analyze_snr_book(SYMBOL)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.entry_time, FILL.timestamp())

    def test_setup_is_dropped_once_the_fill_window_closed(self):
        stale_tail = _m15_tail(FILL + timedelta(seconds=H4))
        self.assertIsNone(_engine(stale_tail)._analyze_snr_book(SYMBOL))
