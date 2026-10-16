"""
Market Context State tests (improve.md §17, Phase 4).

Run: python manage.py test forex.tests.test_market_context
"""
from __future__ import annotations

from datetime import datetime, timezone

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.context import MarketContextEngine, MarketContextState
from forex.analysis.order_blocks import OBDirection
from forex.analysis.signal_engine import SignalEngine


# ── (a) defaults for an unknown symbol ────────────────────────────────────
class MarketContextStateDefaultsTests(SimpleTestCase):
    def test_unknown_symbol_all_fields_present(self):
        eng = MarketContextEngine()
        st = eng.get("UNKNOWN")
        self.assertIsInstance(st, MarketContextState)
        self.assertEqual(st.symbol, "UNKNOWN")
        # Every §17 field must exist with a safe default (no KeyError).
        for field in (
            "symbol", "current_price", "bid", "ask", "spread", "timestamp",
            "data_freshness", "daily_high", "daily_low", "previous_day_high",
            "previous_day_low", "session", "session_high", "session_low",
            "trend", "trend_strength", "market_structure", "structure_state",
            "liquidity_levels", "liquidity_sweeps", "order_blocks",
            "major_order_blocks", "fvgs", "imbalances", "volume_profile",
            "poc", "vah", "val", "hvn", "lvn", "multi_timeframe_context",
            "active_strategy_states", "candidate_setups", "signal_state",
            "signal_strength", "signal_confidence", "signal_invalidation",
        ):
            self.assertTrue(hasattr(st, field), f"missing field: {field}")
        self.assertEqual(st.fvgs, [])
        self.assertEqual(st.multi_timeframe_context, {})
        self.assertEqual(st.to_dict()["symbol"], "UNKNOWN")

    def test_invalidate_drops_state(self):
        eng = MarketContextEngine()
        eng.update("EURUSD")
        self.assertIsNotNone(eng.get("EURUSD"))
        eng.invalidate("EURUSD")
        # After invalidate, get returns a fresh default (not the old state).
        self.assertEqual(eng.get("EURUSD").symbol, "EURUSD")


# ── (b) update() populates from mocked engines ────────────────────────────
class MarketContextUpdateTests(SimpleTestCase):
    def test_update_populates_structure_spread_volatility(self):
        class FakeBias:
            def __init__(self, v):
                self.value = v

        class FakeRegime:
            def __init__(self, v):
                self.value = v

        class FakeStruct:
            def get_bias(self, tf):
                return FakeBias("BULLISH")

            def get_regime(self, tf):
                return FakeRegime("TRENDING")

        class FakeMicro:
            def get_metrics(self, s):
                class M:
                    spread_pips = 1.5
                    spread_regime = "EXPANDED"
                return M()

        class FakeVol:
            def get_metrics(self, s):
                class V:
                    regime = "HIGH"
                return V()

        eng = MarketContextEngine(
            structure_engines={900: FakeStruct(), 14400: FakeStruct()},
            microstructure_engine=FakeMicro(),
            volatility_engine=FakeVol(),
        )
        eng.update("EURUSD")
        st = eng.get("EURUSD")
        self.assertEqual(st.multi_timeframe_context[900], "bullish")
        self.assertEqual(st.multi_timeframe_context[14400], "bullish")
        self.assertEqual(st.spread_regime, "WIDE")  # EXPANDED -> WIDE
        self.assertEqual(st.volatility_regime, "HIGH")
        self.assertEqual(st.trend, "bullish")
        self.assertEqual(st.market_structure, "trending")
        self.assertGreater(st.trend_strength, 0.0)


# ── (c) SignalEngine with context produces same vocabulary as legacy ──────
class SignalEngineContextParityTests(SimpleTestCase):
    def _make_engines(self):
        class FakeBias:
            def __init__(self, v):
                self.value = v

        class FakeRegime:
            def __init__(self, v):
                self.value = v

        class FakeStruct:
            def get_bias(self, tf):
                return FakeBias("BULLISH")

            def get_regime(self, tf):
                return FakeRegime("TRENDING")

        class FakeFVG:
            direction = "BULLISH"

            class _S:
                value = "FORMED"
            status = _S()

            def to_dict(self):
                return {"direction": self.direction, "status": self.status.value}

        class FakeFVGEngine:
            def get_active(self):
                return [FakeFVG()]

        class FakeOB:
            direction = OBDirection.BULLISH

            class _S:
                value = "VALID"
            state = _S()

            def to_dict(self):
                return {"direction": self.direction.value, "state": self.state.value}

        class FakeOBEngine:
            def get_active(self, limit=10):
                return [FakeOB()]

        class FakePool:
            pool_type = type("T", (), {"value": "EQUAL_HIGH"})()
            price = 1.1
            state = type("S", (), {"value": "POOL"})()

        class FakeLiqEngine:
            def get_pools(self, state=None, limit=20):
                return []

        class FakeMicro:
            def get_metrics(self, s):
                class M:
                    spread_pips = 1.0
                    spread_regime = "EXPANDED"
                return M()

        class FakeVol:
            def get_metrics(self, s):
                class V:
                    regime = "HIGH"
                return V()

        struct = {900: FakeStruct(), 14400: FakeStruct()}
        fvg = {900: FakeFVGEngine()}
        ob = {900: FakeOBEngine()}
        liq = {900: FakeLiqEngine()}
        micro = FakeMicro()
        vol = FakeVol()
        return struct, fvg, ob, liq, micro, vol

    def test_parity_old_vs_new(self):
        struct, fvg, ob, liq, micro, vol = self._make_engines()
        evidence: dict = {}

        # Legacy path (no context engine).
        sig_old = SignalEngine(
            cache=HotCache(),
            structure_engines=struct,
            liquidity_engines=liq,
            ob_engines=ob,
            fvg_engines=fvg,
            microstructure_engine=micro,
            volatility_engine=vol,
            session_engine=None,
            spread_engine=None,
            volume_profile_engine=None,
        )
        old = sig_old._build_market_state("EURUSD", "BUY", evidence)

        # New path: shared context built from the SAME engines.
        ctx = MarketContextEngine(
            structure_engines=struct,
            liquidity_engines=liq,
            ob_engines=ob,
            fvg_engines=fvg,
            microstructure_engine=micro,
            volatility_engine=vol,
            session_engine=None,
            cache=HotCache(),
        )
        ctx.update("EURUSD")
        sig_new = SignalEngine(
            cache=HotCache(),
            structure_engines=struct,
            liquidity_engines=liq,
            ob_engines=ob,
            fvg_engines=fvg,
            microstructure_engine=micro,
            volatility_engine=vol,
            session_engine=None,
            spread_engine=None,
            volume_profile_engine=None,
            context_engine=ctx,
        )
        new = sig_new._build_market_state("EURUSD", "BUY", evidence)

        # Legacy fields must match exactly between the old (no-context) builder
        # and the new (context-driven) builder. The new builder intentionally
        # adds 4 structural fields (improve.md §41) the legacy one lacks, so a
        # naive `old == new` is no longer valid.
        for key in old:
            self.assertEqual(old[key], new[key], msg=f"legacy field {key!r} mismatch")
        # The 4 new structural fields must be present in the new output and
        # pulled from the shared context.
        for extra in ("price", "daily_high", "daily_low", "liquidity_levels"):
            self.assertIn(extra, new)
        state = ctx.get("EURUSD")
        self.assertEqual(new["price"], state.current_price)
        self.assertEqual(new["daily_high"], state.daily_high)
        self.assertEqual(new["daily_low"], state.daily_low)
        self.assertEqual(new["liquidity_levels"], state.liquidity_levels)
        # Spot-check the vocabulary is intact.
        self.assertEqual(new["htf_bias"], "bullish")
        self.assertEqual(new["spread_regime"], "WIDE")
        self.assertEqual(new["volatility_regime"], "HIGH")
        self.assertTrue(new["has_fvg"])
        self.assertEqual(new["fvg_status"], "FORMED")
        self.assertTrue(new["has_ob"])
        self.assertEqual(new["ob_status"], "VALID")
        self.assertEqual(new["session"], "")


# ── (d) ticks_engine integration: _on_candle_close updates context ───────
class TicksEngineContextIntegrationTests(SimpleTestCase):
    def test_candle_close_updates_context(self):
        from forex.pepperstone.ticks_engine import TicksEngine

        eng = TicksEngine(symbols=["EURUSD"])
        # Avoid ClickHouse reads during the strategy-refresh block.
        eng._read_user_strategy = lambda: "malysian"
        eng._read_user_symbols = lambda: []
        eng._subscribe_new_symbols = lambda syms: None
        eng._broadcast_symbol_list = lambda: None

        candle = {
            "symbol": "EURUSD",
            "timeframe": 900,
            "open": 1.1000,
            "high": 1.1050,
            "low": 1.0950,
            "close": 1.1020,
            "open_time": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            "volume_ticks": 10,
        }
        # Non-fatal: must not raise even if a context sub-update fails.
        eng._on_candle_close(candle)

        self.assertIsNotNone(eng._context_engine)
        st = eng._context_engine.get("EURUSD")
        self.assertEqual(st.current_price, 1.1020)
        # Structure engine for tf=900 was created → bias present (neutral).
        self.assertIn(900, st.multi_timeframe_context)
        self.assertEqual(st.multi_timeframe_context[900], "neutral")
