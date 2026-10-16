"""
Signal strength tiers + order-block retest pair tests.

Covers:
- classify_strength() five-tier mapping (RECOMMENDED/STRONG/NORMAL/WEAK/RISKY)
- engine emits WEAK/RISKY for sub-60-confidence drafts instead of dropping them
- counter-trend fade SELL around an extended bullish OB (RISKY)
- confirmed continuation BUY after a bullish OB retest (RECOMMENDED)

Run: python manage.py test forex.tests.test_signal_tiers
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.order_blocks import OBDirection, OBState, OrderBlock, OrderBlockEngine
from forex.analysis.signal_engine import SignalEngine, SignalDraft
from forex.analysis.signal_lifecycle import Signal, SignalState


def _candle(open_: float, high: float, low: float, close: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close, "tick_volume": 100}


def _quiet_candles(n: int = 35, base: float = 100.0, rng: float = 0.2) -> list[dict]:
    """Range candles: small, flat, same area — used as the prior context."""
    return [_candle(base, base + rng, base - rng, base) for _ in range(n)]


def _ob_engine(high: float, low: float, quality: float, direction: OBDirection) -> OrderBlockEngine:
    eng = OrderBlockEngine()
    eng._obs.append(OrderBlock(
        direction=direction, state=OBState.VALID,
        high=high, low=low, quality_score=quality,
    ))
    return eng


# 2026-01-01 13:00 UTC — inside london/newyork, so EURUSD clears the session
# gate.  The gate fails closed on a missing entry_time, so a synthetic draft
# without one would be dropped before its strength tier was ever computed.
ENTRY_TIME = 1767272400.0


# ── 1. Five tiers ───────────────────────────────────────────────────────────
class StrengthTierTests(SimpleTestCase):
    def test_classify_strength_five_tiers(self):
        self.assertEqual(Signal.classify_strength(80.0), "RECOMMENDED")
        self.assertEqual(Signal.classify_strength(85.0), "RECOMMENDED")
        self.assertEqual(Signal.classify_strength(70.0), "STRONG")
        self.assertEqual(Signal.classify_strength(55.0), "NORMAL")
        self.assertEqual(Signal.classify_strength(40.0), "WEAK")
        self.assertEqual(Signal.classify_strength(30.0), "RISKY")
        self.assertEqual(Signal.classify_strength(0.0), "RISKY")


# ── 2. Low-confidence drafts are emitted (not dropped) ──────────────────────
class LowConfidenceEmissionTests(SimpleTestCase):
    def _engine_with_draft(self, direction: str, confidence: float) -> SignalEngine:
        eng = SignalEngine(cache=HotCache())
        eng._build_market_state = lambda symbol, direction, evidence: {}

        def build_draft(symbol):
            # SNR path: single-arg; the direction is fixed by the draft returned.
            draft = SignalDraft(direction=direction, entry=1.1000, stop_loss=1.0970, take_profit=1.1060,
                                entry_time=ENTRY_TIME)
            draft.confidence = confidence
            draft.evidence = {"analyzer": "snr", "htf_bias": 8.0}
            draft.explanation_parts = ["low-conviction test setup"]
            draft.risk_reward = 2.0
            draft.entry_zone_low = 1.0980
            draft.entry_zone_high = 1.1020
            return draft

        eng._build_snr_draft = build_draft
        eng._contradiction.analyze = lambda *a, **k: _FakeReport("TRADE")
        return eng

    def test_confidence_40_emits_weak_signal(self):
        eng = self._engine_with_draft("BUY", confidence=40.0)
        signals = eng.evaluate("EURUSD", "malysian")
        active = eng.get_active_signals("EURUSD")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].state, SignalState.CONFIRMED)
        self.assertEqual(active[0].strength, "WEAK")
        self.assertIn("entry 1.10000", active[0].explanation)

    def test_confidence_adjacent_to_floor_emits_weak(self):
        # 34 < 35 → RISKY boundary; must still EMIT (floor is 20, not 60).
        eng = self._engine_with_draft("SELL", confidence=34.0)
        signals = eng.evaluate("EURUSD", "malysian")
        active = eng.get_active_signals("EURUSD")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].strength, "RISKY")

    def test_confidence_below_floor_is_dropped(self):
        eng = self._engine_with_draft("BUY", confidence=10.0)
        signals = eng.evaluate("EURUSD", "malysian")
        self.assertEqual(eng.get_active_signals("EURUSD"), [])


class _FakeReport:
    def __init__(self, recommendation="TRADE", severity="NONE", net_confidence=80.0):
        self.recommendation = recommendation
        self.severity = severity
        self.net_confidence = net_confidence
        self.reasons: list[str] = []

    def to_dict(self) -> dict:
        return {"recommendation": self.recommendation, "severity": self.severity, "net_confidence": self.net_confidence, "reasons": []}


# ── 2b. _build_explanation: tier label + evidence fallback ──────────────────
class BuildExplanationTests(SimpleTestCase):
    def test_empty_parts_falls_back_to_evidence_and_tier(self):
        engine = SignalEngine(cache=HotCache())
        draft = SignalDraft(direction="BUY", entry=1.1000, stop_loss=1.0970, take_profit=1.1060)
        draft.confidence = 72.0
        draft.evidence = {"analyzer": "snr", "htf_bias": 15.0, "ob_quality": 12.0, "mss": 8.0}
        draft.explanation_parts = []
        draft.risk_reward = 2.0
        draft.entry_zone_low = 1.0980
        draft.entry_zone_high = 1.1020

        text = engine._build_explanation(draft)
        lines = text.splitlines()

        # Not a bare one-liner — even with empty explanation_parts.
        self.assertGreater(len(lines), 1)
        self.assertIn("Why?", text)
        # Tier label present (72 -> STRONG).
        self.assertIn("BUY", text)
        self.assertIn("STRONG", text)
        self.assertIn("confidence 72/100", text)
        # Evidence-derived lines rendered from draft.evidence.
        self.assertIn("HTF structure bias", text)
        self.assertIn("order block quality", text)
        self.assertIn("market-structure shift", text)
        self.assertIn("entry 1.10000", text)

    def test_parts_still_rendered_when_present(self):
        engine = SignalEngine(cache=HotCache())
        draft = SignalDraft(direction="SELL", entry=1.2000, stop_loss=1.2030, take_profit=1.1940)
        draft.confidence = 40.0
        draft.evidence = {"analyzer": "snr", "htf_bias": 10.0}
        draft.explanation_parts = ["4H bearish structure", "bearish MSS"]
        draft.risk_reward = 2.0

        text = engine._build_explanation(draft)
        self.assertIn("- 4H bearish structure", text)
        self.assertIn("- bearish MSS", text)
        self.assertIn("WEAK", text)  # tier label for 40/100


# ── 3. Order-block counter-trend fade (RISKY) ───────────────────────────────
class OBRetestFadeTests(SimpleTestCase):
    def test_extended_bullish_ob_emits_risky_fade_sell(self):
        # Bullish OB 100.0/99.5; price ran up to ~101 and prints a bearish rejection.
        ob_high, ob_low = 100.0, 99.5
        candles = _quiet_candles(34, base=99.0)
        candles[-1] = _candle(101.5, 101.9, 100.8, 100.9)  # solid bearish rejection
        cache = HotCache()
        cache.set_candles("AUDJPY", 3600, candles)
        eng = SignalEngine(
            cache=cache, ob_engines={3600: _ob_engine(ob_high, ob_low, 0.3, OBDirection.BULLISH)}
        )
        eng._build_market_state = lambda symbol, direction, evidence: {}
        eng._contradiction.analyze = lambda *a, **k: _FakeReport()

        signals = eng._evaluate_order_block_retest("AUDJPY")

        self.assertEqual(len(signals), 1, f"expected exactly the fade SELL, got {len(signals)}")
        sig = signals[0]
        self.assertEqual(sig.direction, "SELL")
        self.assertEqual(sig.strength, "RISKY")
        self.assertEqual(sig.state, SignalState.CONFIRMED)
        self.assertGreater(sig.take_profit, ob_high)  # fade exits just above the OB
        self.assertLess(sig.take_profit, candles[-1]["close"])  # …below current price
        self.assertIn("counter-trend fade", sig.explanation)
        self.assertIn("order block", sig.explanation)  # cites the OB zone
        self.assertGreater(len(sig.explanation.splitlines()), 1)

    def test_relaxed_extension_and_soft_rejection_still_emit_risky_fade_sell(self):
        # Extended only 0.25x the OB height + a WEAKER (soft) rejection candle still
        # fires the fade, but it stays RISKY-tiered with the detailed explanation.
        ob_high, ob_low = 100.0, 99.5
        candles = _quiet_candles(34, base=99.0)
        # Price only ~0.25x OB height above the top (100.125 threshold) and a
        # soft rejection: body ~37% of range, close parked in the bottom third.
        candles[-1] = _candle(100.9, 101.2, 100.4, 100.6)
        cache = HotCache()
        cache.set_candles("AUDJPY", 3600, candles)
        eng = SignalEngine(
            cache=cache, ob_engines={3600: _ob_engine(ob_high, ob_low, 0.3, OBDirection.BULLISH)}
        )
        eng._build_market_state = lambda symbol, direction, evidence: {}
        eng._contradiction.analyze = lambda *a, **k: _FakeReport()

        signals = eng._evaluate_order_block_retest("AUDJPY")

        self.assertEqual(len(signals), 1, f"expected exactly the fade SELL, got {len(signals)}")
        sig = signals[0]
        self.assertEqual(sig.direction, "SELL")
        self.assertEqual(sig.strength, "RISKY")
        self.assertEqual(sig.state, SignalState.CONFIRMED)
        self.assertGreater(sig.take_profit, ob_high)  # fade exits just above the OB
        self.assertLess(sig.take_profit, candles[-1]["close"])  # …below current price
        self.assertIn("counter-trend fade", sig.explanation)
        self.assertIn("order block", sig.explanation)  # cites the OB zone
        self.assertGreater(len(sig.explanation.splitlines()), 1)


# ── 4. Order-block confirmed continuation (RECOMMENDED) ─────────────────────
class OBRetestContinuationTests(SimpleTestCase):
    def test_retested_bullish_ob_with_displacement_emits_recommended_buy(self):
        ob_high, ob_low = 100.0, 99.5
        candles = _quiet_candles(34, base=99.0)
        candles[-1] = _candle(99.6, 100.2, 99.4, 100.1)  # touches the zone + bullish displacement
        cache = HotCache()
        cache.set_candles("AUDJPY", 3600, candles)
        eng = SignalEngine(
            cache=cache, ob_engines={3600: _ob_engine(ob_high, ob_low, 0.8, OBDirection.BULLISH)}
        )
        eng._build_market_state = lambda symbol, direction, evidence: {}
        eng._contradiction.analyze = lambda *a, **k: _FakeReport()

        signals = eng._evaluate_order_block_retest("AUDJPY")

        self.assertEqual(len(signals), 1, f"expected exactly the continuation BUY, got {len(signals)}")
        sig = signals[0]
        self.assertEqual(sig.direction, "BUY")
        self.assertEqual(sig.strength, "RECOMMENDED")
        self.assertEqual(sig.state, SignalState.CONFIRMED)
        self.assertLess(sig.stop_loss, ob_low)  # SL beyond the opposite side of the zone
        self.assertGreater(sig.take_profit, ob_high)  # TP a measured move above the zone
        self.assertIn("retested", sig.explanation)
        self.assertIn("order block", sig.explanation)  # cites the OB zone
        self.assertGreater(len(sig.explanation.splitlines()), 1)
        self.assertEqual(sig.confidence_evidence.get("ob_retest_pair"), "continuation")