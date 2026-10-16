"""
Malaysian SNR Step3-4 (15M box breakout) tests.

Acceptance dry-run: the Oct02 07:30-10:00 box (sup 4180.6 / res 4188.5) breaks
up — a 15M body close above 4188.5 in the same direction as the 4H BUY
rejection must yield a BUY breakout draft (entry = next 15M open, SL beyond
the breakout low + buffer, TP 2R fallback gated by the shared pipeline).

Run: python manage.py test forex.tests.test_snr_breakout
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.snr_breakout import (
    BREAKOUT_BOX_BARS,
    find_body_breakout,
    local_box,
)

T_REJ = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)  # 4H rejection opens
H4 = 14400
M15 = 900


def _cand(o: float, h: float, l: float, c: float, ot) -> dict:
    return {"symbol": "XAUUSD", "open_time": ot, "open": o, "high": h,
            "low": l, "close": c}


def _h4_buy() -> list[dict]:
    # t(9) = T_REJ = 10:00 UTC — the rejection open_time must equal T_REJ so that
    # the first M15 at/after T_REJ is val_index=10 (after the 10-bar box).
    # level["index"] = 7 → rejection at 7 + SNR_REJECTION_OFFSET(2) = idx9 = last.
    # That satisfies _snr_tradable_trendline: idx9 >= last - SNR_H4_RECENT_BARS(2) + 1 = 8.
    t = lambda i: T_REJ - timedelta(seconds=(9 - i) * H4)  # noqa: E731
    return [
        _cand(4175.0, 4176.0, 4174.0, 4176.0, t(0)),  # padding
        _cand(4176.0, 4177.0, 4175.0, 4177.0, t(1)),  # padding
        _cand(4176.5, 4177.5, 4176.0, 4177.0, t(2)),  # padding
        _cand(4177.0, 4178.0, 4176.5, 4177.5, t(3)),  # padding
        _cand(4177.5, 4178.5, 4177.0, 4178.0, t(4)),  # padding
        _cand(4177.0, 4178.0, 4176.5, 4177.5, t(5)),  # padding
        _cand(4180.5, 4181.0, 4179.5, 4180.0, t(6)),  # V c0: bearish close=4180.0
        _cand(4180.2, 4186.0, 4180.0, 4185.0, t(7)),  # V c1: bullish open=4180.2 → level idx=7
        _cand(4184.0, 4187.0, 4183.0, 4186.5, t(8)),  # MISS: bullish, low=4183 > line+tol≈4182.5
        _cand(4188.0, 4188.5, 4179.5, 4192.0, t(9)),  # rejection idx=7+2=9 (=T_REJ): pin bar, wick low=4179.5 touches ~4180 line
    ]


def _m15_box_breakout() -> list[dict]:
    closes = [4181.0, 4182.5, 4180.6, 4184.0, 4183.2,
              4185.1, 4186.0, 4184.8, 4187.3, 4188.5]  # sup 4180.6 / res 4188.5
    out = []
    for i, c in enumerate(closes):
        ot = datetime(2026, 10, 2, 7, 30, tzinfo=timezone.utc) + timedelta(seconds=i * M15)
        out.append(_cand(c - 0.2, c + 0.5, c - 0.5, c, ot))
    out.append(_cand(4188.0, 4191.0, 4187.5, 4190.2,  # breakout: body close > res
                     datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)))
    out.append(_cand(4190.3, 4191.0, 4189.5, 4190.8,  # entry candle (london session)
                     datetime(2026, 10, 2, 10, 15, tzinfo=timezone.utc)))
    return out


def _engine_with(symbol: str, h4: list[dict], m15: list[dict]) -> SignalEngine:
    cache = HotCache()
    for tf, bars in ((H4, h4), (M15, m15)):
        for b in bars:
            b = dict(b)
            b["timeframe"] = tf
            cache.add(b)
    return SignalEngine(cache=cache)


class LocalBoxTests(SimpleTestCase):
    def test_box_is_min_max_of_body_closes(self):
        sup, res = local_box([4181.0, 4180.6, 4188.5, 4184.0])
        self.assertAlmostEqual(sup, 4180.6, places=5)
        self.assertAlmostEqual(res, 4188.5, places=5)

    def test_box_uses_last_lookback_closes(self):
        sup, res = local_box([4100.0, 4181.0, 4180.6, 4188.5], lookback=3)
        self.assertAlmostEqual(sup, 4180.6, places=5)
        self.assertAlmostEqual(res, 4188.5, places=5)

    def test_box_needs_a_close(self):
        with self.assertRaises(ValueError):
            local_box([])


class BodyBreakoutTests(SimpleTestCase):
    def _bars(self, closes: list[float]) -> list[dict]:
        return [{"open_time": float(i * 900), "open": c - 0.1, "high": c + 0.2,
                 "low": c - 0.2, "close": c} for i, c in enumerate(closes)]

    def test_buy_breaks_on_body_close_above_res(self):
        bars = self._bars([4185.0, 4188.5, 4190.2])
        self.assertEqual(find_body_breakout(bars, 0, 4180.6, 4188.5, "BUY"), 2)

    def test_buy_ignores_wick_pierce(self):
        bars = self._bars([4185.0, 4186.0])
        bars[1]["high"] = 4195.0  # wick through res, body still inside
        self.assertIsNone(find_body_breakout(bars, 0, 4180.6, 4188.5, "BUY"))

    def test_sell_breaks_on_body_close_below_sup(self):
        bars = self._bars([4185.0, 4179.9])
        self.assertEqual(find_body_breakout(bars, 0, 4180.6, 4188.5, "SELL"), 1)

    def test_no_breakout_inside_box(self):
        bars = self._bars([4181.0, 4188.5, 4180.6])
        self.assertIsNone(find_body_breakout(bars, 0, 4180.6, 4188.5, "BUY"))
        self.assertIsNone(find_body_breakout(bars, 0, 4180.6, 4188.5, "SELL"))


class BreakoutAcceptanceTests(SimpleTestCase):
    """Oct02 07:30-10:00 box (sup 4180.6 / res 4188.5) breaks up → BUY draft."""

    def _candidates(self):
        eng = _engine_with("XAUUSD", _h4_buy(), _m15_box_breakout())
        with mock.patch.dict(os.environ, {"SNR_MALAYSIAN_ANALYZERS": "marriage,breakout"}):
            return eng, eng._malaysian_candidates("XAUUSD")

    def test_breakout_draft_fires_aligned_with_4h(self):
        eng, drafts = self._candidates()
        breakout = [d for d in drafts if (d.evidence or {}).get("analyzer") == "breakout"]
        self.assertEqual(len(breakout), 1)
        d = breakout[0]
        self.assertEqual(d.direction, "BUY")  # same direction as the 4H rejection
        box = d.evidence["breakout_box_levels"]
        self.assertAlmostEqual(box["sup"], 4180.6, places=4)
        self.assertAlmostEqual(box["res"], 4188.5, places=4)
        self.assertEqual(box["bars"], BREAKOUT_BOX_BARS)
        self.assertAlmostEqual(d.entry, 4190.3, places=4)  # next 15M open
        # SL beyond the breakout low + buffer, well clear of the entry.
        self.assertLess(d.stop_loss, 4187.5)
        self.assertGreater(d.entry - d.stop_loss, 0.1)
        # No fresh SNR overhead → 2R fallback, refined by the shared dynamic-RR
        # band (1.5–3.0) — always above the polisher MIN_RR 0.5 floor.
        self.assertGreaterEqual(d.risk_reward, 0.5)
        self.assertGreater(d.take_profit, d.entry)

    def test_breakout_survives_session_and_storyline_gates(self):
        eng, drafts = self._candidates()
        with mock.patch.dict(os.environ, {"SNR_MALAYSIAN_ANALYZERS": "marriage,breakout"}):
            survivors = eng._best_malaysian_draft("XAUUSD", drafts)
        self.assertTrue(any((d.evidence or {}).get("analyzer") == "breakout"
                            for d in survivors))

    def test_disabled_analyzer_is_dropped_via_funnel(self):
        eng = _engine_with("XAUUSD", _h4_buy(), _m15_box_breakout())
        with mock.patch.dict(os.environ, {"SNR_MALAYSIAN_ANALYZERS": "marriage"}):
            drafts = eng._malaysian_candidates("XAUUSD")
        self.assertFalse(any((d.evidence or {}).get("analyzer") == "breakout"
                             for d in drafts))
