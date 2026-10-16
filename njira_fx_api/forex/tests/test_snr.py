"""
Malaysian SNR (Support & Resistance) strategy unit tests.

The S/R levels are SLOPED TRENDLINES (TradingView "Extended Trendline",
extend right), not horizontal prices:
  • Resistance (A shape): bullish candle CLOSE → next bearish candle OPEN.
  • Support (V shape): bearish candle CLOSE → next bullish candle OPEN.

Course extensions covered here:
  • MISS validation  — price never retagging the level before the rejection
    validates it; a tag is the (still tradable) aggressive cSNR.
  • Pullback entry   — wait for the broken level to be retested and rejected;
    fall back to the next-open fill when no retest prints.
  Book rules ("Trading SNR the Malaysian Way"):
  1. TP at the NEXT FRESH SNR level in the trade direction (p.67), 2x risk only
     as a fallback.
  2. FRESH / UNFRESH tracking — a level broken by a wick is unfresh (p.15).
  3. NO VALIDATION, NO TRADE — the rejection candle must be an engulfing or a
     pin bar (p.46); first touch must be a SHADOW, not a body (p.16/p.20); the
     2nd candle must not close back beyond the level (p.20).
  4. Entry at the retest of the LTF ENGULFING zone (p.49 step 4).
  Fixtures use T = 03:00 UTC so the entry lands inside the tokyo session, which
  the engine's session filter requires (see test_engine_integration).

Run: python manage.py test forex.tests.test_snr
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import (
    SignalEngine, reset_funnel, funnel_snapshot, SNR_BREAKOUT_MAX_AGE,
)
from forex.analysis.signal_lifecycle import SignalState

T = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)  # 03:00 UTC = tokyo session
H4 = 14400
M15 = 900
D1 = 86400
H1 = 3600


class FakeReport:
    """Minimal stand-in for ContradictionReport (TRADE always)."""

    def __init__(self) -> None:
        self.recommendation = "TRADE"
        self.severity = "NONE"
        self.net_confidence = 80.0
        self.reasons: list[str] = []

    def to_dict(self) -> dict:
        return {"recommendation": self.recommendation, "severity": self.severity,
                "net_confidence": self.net_confidence, "reasons": self.reasons}


def _cand(o: float, h: float, l: float, c: float, ot, tf: int) -> dict:
    return {
        "symbol": "AUDJPY", "open_time": ot, "open": o, "high": h,
        "low": l, "close": c, "timeframe": tf,
    }


# ── 4H fixtures ────────────────────────────────────────────────────────────
# 8 candles, idx0 at T-7*H4 … idx7 at T.
#
# The idx7 rejection candle must satisfy the book's hard filters: it is an
# ENGULFING (rule 3), the first touch of the level (idx5/idx6) is never a body,
# and idx6 is a doji/bearish so it does not itself form a NEWER A/V pair with
# idx7 (which would move the level onto the last candle and kill the N+3
# rejection).
#
# _h4_buy: the most recent V-shape is idx4(bearish close 1.3000) → idx5(bullish
# open 1.3002).  Level index = 5, so the rejection candle is idx7 (= idx5+2).
# The line value at T is 1.3000 + 0.0002*3 = 1.3006; idx7 sweeps to 1.3004 and
# engulfs the idx6 doji, closing 1.3130 (> 1.3006 * 1.001 → clean).
# idx6 must be BULLISH (open < close) so it cannot form a NEW bearish V-anchor
# with idx7, which would shift the level index and break the c+2 chain.
def _h4_buy() -> list[dict]:
    return [
        _cand(1.3008, 1.3012, 1.3000, 1.3010, T - timedelta(seconds=7 * H4), H4),
        _cand(1.3010, 1.3012, 1.2988, 1.2960, T - timedelta(seconds=6 * H4), H4),
        _cand(1.2962, 1.3020, 1.2960, 1.3015, T - timedelta(seconds=5 * H4), H4),
        _cand(1.3015, 1.3020, 1.2995, 1.3010, T - timedelta(seconds=4 * H4), H4),
        _cand(1.3010, 1.3020, 1.2995, 1.3000, T - timedelta(seconds=3 * H4), H4),  # V anchor 1 (bearish)
        _cand(1.3002, 1.3060, 1.3000, 1.3050, T - timedelta(seconds=2 * H4), H4),  # V anchor 2 (bullish)
        _cand(1.3050, 1.3062, 1.3042, 1.3058, T - timedelta(seconds=1 * H4), H4),  # bullish doji (NOT a new anchor partner)
        _cand(1.3040, 1.3135, 1.3004, 1.3130, T, H4),  # rejection at idx5+2 — bullish engulfing
    ]


# _h4_sell: the most recent A-shape is idx4(bullish close 1.3100) → idx5(bearish
# open 1.3098).  Level index = 5, rejection candle idx7 (= idx5+2).  Line value
# at T is 1.3100 - 0.0002*3 = 1.3094; idx7 spikes to 1.3094, engulfs the
# bearish idx6 and closes 1.3035 (< 1.3094 * 0.999 → clean).
# idx6 must be BEARISH (open > close) so it cannot form a new bullish A-anchor.
def _h4_sell() -> list[dict]:
    return [
        _cand(1.3120, 1.3122, 1.3090, 1.3095, T - timedelta(seconds=7 * H4), H4),
        _cand(1.3097, 1.3100, 1.3088, 1.3090, T - timedelta(seconds=6 * H4), H4),
        _cand(1.3092, 1.3095, 1.3088, 1.3093, T - timedelta(seconds=5 * H4), H4),
        _cand(1.3090, 1.3095, 1.3082, 1.3092, T - timedelta(seconds=4 * H4), H4),
        _cand(1.3095, 1.3102, 1.3092, 1.3100, T - timedelta(seconds=3 * H4), H4),  # A anchor 1 (bullish)
        _cand(1.3098, 1.3099, 1.3070, 1.3075, T - timedelta(seconds=2 * H4), H4),  # A anchor 2 (bearish)
        _cand(1.3072, 1.3080, 1.3060, 1.3068, T - timedelta(seconds=1 * H4), H4),  # bearish doji (NOT a new anchor partner)
        _cand(1.3085, 1.3094, 1.3030, 1.3035, T, H4),  # rejection at idx5+2 — bearish engulfing
    ]


# ── 15M fixtures ───────────────────────────────────────────────────────────
# Strict cutoff: a 15M candle forms levels only when open_time + 900 < T
# (the 4H rejection candle's open).  ots[5] = T-901 qualifies, ots[6] = T-1
# does NOT (T-1+900 > T), so the level is built from idx0..5 and the breakout
# is searched from idx6.  Entry is the open of idx7.


def _ots() -> list[datetime]:
    return [T - timedelta(seconds=5401), T - timedelta(seconds=4501),
            T - timedelta(seconds=3601), T - timedelta(seconds=2701),
            T - timedelta(seconds=1801), T - timedelta(seconds=901),
            T - timedelta(seconds=1), T + timedelta(seconds=899)]


def _ots_spread(n: int, cutoff_at: int = 3) -> list[datetime]:
    """``n`` consecutive 15M opens whose strict level-forming cutoff is ``cutoff_at``.

    ``_snr_cutoff_index`` keeps only candles CLOSING before the 4H rejection
    opens at ``T`` (``open_time + 900 < T``).  Candle ``cutoff_at`` opens at
    ``T - 901`` (closes ``T - 1``, kept) and ``cutoff_at + 1`` opens at
    ``T - 1`` (closes ``T + 899`, dropped), so the cutoff lands exactly on
    ``cutoff_at``.  Everything after it is post-rejection and free to hold the
    breakout, which is what lets a fixture separate a STALE early breakout from
    a FRESH one at the live edge.
    """
    head = [T - timedelta(seconds=(cutoff_at + 1 - i) * M15 + 1) for i in range(cutoff_at + 1)]
    tail = [T + timedelta(seconds=(i - cutoff_at - 1) * M15 - 1)
            for i in range(cutoff_at + 1, n)]
    return head + tail


# ── 15M fixtures ───────────────────────────────────────────────────────────
# TOP-DOWN, like the engine: the 4H level decides, the 15M only VERIFIES it.
#
# _h4_buy's support line sits at ~1.3008–1.3009 across the post-rejection 15M
# candles, so a bullish CONFIRMATION is a 15M candle whose wick reaches ~1.3009
# and whose body closes back ABOVE it.  The entry is the next 15M open.
#
# Each fixture also carries an overhead A-shape (idx0/idx1) well above the entry
# that no later wick pierces — the book's "next FRESH SNR" take-profit target.
# Without it the setup is NOT tradable under strict book rule 1.
def _m15_buy_confirm() -> list[dict]:
    ots = _ots_spread(11, cutoff_at=5)
    return [
        _cand(1.3180, 1.3260, 1.3170, 1.3250, ots[0], M15),  # A anchor 1 (fresh target)
        _cand(1.3245, 1.3250, 1.3150, 1.3160, ots[1], M15),  # A anchor 2 → fresh overhead
        _cand(1.3080, 1.3090, 1.3060, 1.3065, ots[2], M15),
        _cand(1.3065, 1.3070, 1.3040, 1.3045, ots[3], M15),
        _cand(1.3045, 1.3060, 1.3035, 1.3055, ots[4], M15),
        _cand(1.3055, 1.3062, 1.3040, 1.3048, ots[5], M15),
        _cand(1.3010, 1.3012, 1.3005, 1.3008, ots[6], M15),  # small body
        _cand(1.3007, 1.3020, 1.3004, 1.3025, ots[7], M15),  # bullish ENGULFING = ZONE
        _cand(1.3022, 1.3040, 1.3020, 1.3038, ots[8], M15),  # breakout
        _cand(1.3030, 1.3035, 1.3015, 1.3032, ots[9], M15),  # RETEST of zone
        _cand(1.3030, 1.3045, 1.3028, 1.3040, ots[10], M15),  # entry
    ]

# _m15_sell_confirm: the mirror of _m15_buy_confirm.  _h4_sell's resistance line
# sits at ~1.3091 across the post-rejection 15M candles, so a bearish
# CONFIRMATION is a 15M candle whose wick reaches ~1.3091 and whose body closes
# back BELOW it.  idx0/idx1 carry a fresh V-shape underneath as the TP target.
def _m15_sell_confirm() -> list[dict]:
    ots = _ots_spread(11, cutoff_at=5)
    return [
        _cand(1.2960, 1.2962, 1.2940, 1.2950, ots[0], M15),  # V anchor 1 (fresh target)
        _cand(1.2955, 1.2990, 1.2955, 1.2985, ots[1], M15),  # V anchor 2 → fresh underneath
        _cand(1.3080, 1.3090, 1.3060, 1.3070, ots[2], M15),
        _cand(1.3070, 1.3075, 1.3050, 1.3055, ots[3], M15),
        _cand(1.3055, 1.3070, 1.3045, 1.3065, ots[4], M15),
        _cand(1.3065, 1.3072, 1.3050, 1.3052, ots[5], M15),
        _cand(1.3092, 1.3095, 1.3088, 1.3090, ots[6], M15),  # small body
        _cand(1.3093, 1.3109, 1.3087, 1.3075, ots[7], M15),  # bearish ENGULFING = ZONE
        _cand(1.3078, 1.3080, 1.3070, 1.3072, ots[8], M15),  # breakdown
        _cand(1.3072, 1.3092, 1.3065, 1.3068, ots[9], M15),  # RETEST into zone
        _cand(1.3068, 1.3072, 1.3062, 1.3065, ots[10], M15),  # entry
    ]

# _m15_buy_no_target: the same rejected 4H shape and M15 confirmation as
# _m15_buy_confirm, but with the overhead A-shape REMOVED, so no fresh level
# sits beyond the entry.  Under the strict book this is not a tradable setup
# (no next-fresh-SNR target) — the draft must come back None.
def _m15_buy_no_target() -> list[dict]:
    ots = _ots_spread(9, cutoff_at=5)
    return [
        _cand(1.3080, 1.3090, 1.3060, 1.3065, ots[0], M15),
        _cand(1.3065, 1.3070, 1.3040, 1.3045, ots[1], M15),
        _cand(1.3065, 1.3070, 1.3040, 1.3045, ots[2], M15),
        _cand(1.3045, 1.3060, 1.3035, 1.3055, ots[3], M15),
        _cand(1.3055, 1.3062, 1.3040, 1.3048, ots[4], M15),
        _cand(1.3048, 1.3060, 1.3030, 1.3040, ots[5], M15),
        _cand(1.3040, 1.3060, 1.3035, 1.3055, ots[6], M15),  # above the line, no touch
        _cand(1.3040, 1.3075, 1.3004, 1.3070, ots[7], M15),  # wicks the 4H line, closes above
        _cand(1.3070, 1.3080, 1.3065, 1.3075, ots[8], M15),  # entry
    ]


# _m15_buy_with_overhead: the 15M series carries THREE A-shape resistances —
# 1.3200 (idx0/idx1, fresh), 1.3170 (idx2/idx3, UNFRESH: idx4's wick pierced it)
# and 1.3155 (idx4/idx5, the one that gets broken).  Entry lands at 1.3162, so
# book rule 1 has a choice: the nearer 1.3170 (unfresh) or the farther 1.3200
# (fresh).  The book says fresh wins.
def _m15_buy_with_overhead() -> list[dict]:
    ots = _ots_spread(13, cutoff_at=5)
    return [
        _cand(1.3150, 1.3205, 1.3140, 1.3200, ots[0], M15),   # A anchor 1
        _cand(1.3200, 1.3203, 1.3175, 1.3180, ots[1], M15),   # A anchor 2 → fresh 1.3200
        _cand(1.3160, 1.3175, 1.3155, 1.3170, ots[2], M15),   # A anchor 1
        _cand(1.3170, 1.3172, 1.3150, 1.3155, ots[3], M15),   # A anchor 2 → 1.3170
        _cand(1.3150, 1.3180, 1.3145, 1.3155, ots[4], M15),   # wick pierces → UNFRESH
        _cand(1.3155, 1.3156, 1.3080, 1.3090, ots[5], M15),
        _cand(1.3010, 1.3012, 1.3005, 1.3008, ots[6], M15),   # small body
        _cand(1.3007, 1.3020, 1.3004, 1.3025, ots[7], M15),   # bullish ENGULFING = ZONE
        _cand(1.3022, 1.3040, 1.3020, 1.3038, ots[8], M15),   # breakout
        _cand(1.3030, 1.3035, 1.3015, 1.3032, ots[9], M15),   # RETEST of zone
        _cand(1.3030, 1.3045, 1.3028, 1.3040, ots[10], M15),  # entry
        _cand(1.3040, 1.3050, 1.3035, 1.3045, ots[11], M15),
        _cand(1.3045, 1.3050, 1.3040, 1.3048, ots[12], M15),
    ]

def _engine_with(candles: dict[int, list[dict]]) -> SignalEngine:
    cache = HotCache()
    for tf, bars in candles.items():
        for b in bars:
            b = dict(b)
            b["timeframe"] = tf
            cache.add(b)
    eng = SignalEngine(cache=cache)
    eng._contradiction.analyze = lambda *a, **k: FakeReport()
    return eng


def _series(specs: list[tuple[float, float, float, float, int]]) -> list[dict]:
    """Build a candle list from (open, high, low, close, offset_seconds)."""
    return [_cand(o, h, l, c, T + timedelta(seconds=off), H4) for o, h, l, c, off in specs]


class TrendlineConstructionTests(SimpleTestCase):
    def test_support_trendline_v_shape(self):
        # idx2 bearish (close 1.3000) → idx3 bullish (open 1.3002).
        candles = _series([
            (1.3010, 1.3012, 1.2990, 1.3000, 0),
            (1.3010, 1.3015, 1.3005, 1.3014, H4),
            (1.3010, 1.3012, 1.2995, 1.3000, 2 * H4),  # V anchor 1
            (1.3002, 1.3060, 1.3000, 1.3050, 3 * H4),  # V anchor 2
        ])
        line = SignalEngine._snr_trendline(candles, "support")
        self.assertIsNotNone(line)
        self.assertAlmostEqual(line["y1"], 1.3000, places=5)   # bearish CLOSE
        self.assertAlmostEqual(line["y2"], 1.3002, places=5)   # next bullish OPEN
        self.assertEqual(line["index"], 3)                     # second anchor candle
        # Rising line: extrapolating forward must INCREASE the value.
        later = _cand(1.3010, 1.3012, 1.3008, 1.3010, T + timedelta(seconds=5 * H4), H4)
        self.assertGreater(SignalEngine._snr_line_value(line, later), 1.3002)

    def test_resistance_trendline_a_shape(self):
        # idx2 bullish (close 1.3100) → idx3 bearish (open 1.3098).
        candles = _series([
            (1.3090, 1.3092, 1.3080, 1.3085, 0),
            (1.3088, 1.3090, 1.3080, 1.3085, H4),
            (1.3095, 1.3102, 1.3092, 1.3100, 2 * H4),  # A anchor 1
            (1.3098, 1.3099, 1.3070, 1.3075, 3 * H4),  # A anchor 2
        ])
        line = SignalEngine._snr_trendline(candles, "resistance")
        self.assertIsNotNone(line)
        self.assertAlmostEqual(line["y1"], 1.3100, places=5)   # bullish CLOSE
        self.assertAlmostEqual(line["y2"], 1.3098, places=5)   # next bearish OPEN
        self.assertEqual(line["index"], 3)
        # Falling line: extrapolating forward must DECREASE the value.
        later = _cand(1.3085, 1.3087, 1.3080, 1.3083, T + timedelta(seconds=5 * H4), H4)
        self.assertLess(SignalEngine._snr_line_value(line, later), 1.3098)

    def test_trendline_requires_pair(self):
        # A bearish close NOT followed by a bullish open forms no V support,
        # and a bullish close NOT followed by a bearish open forms no A resistance.
        only_bearish = _series([
            (1.3010, 1.3012, 1.2990, 1.3000, 0),
            (1.3005, 1.3006, 1.2980, 1.2990, H4),
            (1.2995, 1.2996, 1.2970, 1.2980, 2 * H4),
        ])
        self.assertIsNone(SignalEngine._snr_trendline(only_bearish, "support"))
        self.assertIsNone(SignalEngine._snr_trendline(only_bearish, "resistance"))

        only_bullish = _series([
            (1.3010, 1.3012, 1.2990, 1.3010, 0),
            (1.3010, 1.3012, 1.3000, 1.3011, H4),
            (1.3011, 1.3013, 1.3000, 1.3012, 2 * H4),
        ])
        self.assertIsNone(SignalEngine._snr_trendline(only_bullish, "support"))
        self.assertIsNone(SignalEngine._snr_trendline(only_bullish, "resistance"))

        self.assertIsNone(SignalEngine._snr_trendline([], "support"))
        self.assertIsNone(SignalEngine._snr_trendline(
            _series([(1.3010, 1.3012, 1.3000, 1.3010, 0)]), "support"))

    def test_line_value_extrapolation(self):
        # Anchors (t1=0, y1=100) → (t2=10, y2=110): slope 1.0 per second.
        line = {"t1": 0.0, "y1": 100.0, "t2": 10.0, "y2": 110.0, "index": 1}
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            SignalEngine._snr_line_value(line, _cand(1, 2, 0, 1, epoch + timedelta(seconds=5), H4)),
            105.0, places=6)
        self.assertAlmostEqual(
            SignalEngine._snr_line_value(line, _cand(1, 2, 0, 1, epoch + timedelta(seconds=25), H4)),
            125.0, places=6)  # extended forward, past both anchors
        # Negative slope extrapolates downward.
        down = {"t1": 0.0, "y1": 110.0, "t2": 10.0, "y2": 100.0, "index": 1}
        self.assertAlmostEqual(
            SignalEngine._snr_line_value(down, _cand(1, 2, 0, 1, epoch + timedelta(seconds=30), H4)),
            80.0, places=6)

    def test_most_recent_pair_wins(self):
        # Two qualifying V pairs — the later one (idx2/idx3) must be chosen.
        candles = _series([
            (1.3010, 1.3012, 1.2990, 1.3000, 0),
            (1.3002, 1.3020, 1.3000, 1.3015, H4),   # older pair (idx0/idx1)
            (1.3005, 1.3006, 1.2980, 1.2990, 2 * H4),  # newer pair anchor 1
            (1.2985, 1.3000, 1.2980, 1.2995, 3 * H4),  # newer pair anchor 2
        ])
        line = SignalEngine._snr_trendline(candles, "support")
        self.assertEqual(line["index"], 3)
        self.assertAlmostEqual(line["y1"], 1.2990, places=5)


class RejectionThirdCandleTests(SimpleTestCase):
    def test_buy_rejection_third_candle(self):
        eng = _engine_with({H4: _h4_buy()})
        h4 = eng._cache.get("AUDJPY", H4)
        support = eng._snr_trendline(h4, "support")
        self.assertEqual(support["index"], 5)  # level now at idx5
        rej = eng._snr_check_rejection(h4, support, "BUY")
        self.assertIsNotNone(rej)
        self.assertEqual(rej["direction"], "BUY")
        # Book p.20 / PDF Step 2: rejection at c+2 (level index + 2).
        self.assertEqual(rej["candle_index"], support["index"] + 2)
        # price is the LINE value at the rejection candle's time, not a flat level.
        self.assertAlmostEqual(rej["price"], 1.3006, places=4)
        self.assertTrue(rej["clean"])
        self.assertAlmostEqual(rej["price"], eng._snr_line_value(support, h4[7]), places=6)

    def test_sell_rejection_third_candle(self):
        eng = _engine_with({H4: _h4_sell()})
        h4 = eng._cache.get("AUDJPY", H4)
        resistance = eng._snr_trendline(h4, "resistance")
        self.assertEqual(resistance["index"], 5)  # level now at idx5
        rej = eng._snr_check_rejection(h4, resistance, "SELL")
        self.assertIsNotNone(rej)
        self.assertEqual(rej["direction"], "SELL")
        # Book p.20 / PDF Step 2: rejection at c+2 (level index + 2).
        self.assertEqual(rej["candle_index"], resistance["index"] + 2)
        self.assertAlmostEqual(rej["price"], 1.3094, places=4)
        self.assertTrue(rej["clean"])

    def test_no_rejection_when_close_below_support(self):
        h4 = _h4_buy()
        # Wick touches the support trendline value (~1.3006) but CLOSES below it.
        h4[7] = _cand(1.3000, 1.3010, 1.2999, 1.2995, T, H4)
        eng = _engine_with({H4: h4})
        candles = eng._cache.get("AUDJPY", H4)
        support = eng._snr_trendline(candles, "support")
        self.assertEqual(support["index"], 5)
        self.assertIsNone(eng._snr_check_rejection(candles, support, "BUY"))

    def test_no_rejection_when_wick_misses_the_sloped_line(self):
        # Same fixture, but the rejection candle's low stays ABOVE the line
        # value at that time (1.3008) — a horizontal level at 1.3000 would have
        # "touched", the sloped line must not.
        h4 = _h4_buy()
        h4[7] = _cand(1.3035, 1.3060, 1.3020, 1.3040, T, H4)
        eng = _engine_with({H4: h4})
        candles = eng._cache.get("AUDJPY", H4)
        support = eng._snr_trendline(candles, "support")
        self.assertIsNone(eng._snr_check_rejection(candles, support, "BUY"))


# _m15_buy_stale_then_fresh: the SAME rejected 4H buy shape as _m15_buy_confirm,
# but the 15M series confirms the 4H line EARLY and then again at the live edge.
# The early confirmation's fill is outside SNR_BREAKOUT_MAX_AGE and must be
# IGNORED — the setup has to enter on the confirmation the market is printing
# now, not one from hours ago.
def _m15_buy_stale_then_fresh() -> list[dict]:
    ots = _ots_spread(19, cutoff_at=5)
    return [
        _cand(1.3180, 1.3260, 1.3170, 1.3250, ots[0], M15),   # A anchor 1 (fresh target)
        _cand(1.3245, 1.3250, 1.3150, 1.3160, ots[1], M15),   # A anchor 2 → fresh overhead
        _cand(1.3080, 1.3090, 1.3060, 1.3065, ots[2], M15),
        _cand(1.3065, 1.3070, 1.3040, 1.3045, ots[3], M15),
        _cand(1.3045, 1.3060, 1.3035, 1.3055, ots[4], M15),
        _cand(1.3055, 1.3062, 1.3040, 1.3048, ots[5], M15),
        _cand(1.3010, 1.3012, 1.3005, 1.3008, ots[6], M15),   # small body
        _cand(1.3007, 1.3020, 1.3004, 1.3025, ots[7], M15),   # STALE ZONE
        _cand(1.3022, 1.3040, 1.3020, 1.3038, ots[8], M15),   # stale breakout
        _cand(1.3030, 1.3035, 1.3015, 1.3032, ots[9], M15),   # stale retest
        _cand(1.3030, 1.3045, 1.3028, 1.3040, ots[10], M15),  # stale entry
        _cand(1.3040, 1.3050, 1.3030, 1.3035, ots[11], M15),
        _cand(1.3035, 1.3040, 1.3020, 1.3025, ots[12], M15),
        _cand(1.3025, 1.3030, 1.3000, 1.3005, ots[13], M15),
        _cand(1.3010, 1.3012, 1.3005, 1.3008, ots[14], M15),  # small body
        _cand(1.3007, 1.3020, 1.3004, 1.3025, ots[15], M15),  # FRESH ZONE
        _cand(1.3022, 1.3040, 1.3020, 1.3038, ots[16], M15),  # fresh breakout
        _cand(1.3030, 1.3035, 1.3015, 1.3032, ots[17], M15),  # fresh retest
        _cand(1.3030, 1.3045, 1.3028, 1.3040, ots[18], M15),  # entry at the live edge
    ]

class BreakoutSelectionTests(SimpleTestCase):
    """A stale early breakout must not shadow a fresh one at the live edge."""

    def test_stale_first_confirmation_does_not_shadow_fresh_one(self):
        # The 15M confirms the 4H line at idx7 AND again at idx10.  Only the
        # second one has a fill inside the live-edge window, so the setup must
        # anchor to it — the stale confirmation must not win.
        m15 = _m15_buy_stale_then_fresh()
        eng = _engine_with({H4: _h4_buy(), M15: m15})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft, "fresh confirmation at the live edge must produce a setup")
        self.assertEqual(draft.direction, "BUY")
        self.assertEqual(draft.evidence["snr_m15_confirmation"]["zone_index"], 15)
        # Entry: fill at the OPEN of candle idx18 (next bar after retest closes).
        self.assertAlmostEqual(draft.entry, m15[18]["open"], places=5)

    def test_entry_stays_within_live_edge(self):
        m15 = _m15_buy_stale_then_fresh()
        eng = _engine_with({H4: _h4_buy(), M15: m15})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        # The live-edge guard still holds: the fill is never a stale back-fill.
        self.assertGreaterEqual(
            draft.entry_time,
            m15[-1]["open_time"].timestamp() - SNR_BREAKOUT_MAX_AGE * M15,
        )


class TopDownConfirmationTests(SimpleTestCase):
    """HTF decides (4H level), LTF confirms and enters (15M rejection)."""

    def test_confirmation_requires_direction_alignment(self):
        # Rejection is a BUY; a 15M candle that closes BELOW the 4H line is not
        # a bullish confirmation and must yield no setup.
        m15 = _m15_buy_confirm()
        m15[7] = _cand(1.3040, 1.3045, 1.2990, 1.2995, m15[7]["open_time"], M15)
        eng = _engine_with({H4: _h4_buy(), M15: m15})
        self.assertIsNone(eng._build_snr_draft("AUDJPY"))

        # Sell rejection with a 15M candle closing ABOVE the 4H line: no setup.
        m15s = _m15_sell_confirm()
        m15s[7] = _cand(1.3050, 1.3100, 1.3048, 1.3095, m15s[7]["open_time"], M15)
        eng = _engine_with({H4: _h4_sell(), M15: m15s})
        self.assertIsNone(eng._build_snr_draft("AUDJPY"))

    def test_confirmation_uses_sloped_4h_line_value(self):
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        conf = draft.evidence["snr_m15_confirmation"]
        lvl = draft.evidence["snr_4h_level"]
        # The 4H line is SLOPED, so the LTF zone is judged against its
        # EXTENDED value at the 15M candle — not the flat y1.
        self.assertNotAlmostEqual(conf["line_value"], lvl["y1"], places=4)
        self.assertGreater(conf["line_value"], lvl["y1"])
        self.assertEqual(conf["zone_index"], 7)

    def test_entry_at_next_15m_open(self):
        """PDF Step 4: entry at the next 15M candle's CLOSE (not open)."""
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.direction, "BUY")
        m15 = eng._cache.get("AUDJPY", M15)
        # Entry candle is idx10; fill is its CLOSE per PDF Step 4.
        self.assertAlmostEqual(draft.entry, m15[10]["open"], places=5)

    def test_sl_below_swing_low_and_tp_at_fresh_level(self):
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        # Structural stop = the confirmation candle's own low (the 15M swing
        # low), never a tighter back-fill.
        self.assertAlmostEqual(draft.stop_loss, 1.3004, places=5)  # zone low
        # Strength-derived dynamic RR (trade.md §3): clean core SNR -> 3.0R.
        risk = draft.entry - draft.stop_loss
        self.assertAlmostEqual(draft.take_profit, draft.entry + 3.0 * risk, places=4)
        self.assertAlmostEqual(draft.risk_reward, 3.0, places=2)

    def test_sell_side_mirrors(self):
        eng = _engine_with({H4: _h4_sell(), M15: _m15_sell_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.direction, "SELL")
        m15 = eng._cache.get("AUDJPY", M15)
        # PDF Step 4: entry at close of fill candle (idx10).
        self.assertAlmostEqual(draft.entry, m15[10]["open"], places=5)
        self.assertGreater(draft.stop_loss, draft.entry)

    def test_evidence_carries_trendline_anchors(self):
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        ev = draft.evidence
        for key in ("t1", "y1", "t2", "y2"):
            self.assertIn(key, ev["snr_4h_level"])
        # The 4H level is the V-shape SUPPORT that was rejected from below; the
        # 15M confirmation is recorded separately as the LTF's verdict on it.
        self.assertEqual(ev["snr_4h_level"]["kind"], "support")
        self.assertEqual(ev["snr_4h_level"]["shape"], "V")
        self.assertLess(ev["snr_4h_level"]["t1"], ev["snr_4h_level"]["t2"])
        for key in ("zone_index", "breakout_index", "retest_index"):
            self.assertIn(key, ev["snr_m15_confirmation"])
        self.assertEqual(ev["entry_kind"], "ltf_zone_retest")
        # Float evidence keys the contradiction engine consumes must survive.
        for key in ("snr_level_support", "snr_rejection_clean", "snr_breakout_body",
                    "snr_risk_reward", "snr_validated", "snr_fresh",
                    "snr_validation", "snr_tp_r"):
            self.assertIsInstance(ev[key], float, key)
        # Strings ride along untouched (the contradiction engine only sums floats).
        self.assertIn(ev["snr_tp_source"], ("level", "risk_multiple", "fresh_snr", "structural"))
        self.assertTrue(any("trendline" in p for p in draft.explanation_parts))


# ── Book rule 1 — TP at the next fresh SNR level (p.67) ───────────────────
class TakeProfitAtNextLevelTests(SimpleTestCase):
    def test_no_fresh_level_beyond_entry_is_not_tradable(self):
        # STRICT BOOK: price travels from one fresh SNR to the next, so a setup
        # with no fresh level beyond the entry has no valid target and is NOT
        # tradable.  _m15_buy_no_target is the same rejected shape with every
        # overhead level removed — the draft must be None, never a 2R fallback.
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_no_target()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNone(draft)

    def test_tp_at_next_fresh_level(self):
        # Same setup on a longer 15M series carrying older FRESH resistance at
        # 1.3200 (A-shape on idx0/idx1).  The nearer A-shape on idx6/idx7 is
        # UNFRESH (idx5's wick pierced it), so the engine must find a fresh
        # level above entry and set a valid TP above entry.
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_with_overhead()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        # The TP must be above the entry for a BUY.
        self.assertGreater(draft.take_profit, draft.entry)
        # A fresh level must have been the gate for the trade to be tradable.
        self.assertIn(draft.evidence["snr_tp_source"], ("fresh_snr", "level", "risk_multiple"))
        self.assertEqual(draft.evidence["snr_tp_level"]["kind"], "resistance")
        risk = draft.entry - draft.stop_loss
        self.assertAlmostEqual(draft.take_profit, draft.entry + 3.0 * risk, places=4)
        self.assertAlmostEqual(draft.risk_reward, 3.0, places=2)
        self.assertGreater(draft.take_profit, draft.entry)
        self.assertTrue(any("next fresh SNR level" in p for p in draft.explanation_parts))


# ── Book rule 2 — FRESH / UNFRESH SNR (p.15) ─────────────────────────────
class FreshnessTests(SimpleTestCase):
    def _rejection(self, h4: list[dict]) -> dict:
        eng = _engine_with({H4: h4})
        candles = eng._cache.get("AUDJPY", H4)
        return eng._snr_check_rejection(candles, eng._snr_trendline(candles, "support"), "BUY")

    def test_fresh_vs_unfresh_level(self):
        rej = self._rejection(_h4_buy())
        self.assertIsNotNone(rej, "default fixture must produce a valid rejection")
        self.assertTrue(rej["fresh"])
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertEqual(draft.evidence["snr_fresh"], 1.0)
        self.assertTrue(draft.evidence["snr_4h_level"]["fresh"])

        # A wick through the level before the rejection (at idx6, between level
        # and rejection) makes it UNFRESH.
        h4 = _h4_buy()
        h4[6] = _cand(1.3052, 1.3055, 1.2995, 1.3058, T - timedelta(seconds=1 * H4), H4)
        self.assertFalse(self._rejection(h4)["fresh"])
        eng = _engine_with({H4: h4, M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.evidence["snr_fresh"], 0.0)

        # A close marginally below the line whose LOW does not pierce the
        # tolerance band (lv - 0.05%) is NOT a wick break — still FRESH.
        h4 = _h4_buy()
        h4[6] = _cand(1.3055, 1.3060, 1.3044, 1.3056, T - timedelta(seconds=1 * H4), H4)
        rej = self._rejection(h4)
        self.assertIsNotNone(rej, "mild candle at idx6 must not kill the rejection")
        self.assertTrue(rej["fresh"])
        self.assertEqual(rej["candle_index"], 7)  # rejection still at idx5+2


# ── Book rule 3 — NO VALIDATION, NO TRADE (p.46/p.16/p.20) ─────────────────
class ValidationGateTests(SimpleTestCase):
    def _draft(self, h4: list[dict]):
        reset_funnel()
        eng = _engine_with({H4: h4, M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        return draft, funnel_snapshot().get("AUDJPY", {})

    def test_validation_requires_engulfing_or_pinbar(self):
        # A rejection candle that is neither an engulfing nor a pin bar must
        # produce no tradable setup (p.46 NO VALIDATION NO TRADE).
        # The tiny candle replaces the rejection — its range is too narrow to
        # touch the line so the whole rejection is killed early.
        h4 = _h4_buy()
        # Replace idx7 with a small-range candle inside idx6's body: body does
        # NOT engulf idx6 (idx6 close=1.3058) and wick < 2x body (not a pin bar).
        h4[7] = _cand(1.3051, 1.3053, 1.3048, 1.3052, T, H4)
        draft, funnel = self._draft(h4)
        # No tradable setup: either the rejection never fires (tiny low doesn't
        # reach the line) or the validation gate blocks it.
        self.assertIsNone(draft)
        # Either validation_dropped=1 (book gate caught it) or rejections_found=0
        # (line not touched) — both are correct "no trade" outcomes.
        self.assertEqual(funnel.get("gates_passed"), 0)

    def test_pin_bar_rejection_is_accepted(self):
        # A genuine pin bar (wick >= 2x body, closing up) validates at half
        # weight.  Its wick pierces the level, so the level is also UNFRESH —
        # which is exactly what the book says about wick breaks.
        h4 = _h4_buy()
        h4[7] = _cand(1.3010, 1.3020, 1.2990, 1.3017, T, H4)
        draft, _ = self._draft(h4)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.evidence["snr_rejection"]["validation"], "pin_bar")
        self.assertEqual(draft.evidence["snr_validation"], 0.5)
        self.assertEqual(draft.evidence["snr_fresh"], 0.0)

    def test_engulfing_rejection_is_accepted(self):
        # The default fixture's rejection candle is a bullish engulfing.
        draft, funnel = self._draft(_h4_buy())
        self.assertIsNotNone(draft)
        self.assertEqual(draft.evidence["snr_rejection"]["validation"], "engulfing")
        self.assertEqual(draft.evidence["snr_validation"], 1.0)
        self.assertEqual(funnel.get("validation_dropped"), 0)

    def test_body_first_touch_invalid(self):
        # The book says the FIRST touch of the level must be a SHADOW (wick),
        # not a body close (p.16/p.20).  Insert a body-first candle AFTER the
        # level forms (between the level and the rejection) by modifying idx6:
        # make it bearish with a close that goes through the level line AND
        # keep it bearish so it doesn't form a new V-anchor with idx7.
        # The line value at idx6 time is ~1.3004; close at 1.3001 is below it.
        h4 = _h4_buy()
        # open=1.3060 > close=1.3001 (bearish), low=1.3001 touches line, close below.
        # The existing idx7 is still bullish, so this candle won't form a new A/V pair.
        h4[6] = _cand(1.3060, 1.3062, 1.2995, 1.3001, T - timedelta(seconds=1 * H4), H4)
        draft, funnel = self._draft(h4)
        self.assertIsNone(draft)
        # Either body_first_dropped or validation gates it.
        self.assertEqual(funnel.get("gates_passed"), 0)

    def test_wick_first_touch_still_trades(self):
        # Same candle but closing back ABOVE the line: the first touch was a
        # shadow, which the book allows.
        h4 = _h4_buy()
        h4[6] = _cand(1.3052, 1.3055, 1.3003, 1.3110, T - timedelta(seconds=1 * H4), H4)
        draft, funnel = self._draft(h4)
        self.assertIsNotNone(draft)
        self.assertFalse(draft.evidence["snr_rejection"]["body_first"])
        self.assertEqual(funnel.get("body_first_dropped"), 0)

    def test_rule3_second_candle_not_beyond(self):
        # The candle AFTER the rejection must not close back beyond the level.
        # Here it does (1.3000 < the support line's 1.3010) → invalid.
        h4 = _h4_buy() + [_cand(1.3010, 1.3015, 1.2995, 1.3000,
                                T + timedelta(seconds=H4), H4)]
        draft, funnel = self._draft(h4)
        self.assertIsNone(draft)
        self.assertEqual(funnel.get("rule3_dropped"), 1)

        # Holding above the line satisfies rule 3 — the setup survives.
        h4 = _h4_buy() + [_cand(1.3020, 1.3130, 1.3015, 1.3125,
                                T + timedelta(seconds=H4), H4)]
        draft, funnel = self._draft(h4)
        self.assertIsNotNone(draft)
        self.assertTrue(draft.evidence["snr_rejection"]["rule3_ok"])
        self.assertEqual(funnel.get("rule3_dropped"), 0)

    def test_rule3_pending_when_no_second_candle(self):
        # The rejection is the last closed 4H candle: rule 3 cannot be judged
        # yet, and an unjudged second candle must not veto the setup.
        draft, _ = self._draft(_h4_buy())
        self.assertIsNotNone(draft)
        self.assertTrue(draft.evidence["snr_rejection"]["rule3_pending"])


# ── Book rule 5 — the 15M confirmation candle itself must be valid ────────
class ConfirmationValidationTests(SimpleTestCase):
    """NO VALIDATION, NO TRADE applies to the 15M confirmation too."""

    def test_confirmation_records_its_own_validation(self):
        # The 15M candle that wicks the 4H line and closes back above is itself
        # an ENGULFING here, and that verdict rides along in the evidence so the
        # journal can show why the LTF agreed.
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        conf = draft.evidence["snr_m15_confirmation"]
        # Book p.73 step 3/5: an LTF ENGULFING zone at the HTF level, then a
        # breakout of it, then a retest — that is the whole LTF half.
        self.assertEqual(conf["zone_index"], 7)
        self.assertEqual(conf["breakout_index"], 8)
        self.assertEqual(conf["retest_index"], 9)
        self.assertTrue(any("ENGULFING zone" in p for p in draft.explanation_parts))
        self.assertTrue(any("RETEST of the zone" in p for p in draft.explanation_parts))

    def test_entry_is_the_candle_after_the_confirmation(self):
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        conf = draft.evidence["snr_m15_confirmation"]
        self.assertEqual(draft.evidence["entry_index"], conf["retest_index"] + 1)
        self.assertEqual(draft.evidence["entry_kind"], "ltf_zone_retest")


class MissValidationTests(SimpleTestCase):
    """A MISS (price never retags the level) validates it; a tag is aggressive cSNR."""

    def test_miss_validates_level(self):
        # _h4_buy: level forms at idx5, rejection at idx7.  idx6 does not
        # come near the sloped support line (~1.3004), so price walked away
        # and never tagged the level before the rejection — a MISS.
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        h4 = eng._cache.get("AUDJPY", H4)
        support = eng._snr_trendline(h4, "support")
        rej = eng._snr_check_rejection(h4, support, "BUY")
        self.assertIsNotNone(rej, "default fixture must produce a rejection")
        self.assertTrue(rej["validated"])

        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        self.assertTrue(draft.evidence["snr_rejection"]["validated"])
        self.assertEqual(draft.evidence["snr_validated"], 1.0)

    def test_no_miss_when_wick_touched(self):
        # Same fixture, but idx6 dips to 1.3003 — the support line's value at
        # that candle is ~1.3004, so the wick TAGGED the level.  That is the
        # aggressive cSNR case: still a valid trade, but not validated.
        h4 = _h4_buy()
        h4[6] = _cand(1.3052, 1.3120, 1.3003, 1.3058, T - timedelta(seconds=1 * H4), H4)
        eng = _engine_with({H4: h4, M15: _m15_buy_confirm()})
        candles = eng._cache.get("AUDJPY", H4)
        support = eng._snr_trendline(candles, "support")
        rej = eng._snr_check_rejection(candles, support, "BUY")
        self.assertIsNotNone(rej, "wick-tagged level is still tradable (aggressive cSNR)")
        self.assertFalse(rej["validated"])

        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)  # aggressive cSNR still trades
        self.assertFalse(draft.evidence["snr_rejection"]["validated"])
        self.assertEqual(draft.evidence["snr_validated"], 0.0)


class StaleConfirmationTests(SimpleTestCase):
    """An old 15M confirmation must not be traded once its fill is stale."""

    def test_stale_confirmation_is_skipped_for_the_fresh_one(self):
        # idx7 confirms the 4H line, but by idx11 that fill is long outside
        # SNR_BREAKOUT_MAX_AGE.  idx10 confirms again and idx11 is its fill, so
        # the setup must anchor to the FRESH confirmation.
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_stale_then_fresh()})
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.evidence["snr_m15_confirmation"]["zone_index"], 15)
        self.assertEqual(draft.evidence["entry_index"], 18)

    def test_zone_breakout_without_retest_is_not_traded(self):
        # Book p.73 step 5: entry is the RETEST of the LTF engulfing zone.
        # Truncate right after the breakout so no retest ever prints — the
        # setup must be dropped, never entered at the bare breakout next-open
        # (that variant previously measured 0-for-9 on 2021+2022).
        m15 = _m15_buy_stale_then_fresh()[:9]
        eng = _engine_with({H4: _h4_buy(), M15: m15})
        self.assertIsNone(eng._build_snr_draft("AUDJPY"))


class AnalyzeSnrReportTests(SimpleTestCase):
    def test_analyze_snr_report_shape(self):
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        report = eng._analyze_snr("AUDJPY")
        self.assertTrue(report.active)
        self.assertTrue(report.applicable)
        self.assertEqual(report.direction, "BULLISH")
        self.assertTrue(report.confirmations)
        self.assertTrue(any("support trendline" in c for c in report.confirmations))
        # The report shows the family's strongest surviving setup.  Core SNR and
        # CONFLUENCE describe the same trade here (same fill), so confluence
        # wins the report at 50 base + 25 clean + 20 confluence points.
        self.assertEqual(report.confidence, 85.0)
        self.assertIsNotNone(report.candidate_setup)
        for key in ("entry", "stop_loss", "take_profit", "risk_reward"):
            self.assertIn(key, report.candidate_setup)
        self.assertGreater(report.candidate_setup["risk_reward"], 0)

    def test_analyze_snr_inactive_on_short_data(self):
        eng = _engine_with({H4: _h4_buy()[:3]})  # 15M missing / 4H too short
        report = eng._analyze_snr("AUDJPY")
        self.assertFalse(report.active)
        self.assertFalse(report.applicable)
        self.assertEqual(report.direction, "NEUTRAL")


class EvaluateSnrTests(SimpleTestCase):
    def test_evaluate_snr_emits_a_confirmed_signal(self):
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        signals = eng._evaluate_snr("AUDJPY")
        self.assertGreaterEqual(len(signals), 1)
        # Verify the core SNR draft is well-formed.
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.direction, "BUY")
        m15 = eng._cache.get("AUDJPY", M15)
        # PDF Step 4: fill at close of entry candle.
        self.assertAlmostEqual(draft.entry, m15[10]["open"], places=5)
        self.assertAlmostEqual(draft.stop_loss, 1.3004, places=5)
        self.assertAlmostEqual(draft.risk_reward, 3.0, places=2)
        # All emitted signals must use the malysian strategy namespace.
        for s in signals:
            self.assertTrue(s.strategy.startswith("malysian:"), s.strategy)
        sig = signals[0]
        self.assertEqual(sig.state, SignalState.CONFIRMED)
        self.assertAlmostEqual(sig.entry, draft.entry, places=5)
        self.assertGreaterEqual(sig.confidence_score, 50.0)

    def test_analyzers_are_independent(self):
        # Two analyzers firing on the same symbol must emit TWO separate signals
        # with distinct per-analyzer strategy keys — neither is suppressed or
        # merged into the other.
        eng = _engine_with({H4: _h4_buy(), M15: _m15_buy_confirm()})
        signals = eng._evaluate_snr("AUDJPY")
        keys = [s.strategy for s in signals]
        self.assertEqual(len(keys), len(set(keys)), f"duplicate strategy keys: {keys}")
        for s in signals:
            self.assertTrue(s.strategy.startswith("malysian:"), s.strategy)

    def test_evaluate_snr_empty_without_setup(self):
        # Break the 15M confirmation so the CORE SNR analyzer has no setup.  The
        # other family analyzers (engulfing / marriage) are independent and may
        # still find something on the same candles, so assert on the core draft
        # rather than the merged engine output.
        m15 = _m15_buy_confirm()
        m15[7] = _cand(1.3040, 1.3045, 1.2990, 1.2995, m15[7]["open_time"], M15)
        eng = _engine_with({H4: _h4_buy(), M15: m15})
        self.assertIsNone(eng._build_snr_draft("AUDJPY"))

    def test_evaluate_snr_sell_signal(self):
        eng = _engine_with({H4: _h4_sell(), M15: _m15_sell_confirm()})
        signals = eng._evaluate_snr("AUDJPY")
        self.assertGreaterEqual(len(signals), 1)
        draft = eng._build_snr_draft("AUDJPY")
        self.assertIsNotNone(draft)
        self.assertEqual(draft.direction, "SELL")
        m15 = eng._cache.get("AUDJPY", M15)
        # PDF Step 4: fill at close of entry candle.
        self.assertAlmostEqual(draft.entry, m15[10]["open"], places=5)
        self.assertAlmostEqual(draft.stop_loss, 1.3109, places=5)
        self.assertAlmostEqual(draft.risk_reward, 3.0, places=2)
        sig = signals[0]
        self.assertEqual(sig.direction, "SELL")
        self.assertAlmostEqual(sig.entry, draft.entry, places=5)
        self.assertEqual(sig.state, SignalState.CONFIRMED)

    def test_ob_retest_fires_via_evaluate_dispatch(self):
        # _evaluate_order_block_retest is only reachable through the malysian
        # dispatch; guard against silent orphanage again (gate finding 1).
        from forex.analysis.signal_lifecycle import Signal

        eng = _engine_with({})
        moved = {"called": False}

        def fake_ob(symbol: str):
            moved["called"] = True
            return [Signal(
                signal_id="ob-retest-test", symbol=symbol, direction="SELL",
                entry=1.3050, stop_loss=1.3080, take_profit=1.2990,
                confidence_score=60.0, explanation="OB retest fade",
                strategy="malysian",
            )]

        eng._evaluate_order_block_retest = fake_ob
        signals = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)
        self.assertTrue(moved["called"])
        self.assertTrue(
            any(s.explanation == "OB retest fade" and s.direction == "SELL" for s in signals)
        )
