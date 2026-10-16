"""
Backtest flow tests — the 5 closed gaps:

(a) parse_csv with a volume column preserves the real volume.
(b) parse_csv without a volume column synthesizes a non-zero estimate.
(c) resample aggregates volume (sum).
(d) pair override: an explicit symbol replaces every candle's detected pair.
(e) no symbol column + no symbol param -> 400.
(f) evaluate returns MULTIPLE qualifying signals (not just the strongest).
(g) same symbol+direction from two strategies -> one COMBINED signal with
    source_strategies attribution.
(h) the streaming endpoint emits progress events then a done event.

Run: python manage.py test forex.tests.test_backtest_flow
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.signal_lifecycle import Signal, SignalState
from forex.analysis.strategy import StrategyReport
from forex.backtest.engine import BacktestResult, StrategyBacktestResult
from forex.backtest.parser import parse_csv, resample

# Real captured October 2026 XAUUSD M1 export (HistData ASCII). Kept as the
# ground truth for the magnitude guard: these rows must never be rejected.
_OCT_CSV = Path(__file__).resolve().parents[3] / "historical-data" / "XAGUSD_2026" / "XAUUSD_OCT01-09_2026_M1.csv"


def _csv(rows: str, header: str = "ticker,datestamp,timestamp,open,high,low,close") -> StringIO:
    return StringIO(f"{header}\n{rows}")


def _signal(sid: str, strategy: str, direction: str, confidence: float = 75.0) -> Signal:
    return Signal(
        signal_id=sid,
        symbol="XAUUSD",
        direction=direction,
        state=SignalState.CONFIRMED,
        entry=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        confidence_score=confidence,
        explanation="test setup",
        strategy=strategy,
    )


# ── (a) Real volume preserved ─────────────────────────────────────────────
class VolumeColumnTests(SimpleTestCase):
    def test_volume_column_preserved(self):
        csv_text = _csv(
            "XAUUSD,20250101,230100,2625.09,2625.09,2624.47,2624.95,1000\n",
            header="ticker,datestamp,timestamp,open,high,low,close,volume",
        )
        candles = parse_csv(csv_text)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["volume"], 1000)

    def test_alt_volume_aliases_detected(self):
        for alias in ("vol", "tick_volume"):
            csv_text = _csv(
                "XAUUSD,20250101,230100,2625.09,2625.09,2624.47,2624.95,42\n",
                header=f"ticker,datestamp,timestamp,open,high,low,close,{alias}",
            )
            self.assertEqual(parse_csv(csv_text)[0]["volume"], 42)


# ── (b) Synthesized volume ────────────────────────────────────────────────
class SynthesizedVolumeTests(SimpleTestCase):
    def test_volume_synthesized_from_range(self):
        # XAUUSD ~2625 -> price > 1000 -> 2 digits -> tick 0.01; range 0.62 -> 62.
        csv_text = _csv(
            "XAUUSD,20250101,230100,2625.09,2625.09,2624.47,2624.95\n"
        )
        candles = parse_csv(csv_text)
        self.assertEqual(len(candles), 1)
        vol = candles[0]["volume"]
        self.assertIsNotNone(vol)
        self.assertEqual(vol, 62)
        self.assertGreater(vol, 0)

    def test_flat_candle_gets_minimum_volume(self):
        csv_text = _csv("XAUUSD,20250101,000000,2600.0,2600.0,2600.0,2600.0\n")
        self.assertEqual(parse_csv(csv_text)[0]["volume"], 1)


# ── (c) resample aggregates volume ────────────────────────────────────────
class ResampleVolumeTests(SimpleTestCase):
    def test_resample_sums_volume(self):
        candles = [
            {"symbol": "XAUUSD",
             "open_time": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10},
            {"symbol": "XAUUSD",
             "open_time": datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc),
             "open": 100.0, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 5},
        ]
        out = resample(candles, 900)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["volume"], 15)

    def test_resample_without_volume_keeps_no_key(self):
        candles = [
            {"symbol": "XAUUSD",
             "open_time": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        ]
        self.assertNotIn("volume", resample(candles, 900)[0])


# ── (d) Pair override ─────────────────────────────────────────────────────
class PairOverrideTests(SimpleTestCase):
    def test_symbol_override_replaces_detected_ticker(self):
        csv_text = _csv(
            "EURUSD,20250101,230100,1.08500,1.08520,1.08480,1.08510\n"
            "EURUSD,20250101,230200,1.08510,1.08560,1.08500,1.08540\n"
        )
        candles = parse_csv(csv_text, symbol="EURUSD")
        self.assertEqual(len(candles), 2)
        self.assertTrue(all(c["symbol"] == "EURUSD" for c in candles))

    def test_no_override_uses_detected_ticker(self):
        csv_text = _csv("XAUUSD,20250101,230100,2625.0,2625.0,2625.0,2625.0\n")
        self.assertEqual(parse_csv(csv_text)[0]["symbol"], "XAUUSD")


# ── (e) No pair + no symbol -> 400 ────────────────────────────────────────
class NoPairDetectViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(username="btflow", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_missing_symbol_column_returns_400(self):
        csv_text = (
            "datestamp,timestamp,open,high,low,close\n"
            "20250101,000000,1.1,1.1,1.1,1.1\n"
            "20250101,000100,1.1,1.1,1.1,1.1\n"
        )
        uploaded = SimpleUploadedFile("bare.csv", csv_text.encode("utf-8"), content_type="text/csv")
        resp = self.client.post("/api/backtest/upload/", {"file": uploaded}, format="multipart")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Could not detect pair from dataset", resp.json()["message"])

    def test_missing_symbol_column_ok_with_override(self):
        csv_text = (
            "datestamp,timestamp,open,high,low,close\n"
            "20250101,000000,1.1,1.1,1.1,1.1\n"
            "20250101,000100,1.1,1.1,1.1,1.1\n"
        )
        uploaded = SimpleUploadedFile("bare.csv", csv_text.encode("utf-8"), content_type="text/csv")
        with mock.patch("forex.backtest.views.BacktestEngine") as MockEngine:
            MockEngine.return_value.run.return_value = BacktestResult(
                symbol="EURUSD", initial_balance=10000.0, final_balance=10000.0,
                total_pnl=0.0, overall_win_rate=0.0,
                per_strategy=[], trades=[], best_strategy=None,
            )
            resp = self.client.post(
                "/api/backtest/upload/", {"file": uploaded, "symbol": "EURUSD"},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["symbol"], "EURUSD")


# ── (f) Multiple qualifying signals returned ─────────────────────────────
class MultipleSignalsReturnedTests(SimpleTestCase):
    def test_multiple_strategies_return_all_qualifying_signals(self):
        from forex.analysis.strategy import MalysianStrategy, MovingAveragesStrategy
        eng = SignalEngine(cache=HotCache())
        eng._build_market_state = lambda symbol, direction, evidence: {}
        inert = StrategyReport(
            name="malysian", active=True, applicable=False,
            direction="NEUTRAL", reason="test",
        )
        with mock.patch.object(MalysianStrategy, "evaluate", return_value=inert), \
             mock.patch.object(MovingAveragesStrategy, "evaluate", return_value=inert):
            eng._evaluate_snr = lambda symbol: [_signal("sig_mal", "malysian", "BUY", 80.0)]
            eng._evaluate_mtf = lambda symbol: [_signal("sig_mtf", "moving_averages", "SELL", 70.0)]

            out = eng.evaluate("XAUUSD", "both", closed_tf=3600)

        self.assertEqual(len(out), 2)
        strategies = sorted(s.strategy for s in out)
        self.assertEqual(strategies, ["malysian", "moving_averages"])


# ── (g) Combined attribution ──────────────────────────────────────────────
class CombinedAttributionTests(SimpleTestCase):
    def test_same_direction_merges_to_combined_signal(self):
        eng = SignalEngine(cache=HotCache())
        eng._build_market_state = lambda symbol, direction, evidence: {}
        inert = StrategyReport(
            name="malysian", active=True, applicable=False,
            direction="NEUTRAL", reason="test",
        )
        import forex.analysis.strategy as strat
        with mock.patch.object(strat.MalysianStrategy, "evaluate", return_value=inert), \
             mock.patch.object(strat.MovingAveragesStrategy, "evaluate", return_value=inert):
            eng._evaluate_snr = lambda symbol: [_signal("sig_c", "malysian", "BUY", 80.0)]
            eng._evaluate_mtf = lambda symbol: [_signal("sig_m", "moving_averages", "BUY", 75.0)]
            out = eng.evaluate("XAUUSD", "both", closed_tf=3600)

        self.assertEqual(len(out), 1)
        sig = out[0]
        self.assertTrue(sig.combined)
        self.assertEqual(sig.source_strategies, ["malysian", "Moving_averages"])
        self.assertIn("malysian", sig.strategy)
        self.assertIn("Moving_averages", sig.strategy)

    def test_single_strategy_signal_not_marked_combined(self):
        eng = SignalEngine(cache=HotCache())
        eng._build_market_state = lambda symbol, direction, evidence: {}
        eng._evaluate_snr = lambda symbol: [_signal("sig_c", "malysian", "BUY", 80.0)]
        eng._evaluate_mtf = lambda symbol: []
        out = eng.evaluate("XAUUSD", "malysian", closed_tf=3600)
        self.assertEqual(len(out), 1)
        sig = out[0]
        self.assertFalse(sig.combined)
        self.assertEqual(sig.source_strategies, ["malysian"])


# ── (h) Streaming endpoint ────────────────────────────────────────────────
class StreamUploadTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(username="btstream", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_stream_emits_progress_then_done(self):
        csv_text = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "XAUUSD,20250101,000000,2600,2600,2600,2600\n"
            "XAUUSD,20250101,000100,2600,2603,2600,2602\n"
        )
        uploaded = SimpleUploadedFile("gold.csv", csv_text.encode("utf-8"), content_type="text/csv")

        canned = BacktestResult(
            symbol="XAUUSD", initial_balance=10000.0, final_balance=10500.0,
            total_pnl=500.0, overall_win_rate=75.0,
            per_strategy=[StrategyBacktestResult(
                strategy="malysian", total_trades=4, wins=3, losses=1)],
            trades=[{"signal_id": "x", "strategy": "malysian", "outcome": "win"}],
            best_strategy="malysian",
        )

        def fake_run(candles, symbol, strategies=None, initial_balance=10000.0,
                     risk_per_trade=0.01, progress_callback=None,
                     higher_tf_candles=None, base_timeframe=60):
            progress_callback({
                "processed": 1, "total": 2, "percent": 50.0,
                "signals_so_far": 0, "symbol": symbol,
            })
            progress_callback({
                "processed": 2, "total": 2, "percent": 100.0,
                "signals_so_far": 1, "symbol": symbol,
            })
            return canned

        with mock.patch("forex.backtest.views.BacktestEngine") as MockEngine:
            MockEngine.return_value.run.side_effect = fake_run
            resp = self.client.post(
                "/api/backtest/upload/stream/",
                {"file": uploaded, "symbol": "XAUUSD"}, format="multipart",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/event-stream")
        body = b"".join(resp.streaming_content).decode("utf-8")
        self.assertIn("event: done", body)
        self.assertIn('"processed": 1', body)
        self.assertIn('"signals_so_far": 1', body)
        self.assertIn('"symbol": "XAUUSD"', body)
        self.assertIn('"best_strategy": "malysian"', body)


# ---------------------------------------------------------------------------
# (i) Magnitude guard: fail fast on a 1000x-scaled CSV (silent-mis-scale fix).
# ---------------------------------------------------------------------------
class MagnitudeGuardTests(SimpleTestCase):
    """A 1000x mis-scaled XAU file must raise, naming the file + row."""

    def _upload(self, rows: str, name: str = "XAUUSD_M1.csv") -> SimpleUploadedFile:
        """Wrap raw data rows (no header) in a named upload so the parser's
        ValueError can name the file."""
        return SimpleUploadedFile(
            name, _csv(rows).getvalue().encode("utf-8"), content_type="text/csv"
        )

    def test_thousand_x_scaled_xau_raises_value_error_naming_file_and_row(self):
        # 2624.95 is a real XAU price; x1000 is the mis-scale we must catch.
        rows = (
            "XAUUSD,20250101,000000,2624.95,2624.95,2624.95,2624.95\n"
            "XAUUSD,20250101,000100,2624950,2624950,2624950,2624950\n"
            "XAUUSD,20250101,000200,2624950,2624950,2624950,2624950\n"
        )
        with self.assertRaises(ValueError) as ctx:
            parse_csv(self._upload(rows, "XAUUSD_BADSCALE_2025_M1.csv"))
        message = str(ctx.exception)
        self.assertIn("XAUUSD_BADSCALE_2025_M1.csv", message)  # file named
        self.assertIn("row=2", message)  # offending row (1-based)
        self.assertIn("1000x", message)

    def test_guard_rejects_xau_even_when_first_rows_are_fine(self):
        # A true mis-scale inflates every column; the guard trips on row 3,
        # not on the first candle — proving it scans the whole file.
        rows = (
            "XAUUSD,20250101,000000,2624.95,2624.95,2624.95,2624.95\n"
            "XAUUSD,20250101,000100,2624.95,2624.95,2624.95,2624.95\n"
            "XAUUSD,20250101,000200,2624950,2624950,2624.95,2624950\n"
        )
        with self.assertRaises(ValueError) as ctx:
            parse_csv(self._upload(rows))
        self.assertIn("row=3", str(ctx.exception))

    def test_correct_xau_range_4140_to_4230_accepted(self):
        """Real Oct-2026 gold band must pass the guard unchallenged."""
        closes = [4140.0, 4180.25, 4194.35, 4200.0, 4210.5, 4226.69, 4230.0]
        rows = "".join(
            f"XAUUSD,20250101,{i:06d},{c},{c},{c},{c}\n" for i, c in enumerate(closes)
        )
        candles = parse_csv(self._upload(rows))
        self.assertEqual(len(candles), len(closes))
        self.assertEqual([c["close"] for c in candles], closes)

    def test_jpy_pair_in_jpy_band_still_accepted(self):
        # 155.123 is a legitimate USDJPY price — the guard must not touch it.
        candles = parse_csv(
            self._upload("USDJPY,20250101,000000,155.123,155.2,155.0,155.1\n", "USDJPY_M1.csv")
        )
        self.assertEqual(candles[0]["close"], 155.1)

    def test_unknown_symbol_is_not_guarded(self):
        candles = parse_csv(self._upload("SPX500,20250101,000000,5,5,5,5\n", "SPX500_M1.csv"))
        self.assertEqual(len(candles), 1)


class OctoberDataRegressionTests(SimpleTestCase):
    """The real Oct-2026 XAUUSD M1 export must parse and match known extremes.

    Pins the whole data-integrity chain end to end: real file -> parser ->
    magnitude guard -> per-day high/low aggregation. If the 1000x mis-scale ever
    returns (or the guard over-rejects), these fail.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # No skip-if-missing: a vanished fixture is a failure, not a skip.
        assert _OCT_CSV.is_file(), f"missing data-integrity fixture: {_OCT_CSV}"
        cls.candles = parse_csv(
            SimpleUploadedFile(
                _OCT_CSV.name,
                _OCT_CSV.read_bytes(),
                content_type="text/csv",
            )
        )

    def test_all_9519_rows_parse_past_the_magnitude_guard(self):
        self.assertEqual(len(self.candles), 9519)
        self.assertTrue(all(c["symbol"] == "XAUUSD" for c in self.candles))

    def test_period_high_is_4226_69_on_oct_02(self):
        day = [c for c in self.candles if c["open_time"].day == 2]
        self.assertTrue(day, "no 2026-10-02 candles")
        self.assertAlmostEqual(max(c["high"] for c in day), 4226.69, places=2)

    def test_period_low_is_4066_35_on_oct_07(self):
        day = [c for c in self.candles if c["open_time"].day == 7]
        self.assertTrue(day, "no 2026-10-07 candles")
        self.assertAlmostEqual(min(c["low"] for c in day), 4066.35, places=2)

    def test_no_candle_outside_the_real_gold_band(self):
        lows = [c["low"] for c in self.candles]
        highs = [c["high"] for c in self.candles]
        self.assertGreater(min(lows), 1000.0)  # not 1000x-scaled
        self.assertLess(max(highs), 10000.0)