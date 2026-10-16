"""
SignalEngine integration tests for the four Malaysian SNR course modules.

Covers the wiring, not the modules (those are unit-tested in
test_engulfing / test_storyline / test_sessions / test_marriage):
  • ENGULFING — an H4 GAP SNR refined into a 15M engulfing emits a signal.
  • MARRIAGE  — two 4H support SNRs married into a trendline, traded at
    point #3, emit a signal.
  • STORYLINE — weekly bullish vs daily bearish drops a BUY.
  • SESSION   — an off-session entry is dropped.
  • DISPATCH  — the merged best setup reaches a real Signal.

``T = 03:00 UTC`` so entries land inside the tokyo session the engine allows
(AUDJPY home sessions); the session test moves the same fixture to 12:00 UTC.

Run: python manage.py test forex.tests.test_engine_integration
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine, SignalDraft
from forex.analysis.signal_lifecycle import SignalState

T = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)  # 03:00 UTC = tokyo
H4 = 14400
M15 = 900
H1 = 3600
D1 = 86400


def _cand(o: float, h: float, l: float, c: float, ot, tf: int) -> dict:
    return {"symbol": "AUDJPY", "open_time": ot, "open": o, "high": h,
            "low": l, "close": c, "timeframe": tf}


def _h4(specs, base=T) -> list[dict]:
    """(open, high, low, close) rows spaced one H4 bar apart from ``base``."""
    return [_cand(o, h, l, c, base + timedelta(seconds=i * H4), H4)
            for i, (o, h, l, c) in enumerate(specs)]


def _m15(specs, base=T) -> list[dict]:
    return [_cand(o, h, l, c, base + timedelta(seconds=i * M15), M15)
            for i, (o, h, l, c) in enumerate(specs)]


def _engine_with(candles: dict[int, list[dict]]) -> SignalEngine:
    cache = HotCache()
    for tf, bars in candles.items():
        for b in bars:
            b = dict(b)
            b["timeframe"] = tf
            cache.add(b)
    return SignalEngine(cache=cache)


# ── ENGULFING fixture ──────────────────────────────────────────────────────
# Every H4 candle is bullish except a small one, so the core SNR flow finds no
# rejection (its level forms at index 5/6 and N+3 falls past the last candle) and
# the engulfing analyzer is the only candidate — the dispatch test therefore
# proves a NON-SNR analyzer reaches the signal path.
#
# idx7 gaps up: open 1.3040 > idx6 high 1.3018 → the bullish GAP SNR.  Inside
# that H4 candle's window, m15[2] engulfs m15[1] (open below the prior close,
# close above the prior open, bigger body).
_H4_BASE = T - timedelta(seconds=7 * H4)  # so the last H4 candle opens on T
_H4_GAP = [
    (1.2980, 1.2990, 1.2975, 1.2985),
    (1.2986, 1.2995, 1.2980, 1.2992),
    (1.2993, 1.3000, 1.2985, 1.2996),
    (1.2990, 1.3005, 1.2982, 1.2994),
    (1.2995, 1.3010, 1.2990, 1.3005),
    (1.3006, 1.3012, 1.2998, 1.3000),   # small bearish body — no A pair with idx4
    (1.3001, 1.3018, 1.3000, 1.3015),
    (1.3040, 1.3060, 1.3030, 1.3050),   # GAP (open above idx6 high)
]
_M15_ENGULF = [
    (1.3120, 1.3125, 1.3115, 1.3120),   # idx0 A anchor 1 (bullish, fresh target)
    (1.3118, 1.3120, 1.3100, 1.3102),   # idx1 A anchor 2 (bearish) → fresh 1.3118 overhead
    (1.3040, 1.3045, 1.3035, 1.3038),   # idx2 bearish, body 0.0002
    (1.3039, 1.3042, 1.3036, 1.3040),   # idx3 bullish, body 0.0001 — too small, no engulf
    (1.3035, 1.3048, 1.3033, 1.3046),   # idx4 BULLISH ENGULFING of idx3
    (1.3047, 1.3052, 1.3045, 1.3050),   # idx5 entry
    (1.3051, 1.3056, 1.3048, 1.3054),
    (1.3055, 1.3060, 1.3050, 1.3058),
]


# ── MARRIAGE fixture ───────────────────────────────────────────────────────
# Exactly two V-shape (support) SNRs: anchors (1,2) and (5,6).  idx6 sits at
# T-1*H4, so the married line — anchored on the two SECOND anchors
# (T-5*H4, close 1.3000) → (T-1*H4, open 1.3022) — is already established when
# m15 starts.  m15[2] is the point-#3 extension: its wick tags the line and it
# closes back above it, so the entry is the open of m15[3].
_H4_MARRIAGE = [
    (1.2985, 1.2995, 1.2980, 1.2992),   # idx0
    (1.3020, 1.3021, 1.2975, 1.3000),   # idx1 V anchor 1 (bearish, close 1.3000)
    (1.3002, 1.3010, 1.3000, 1.3009),   # idx2 V anchor 2 (bullish, open 1.3002) → SNR #1
    (1.3010, 1.3015, 1.3005, 1.3012),   # idx3
    (1.3013, 1.3018, 1.3008, 1.3016),   # idx4
    (1.3022, 1.3023, 1.3000, 1.3020),   # idx5 V anchor 1 (bearish, close 1.3020)
    (1.3022, 1.3035, 1.3020, 1.3032),   # idx6 V anchor 2 (bullish, open 1.3022) → SNR #2
    (1.3034, 1.3045, 1.3030, 1.3040),   # idx7
]
_M15_MARRIAGE = [
    (1.3040, 1.3050, 1.3035, 1.3045),   # above the line, no touch
    (1.3046, 1.3055, 1.3040, 1.3050),   # above the line, no touch
    (1.3051, 1.3052, 1.3020, 1.3045),   # point #3: wick tags the line, closes above
    (1.3045, 1.3050, 1.3040, 1.3048),   # entry
    (1.3049, 1.3054, 1.3046, 1.3052),
    (1.3053, 1.3058, 1.3050, 1.3056),
    (1.3057, 1.3062, 1.3054, 1.3060),
    (1.3061, 1.3066, 1.3058, 1.3064),
]


class _TradeReport:
    """Minimal stand-in for ContradictionReport — always clears the polisher."""

    recommendation = "TRADE"
    severity = "NONE"
    net_confidence = 80.0
    reasons: list = []

    def to_dict(self) -> dict:
        return {"recommendation": self.recommendation, "severity": self.severity,
                "net_confidence": self.net_confidence, "reasons": []}


def _d1_change_of_storyline() -> list[dict]:
    """
    45 d1 candles: 25 rising bullish (weeks 0-4) then 20 falling bearish.

    The resampled weekly read is BULLISH (5 of 9 weekly candles up and the last
    weekly close, 1.2942, above the first, 1.2840) while the 20-bar daily
    lookback — all bearish and falling — is BEARISH.  That is exactly the
    change-of-storyline pairing the gate blocks a BUY on.
    """
    start = T - timedelta(seconds=44 * D1)
    out: list[dict] = []
    for i in range(45):
        if i < 25:
            o, c = 1.2800 + i * 0.0005, 1.2820 + i * 0.0005
        else:
            o, c = 1.3000 - (i - 25) * 0.0002, 1.2980 - (i - 25) * 0.0002
        out.append(_cand(o, max(o, c) + 0.0004, min(o, c) - 0.0004, c,
                         start + timedelta(seconds=i * D1), D1))
    return out


class EngulfingAnalyzerTests(SimpleTestCase):
    def test_engulfing_signal_emitted(self):
        eng = _engine_with({H4: _h4(_H4_GAP, base=_H4_BASE), M15: _m15(_M15_ENGULF)})
        draft = eng._analyze_engulfing(eng._cache.get("AUDJPY", H4),
                                       eng._cache.get("AUDJPY", M15))
        self.assertIsNotNone(draft)
        self.assertEqual(draft.direction, "BUY")
        self.assertEqual(draft.evidence["analyzer"], "engulfing")
        # Book: enter at the RETEST of the engulfing zone (idx4 zone 1.3033–1.3048,
        # retested/rejected by idx5) → entry = idx6 open; SL at the STRUCTURAL swing
        # low (1.3033, ~18 pips — clears M15 noise), TP at the next FRESH SNR.
        self.assertAlmostEqual(draft.entry, 1.3051, places=5)
        self.assertAlmostEqual(draft.stop_loss, 1.3033, places=5)
        self.assertAlmostEqual(draft.take_profit, 1.3105, places=5)
        self.assertEqual(draft.evidence["engulfing_candle"]["candle_index"], 4)
        self.assertEqual(draft.evidence["engulfing_retest"]["candle_index"], 5)
        self.assertEqual(draft.evidence["engulfing_h4_gap"]["shape"], "bullish")
        for key in ("engulfing_gap", "engulfing_body", "engulfing_risk_reward"):
            self.assertIsInstance(draft.evidence[key], float, key)
        # The H4 gap body bonus lifts it above the bare base confidence.
        self.assertGreater(draft.confidence, 50.0)

    def test_no_engulfing_without_h4_gap(self):
        # Same 15M data, no gap in the H4 series → nothing to refine.
        h4 = _h4(_H4_GAP, base=_H4_BASE)
        h4[7] = _cand(1.3015, 1.3060, 1.3010, 1.3050, T, H4)  # open == idx6 close → no open-close gap
        eng = _engine_with({H4: h4, M15: _m15(_M15_ENGULF)})
        self.assertIsNone(eng._analyze_engulfing(eng._cache.get("AUDJPY", H4),
                                                eng._cache.get("AUDJPY", M15)))


class MarriageAnalyzerTests(SimpleTestCase):
    def test_marriage_signal_emitted(self):
        eng = _engine_with({H4: _h4(_H4_MARRIAGE, base=_H4_BASE), M15: _m15(_M15_MARRIAGE)})
        h4 = eng._cache.get("AUDJPY", H4)
        m15 = eng._cache.get("AUDJPY", M15)

        levels = eng._snr_levels(h4, "support")
        self.assertEqual(len(levels), 2)
        self.assertLess(levels[0]["t2"], levels[1]["t2"])  # oldest first
        self.assertTrue(all(l["kind"] == "support" for l in levels))

        draft = eng._analyze_marriage(h4, m15)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.direction, "BUY")
        self.assertEqual(draft.evidence["analyzer"], "marriage")
        self.assertEqual(draft.evidence["marriage_line"]["kind"], "support")
        self.assertEqual(draft.evidence["marriage_line"]["levels_used"], 2)
        self.assertEqual(draft.evidence["marriage_point3"]["touch_index"], 2)
        # Entry = the open after the point-#3 touch; SL at the touch wick.  TP is
        # the NEXT fresh SNR level in the trade direction (book p.67), not 2x
        # risk: the A-shape resistance on m15[1]/m15[2] extends to 1.3052, above
        # the 1.3045 entry.
        self.assertAlmostEqual(draft.entry, 1.3045, places=5)
        self.assertAlmostEqual(draft.stop_loss, 1.3020, places=5)
        self.assertAlmostEqual(draft.take_profit, 1.3120, places=5)
        self.assertEqual(draft.evidence["snr_tp_source"], "fresh_snr")
        self.assertEqual(draft.evidence["snr_tp_level"]["kind"], "resistance")
        for key in ("marriage_trendline", "marriage_touch", "marriage_risk_reward"):
            self.assertIsInstance(draft.evidence[key], float, key)

    def test_no_marriage_with_a_single_level(self):
        # One V pair is not enough — a trendline needs at least two SNRs.
        h4 = _h4(_H4_MARRIAGE, base=_H4_BASE)
        h4[5] = _cand(1.3019, 1.3021, 1.3000, 1.3021,
                      _H4_BASE + timedelta(seconds=5 * H4), H4)  # bullish → no 2nd V
        eng = _engine_with({H4: h4, M15: _m15(_M15_MARRIAGE)})
        self.assertEqual(len(eng._snr_levels(h4, "support")), 1)
        self.assertIsNone(eng._analyze_marriage(h4, eng._cache.get("AUDJPY", M15)))



class StorylineGateTests(SimpleTestCase):
    def test_storyline_blocks_opposing_direction(self):
        d1 = _d1_change_of_storyline()
        eng = _engine_with({H4: _h4(_H4_GAP, base=_H4_BASE), M15: _m15(_M15_ENGULF),
                            D1: d1, H1: _h4(_H4_GAP, base=_H4_BASE)})
        storyline = eng._storyline("AUDJPY")
        self.assertIsNotNone(storyline)
        self.assertEqual(storyline["weekly"], "bullish")
        self.assertEqual(storyline["daily"], "bearish")

        # A BUY is blocked by the change-of-storyline rule; a SELL is not.
        # entry_time = 03:00 UTC (tokyo, AUDJPY's home session) so the session
        # gate passes and the storyline rule is what is under test here.
        buy = SignalDraft(direction="BUY", entry=1.0, stop_loss=0.9, take_profit=1.2,
                          entry_time=T.timestamp())
        sell = SignalDraft(direction="SELL", entry=1.0, stop_loss=1.1, take_profit=0.8,
                           entry_time=T.timestamp())
        self.assertFalse(eng._entry_allowed(buy, storyline, "AUDJPY"))
        self.assertTrue(eng._entry_allowed(sell, storyline, "AUDJPY"))

        # End to end: the analyzer's own BUY is dropped before it can emit.
        eng2 = _engine_with({H4: _h4(_H4_GAP, base=_H4_BASE), M15: _m15(_M15_ENGULF),
                             D1: d1, H1: _h4(_H4_GAP, base=_H4_BASE)})
        self.assertIsNotNone(eng2._analyze_engulfing(eng2._cache.get("AUDJPY", H4),
                                                    eng2._cache.get("AUDJPY", M15)))
        self.assertEqual(eng2._best_malaysian_draft("AUDJPY"), [])

    def test_storyline_skipped_without_d1_history(self):
        # No d1/h1 in the cache → the filter is skipped, not crashed.
        eng = _engine_with({H4: _h4(_H4_GAP, base=_H4_BASE), M15: _m15(_M15_ENGULF)})
        self.assertIsNone(eng._storyline("AUDJPY"))
        self.assertTrue(eng._best_malaysian_draft("AUDJPY"))


class SessionFilterTests(SimpleTestCase):
    def test_session_filter_drops_off_session_entry(self):
        # Identical fixture at 12:00 UTC (london/newyork only) and 03:00 UTC
        # (tokyo).  The analyzer produces the same setup in both cases — only
        # the session gate differs.  AUDJPY trades in tokyo + sydney, so the
        # 12:00 fill is off-session for it.
        noon = T.replace(hour=12)
        off = _engine_with({H4: _h4(_H4_GAP, base=noon - timedelta(seconds=7 * H4)), M15: _m15(_M15_ENGULF, base=noon)})
        raw = off._analyze_engulfing(off._cache.get("AUDJPY", H4),
                                    off._cache.get("AUDJPY", M15))
        self.assertIsNotNone(raw)                 # the setup itself is fine
        self.assertFalse(off._session_allowed(raw.entry_time, "AUDJPY"))
        self.assertEqual(off._best_malaysian_draft("AUDJPY"), [])

        on = _engine_with({H4: _h4(_H4_GAP, base=_H4_BASE), M15: _m15(_M15_ENGULF)})
        kept = on._best_malaysian_draft("AUDJPY")[0]
        self.assertIsNotNone(kept)
        self.assertTrue(on._session_allowed(kept.entry_time, "AUDJPY"))
        self.assertIn("tokyo", kept.evidence["session"])

    def test_session_allowed_by_symbol(self):
        # AUDJPY is an Asia instrument (tokyo/sydney); every other symbol takes
        # the prime-liquidity london/newyork overlap.  The same 12:00 UTC fill
        # is therefore off-session for AUDJPY and on-session for EURUSD — the
        # blocker: a flat tokyo/sydney list blocked the whole NY window.
        noon = T.replace(hour=12).timestamp()
        self.assertFalse(SignalEngine._session_allowed(noon, "AUDJPY"))
        self.assertTrue(SignalEngine._session_allowed(noon, "EURUSD"))
        self.assertTrue(SignalEngine._session_allowed(noon, "XAUUSD"))
        self.assertTrue(SignalEngine._session_allowed(noon, "GBPUSD"))   # default
        # Case-insensitive symbol match: "audjpy" == "AUDJPY".
        self.assertFalse(SignalEngine._session_allowed(noon, "audjpy"))
        self.assertTrue(SignalEngine._session_allowed(noon, "eurusd"))
        # 03:00 UTC is tokyo only — in-session for AUDJPY, off for EURUSD.
        self.assertTrue(SignalEngine._session_allowed(T.timestamp(), "AUDJPY"))
        self.assertFalse(SignalEngine._session_allowed(T.timestamp(), "EURUSD"))

    def test_session_fails_closed_on_none_timestamp(self):
        # No usable timestamp → no entry we can place in time → not tradable
        # (mirrors _snr_stale failing closed on the same condition).
        self.assertFalse(SignalEngine._session_allowed(None, "AUDJPY"))
        self.assertFalse(SignalEngine._session_allowed(None, "EURUSD"))


class SetupIdentityTests(SimpleTestCase):
    """
    Dedup identity for the SNR path (funnel finding: 267 dedup_merges vs 1 emit).

    The lifecycle key is (symbol, direction, strategy) and nothing anywhere
    retires a CONFIRMED signal, so on its own it caps the strategy at ONE signal
    per direction for the life of the process.  A setup is therefore identified
    by the structure anchors its own evidence carries: same anchors refresh in
    place, different anchors supersede and free the slot.
    """

    @staticmethod
    def _draft(gap_open_time=None) -> SignalDraft:
        evidence = {
            "analyzer": "engulfing", "engulfing_gap": 1.0, "engulfing_body": 1.0,
            "engulfing_risk_reward": 2.0,
        }
        if gap_open_time is not None:
            evidence["engulfing_h4_gap"] = {
                "kind": "gap", "shape": "bullish", "candle_index": 7,
                "open_time": gap_open_time,
            }
        d = SignalDraft(direction="BUY", entry=1.3047, stop_loss=1.3033,
                        take_profit=1.3075, entry_time=T.timestamp())
        d.confidence = 75.0
        d.risk_reward = 2.0
        d.evidence = evidence
        d.explanation_parts = ["H4 GAP SNR refined into 15M engulfing"]
        return d

    def _engine(self, current: list) -> SignalEngine:
        """Engine whose only SNR candidate is the engulfing draft in ``current[0]``.

        The six analyzers are independent, so every OTHER analyzer is stubbed to
        None — otherwise a real flip/QML/confluence draft on the same fixture
        would also emit and the assertions below (which target the engulfing
        signal by position) would be ambiguous.
        """
        eng = _engine_with({H4: _h4(_H4_GAP, base=_H4_BASE), M15: _m15(_M15_ENGULF)})
        eng._build_market_state = lambda symbol, direction, evidence: {}
        eng._contradiction.analyze = lambda *a, **k: _TradeReport()
        eng._build_snr_draft = lambda symbol: None
        eng._analyze_marriage = lambda h4, m15: None
        eng._analyze_flip = lambda symbol: None
        eng._analyze_qml = lambda symbol: None
        eng._confluence_candidate = lambda symbol: None
        eng._analyze_engulfing = lambda h4, m15: current[0]
        return eng

    def test_same_anchors_refresh_in_place(self):
        anchor = T.timestamp() - 7 * H4
        current = [self._draft(anchor)]
        eng = self._engine(current)

        first = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)
        current[0] = self._draft(anchor)          # same structure, next candle
        second = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)

        active = eng.get_active_signals("AUDJPY")
        self.assertEqual(len(active), 1, f"same setup must not create a second signal: {active}")
        self.assertEqual(active[0].signal_id, first[0].signal_id)
        self.assertEqual(active[0].signal_id, second[0].signal_id)
        self.assertEqual(active[0].state, SignalState.CONFIRMED)

    def test_new_anchors_supersede_and_free_the_slot(self):
        current = [self._draft(T.timestamp() - 7 * H4)]
        eng = self._engine(current)
        first = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)
        self.assertEqual(first[0].state, SignalState.CONFIRMED)

        # A fresh H4 gap anchor = a genuinely new setup.  Without the retire
        # step this is absorbed as a duplicate and the direction is locked out.
        current[0] = self._draft(T.timestamp() - 3 * H4)
        second = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)

        def engulfing(sigs):
            got = [s for s in sigs if s.strategy == "malysian:engulfing"]
            self.assertTrue(got, f"engulfing signal missing in {[s.strategy for s in sigs]}")
            return got[0]

        self.assertNotEqual(engulfing(second).signal_id, engulfing(first).signal_id)
        old = eng.get_lifecycle().get_by_id(engulfing(first).signal_id)
        self.assertEqual(old.state, SignalState.EXPIRED)
        active = eng.get_active_signals("AUDJPY")
        active_eng = [s for s in active if s.strategy == "malysian:engulfing"]
        self.assertEqual(len(active_eng), 1)
        self.assertEqual(active_eng[0].signal_id, engulfing(second).signal_id)
        self.assertEqual(active_eng[0].state, SignalState.CONFIRMED)

    def test_draft_without_anchors_keeps_one_setup(self):
        # Synthetic drafts (and the OB/MTF paths) carry no anchors: identity
        # degrades to the original symbol+direction+strategy key, so repeated
        # evaluation must still collapse onto a single signal.
        current = [self._draft()]
        eng = self._engine(current)
        first = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)
        second = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)
        active = eng.get_active_signals("AUDJPY")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].signal_id, first[0].signal_id)
        self.assertEqual(active[0].signal_id, second[0].signal_id)

    def test_fingerprint_uses_epochs_not_cache_indices(self):
        # A rolling cache index shifts every bar; using it as identity would
        # fake a brand-new setup on each close and defeat the dedup entirely.
        a = self._draft(T.timestamp())
        b = self._draft(T.timestamp())
        b.evidence["engulfing_h4_gap"]["candle_index"] = 999
        self.assertEqual(
            SignalEngine._setup_fingerprint(a.evidence),
            SignalEngine._setup_fingerprint(b.evidence),
        )
        # No anchors -> no fingerprint -> nothing is ever treated as superseded.
        self.assertEqual(SignalEngine._setup_fingerprint({}), "")
        self.assertEqual(SignalEngine._setup_fingerprint(None), "")

    def test_superseded_anchors_differ(self):
        self.assertNotEqual(
            SignalEngine._setup_fingerprint(self._draft(T.timestamp()).evidence),
            SignalEngine._setup_fingerprint(self._draft(T.timestamp() + H4).evidence),
        )


class DispatchTests(SimpleTestCase):
    def test_dispatch_returns_best_signal(self):
        eng = _engine_with({H4: _h4(_H4_GAP, base=_H4_BASE), M15: _m15(_M15_ENGULF)})
        signals = eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True)
        # The six analyzers are independent; find the engulfing one by its tag.
        eng_sig = [s for s in signals if s.strategy == "malysian:engulfing"]
        self.assertTrue(eng_sig, f"engulfing signal missing; got {[s.strategy for s in signals]}")
        sig = eng_sig[0]
        self.assertEqual(sig.direction, "BUY")
        self.assertEqual(sig.state, SignalState.CONFIRMED)
        self.assertEqual(sig.strategy, "malysian:engulfing")
        self.assertAlmostEqual(sig.entry, 1.3051, places=5)
        self.assertAlmostEqual(sig.stop_loss, 1.3033, places=5)  # structural swing low
        self.assertAlmostEqual(sig.take_profit, 1.3105, places=5)  # dynamic RR
        self.assertEqual(sig.confidence_evidence["analyzer"], "engulfing")
        self.assertIn("engulfing", sig.explanation.lower())
        # Every emitted signal carries a distinct per-analyzer strategy key.
        keys = [s.strategy for s in signals]
        self.assertEqual(len(keys), len(set(keys)), f"duplicate keys: {keys}")
        # The strategy report surfaces the highest-confidence merged setup.
        report = eng._analyze_snr("AUDJPY")
        self.assertTrue(report.applicable)
        self.assertEqual(report.direction, "BULLISH")
        self.assertAlmostEqual(report.candidate_setup["entry"], 1.3051, places=5)

    def test_dispatch_drops_off_session_signal(self):
        noon = T.replace(hour=12)
        eng = _engine_with({H4: _h4(_H4_GAP, base=noon - timedelta(seconds=7 * H4)), M15: _m15(_M15_ENGULF, base=noon)})
        self.assertEqual(
            eng.evaluate("AUDJPY", "malysian", closed_tf=900, skip_status=True), []
        )
        report = eng._analyze_snr("AUDJPY")
        self.assertFalse(report.applicable)
        self.assertTrue(any("gate" in m for m in report.missing_confirmations))
