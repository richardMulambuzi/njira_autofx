"""
Multi-timeframe backtest upload tests:

(a) infer_timeframe parses D1/H1/H4/M15/M30/M5/M1/W1 filename tokens.
(b) parse_tf_csv returns candles carrying the ``timeframe`` key.
(c) HotCache.set_candles replaces the deque / removes it when empty.
(d) Engine with higher_tf_candles: the hot cache 3600 deque equals the REAL
    uploaded bars (aggregated 3600 bars are NOT appended).
(e) Engine without higher_tf_candles still aggregates 3600 from 1m.
(f) Upload view: POST 1m CSV + higher_tf_files -> 200 with timeframes_used.

Run: python manage.py test forex.tests.test_backtest_mtf
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from forex.analysis.cache import HotCache
from forex.backtest.engine import BacktestEngine, BacktestResult
from forex.backtest.parser import infer_timeframe, parse_csv, parse_tf_csv

# Real MT4/MT5 export shape (no ticker column, full-datetime Date cell).
REAL_HEADER = "Date,open,high,low,close,tick_volume"
REAL_ROW = "2012-11-14 05:15:00,83042.0,83055.0,83029.0,83048.0,313"

# njira_fx_api/ (project root = 2 up from forex/tests).
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_M15_PATH = os.path.join(_PROJECT_ROOT, "historical-data", "AUDJPY", "AUDJPYm15.csv")
REAL_H1_PATH = os.path.join(_PROJECT_ROOT, "historical-data", "AUDJPY", "AUDJPYh1.csv")


# ── (a) TF inference from filename ───────────────────────────────────────
class InferTimeframeTests(SimpleTestCase):
    def test_known_filename_tokens(self):
        cases = {
            "AUDJPYd1.csv": 86400,
            "AUDJPYh1.csv": 3600,
            "AUDJPYh4.csv": 14400,
            "AUDJPYm15.csv": 900,
            "AUDJPYm30.csv": 1800,
            "AUDJPYm5.csv": 300,
            "AUDJPYm1.csv": 60,
            "AUDJPYw1.csv": 604800,
        }
        for name, expected in cases.items():
            self.assertEqual(infer_timeframe(name), expected, name)

    def test_no_token_returns_none(self):
        self.assertIsNone(infer_timeframe("AUDJPY.csv"))
        self.assertIsNone(infer_timeframe(""))

    def test_case_insensitive(self):
        self.assertEqual(infer_timeframe("AUDJPYH1.CSV"), 3600)


# ── (b) parse_tf_csv timeframe key ───────────────────────────────────────
class ParseTfCsvTests(SimpleTestCase):
    def test_timeframe_key_carried_and_inferred(self):
        text = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "AUDJPY,20250101,000000,100.0,101.0,99.0,100.5\n"
        )
        import io
        f = io.StringIO(text)
        f.name = "AUDJPYh1.csv"
        bars = parse_tf_csv(f, symbol="AUDJPY")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["timeframe"], 3600)
        self.assertEqual(bars[0]["symbol"], "AUDJPY")
        # explicit timeframe wins over filename
        bars2 = parse_tf_csv(StringIO(text), symbol="AUDJPY", timeframe=900)
        self.assertEqual(bars2[0]["timeframe"], 900)

    def test_defaults_to_3600_without_token_or_timeframe(self):
        text = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "AUDJPY,20250101,000000,1.1,1.1,1.1,1.1\n"
        )
        import io
        f = io.StringIO(text)
        bars = parse_tf_csv(f, symbol="AUDJPY")  # no .name -> falls back to 3600
        self.assertEqual(bars[0]["timeframe"], 3600)

    def test_parse_csv_is_still_1m(self):
        from forex.backtest.parser import parse_csv
        text = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "AUDJPY,20250101,000000,1.1,1.1,1.1,1.1\n"
        )
        self.assertEqual(parse_csv(StringIO(text))[0]["timeframe"], 60)


# ── (c) HotCache.set_candles ─────────────────────────────────────────────
class SetCandlesTests(SimpleTestCase):
    def test_set_then_get_returns_real_bars(self):
        cache = HotCache()
        bars = [
            {"symbol": "AUDJPY", "timeframe": 3600, "open_time": datetime(2025, 1, 1, tzinfo=timezone.utc),
             "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
        ]
        cache.set_candles("AUDJPY", 3600, bars)
        self.assertEqual(cache.get("AUDJPY", 3600), bars)

    def test_empty_set_removes_entry(self):
        cache = HotCache()
        cache.set_candles("AUDJPY", 3600, [{"symbol": "AUDJPY", "timeframe": 3600}])
        cache.set_candles("AUDJPY", 3600, [])
        self.assertEqual(cache.get("AUDJPY", 3600), [])


# ── (d) + (e) engine override / fallback ─────────────────────────────────
def _candles_1m(symbol: str, n_minutes: int) -> list[dict]:
    start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return [
        {
            "symbol": symbol,
            "open_time": start + timedelta(minutes=i),
            "open": 100.0 + i * 0.1,
            "high": 100.0 + i * 0.1,
            "low": 100.0 + i * 0.1,
            "close": 100.0 + i * 0.1,
        }
        for i in range(n_minutes)
    ]


class _InertSignalEngine:
    def evaluate(self, symbol, mode, closed_tf=None, skip_status=False):
        return []


class EngineHigherTfTests(SimpleTestCase):
    def test_uploaded_h1_overrides_aggregated(self):
        candles = _candles_1m("AUDJPY", 61)  # 00:00..01:00 -> one H1 bar closes
        real_h1 = [
            {"symbol": "AUDJPY", "timeframe": 3600,
             "open_time": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
             "open": 1.0, "high": 999.0, "low": 1.0, "close": 1.0, "volume": 5},
            {"symbol": "AUDJPY", "timeframe": 3600,
             "open_time": datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc),
             "open": 1.0, "high": 999.0, "low": 1.0, "close": 1.0, "volume": 5},
        ]
        engine = BacktestEngine(signal_engine=_InertSignalEngine())
        engine.run(candles, "AUDJPY", initial_balance=1000.0, risk_per_trade=0.01,
                   higher_tf_candles={3600: real_h1})

        cached = engine._hot_cache.get("AUDJPY", 3600)
        self.assertEqual(cached, real_h1)  # exactly the real bars, nothing appended
        self.assertTrue(cached)
        self.assertEqual(cached[0]["high"], 999.0)  # real, not aggregated

    def test_no_upload_falls_back_to_aggregation(self):
        candles = _candles_1m("AUDJPY", 61)
        engine = BacktestEngine(signal_engine=_InertSignalEngine())
        engine.run(candles, "AUDJPY", initial_balance=1000.0, risk_per_trade=0.01)

        cached = engine._hot_cache.get("AUDJPY", 3600)
        self.assertTrue(cached)  # aggregated H1 bar present
        self.assertNotIn(999.0, [c["high"] for c in cached])
        self.assertGreater(cached[0]["high"], 100.0)


# ── (f) Multi-file upload view ───────────────────────────────────────────
class MultiFileUploadViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(username="mtfuser", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_upload_with_h1_file_reports_timeframes_used(self):
        one_min = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "AUDJPY,20250101,000000,100,100,100,100\n"
            "AUDJPY,20250101,000100,100,103,100,102\n"
        )
        h1 = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "AUDJPY,20250101,000000,99.5,101.0,99.0,100.5\n"
            "AUDJPY,20250101,010000,100.5,102.0,100.0,101.5\n"
            "AUDJPY,20250101,020000,101.5,103.0,101.0,102.5\n"
        )
        f1 = SimpleUploadedFile("audjpy.csv", one_min.encode("utf-8"), content_type="text/csv")
        fh1 = SimpleUploadedFile("AUDJPYh1.csv", h1.encode("utf-8"), content_type="text/csv")

        canned = BacktestResult(
            symbol="AUDJPY", initial_balance=10000.0, final_balance=10000.0,
            total_pnl=0.0, overall_win_rate=0.0,
            per_strategy=[], trades=[], best_strategy=None,
        )

        from unittest import mock
        with mock.patch("forex.backtest.views.BacktestEngine") as MockEngine:
            MockEngine.return_value.run.return_value = canned
            resp = self.client.post(
                "/api/backtest/upload/",
                {"file": f1, "higher_tf_files": fh1},
                format="multipart",
            )

        self.assertEqual(resp.status_code, 200)
        tfs = resp.json()["data"]["timeframes_used"]
        by_tf = {t["timeframe"]: t["source"] for t in tfs}
        self.assertEqual(by_tf[3600], "uploaded")
        self.assertEqual(by_tf[60], "aggregated")
        self.assertEqual(by_tf[300], "aggregated")
        # engine got the real H1 bars
        call = MockEngine.return_value.run.call_args
        htf = call.kwargs.get("higher_tf_candles")
        self.assertIn(3600, htf)
        self.assertEqual(len(htf[3600]), 3)
        self.assertEqual(htf[3600][0]["timeframe"], 3600)


# ── (b)+(c) real MT4/MT5 Date format ─────────────────────────────────────
class ParseRealFormatTests(SimpleTestCase):
    def test_parse_tf_csv_reads_real_mt4_format(self):
        text = f"{REAL_HEADER}\n{REAL_ROW}\n2012-11-14 05:30:00,83049.0,83076.0,83044.0,83072.0,347\n"
        f = StringIO(text)
        f.name = "AUDJPYm15.csv"
        bars = parse_tf_csv(f, symbol="AUDJPY")
        self.assertEqual(len(bars), 2)
        b0 = bars[0]
        self.assertEqual(b0["symbol"], "AUDJPY")
        self.assertEqual(b0["timeframe"], 900)  # inferred from filename
        self.assertEqual(b0["open_time"], datetime(2012, 11, 14, 5, 15, tzinfo=timezone.utc))
        self.assertEqual(b0["open"], 83042.0)
        self.assertEqual(b0["high"], 83055.0)
        self.assertEqual(b0["low"], 83029.0)
        self.assertEqual(b0["close"], 83048.0)
        self.assertEqual(b0["volume"], 313)  # tick_volume column
        self.assertEqual(bars[1]["volume"], 347)

    def test_parse_tf_csv_date_only_d1_cell(self):
        text = (
            "Date,open,high,low,close,tick_volume\n"
            "2012-12-04,85629.0,85915.0,85411.0,85695.0,86552\n"
        )
        f = StringIO(text)
        f.name = "AUDJPYd1.csv"
        bars = parse_tf_csv(f, symbol="AUDJPY")
        self.assertEqual(bars[0]["timeframe"], 86400)
        self.assertEqual(bars[0]["open_time"], datetime(2012, 12, 4, 0, 0, tzinfo=timezone.utc))

    def test_parse_csv_regression_on_real_format_requires_symbol(self):
        # Regression: parse_csv previously returned 0 candles for this format.
        text = f"{REAL_HEADER}\n{REAL_ROW}\n"
        self.assertEqual(parse_csv(StringIO(text), symbol="AUDJPY")[0]["volume"], 313)
        # no symbol override -> no candle (no ticker column)
        self.assertEqual(len(parse_csv(StringIO(text))), 0)

    def test_legacy_format_still_parses(self):
        text = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "AUDJPY,20250101,000000,100.0,101.0,99.0,100.5\n"
        )
        c = parse_csv(StringIO(text))[0]
        self.assertEqual(c["symbol"], "AUDJPY")
        self.assertEqual(c["timeframe"], 60)
        self.assertEqual(c["open_time"], datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc))


# ── (e)+(f) engine with M15 base ─────────────────────────────────────────
def _candles_at(symbol: str, n_bars: int, step_minutes: int) -> list[dict]:
    start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return [
        {
            "symbol": symbol,
            "open_time": start + timedelta(minutes=i * step_minutes),
            "open": 100.0 + i * 0.1,
            "high": 100.0 + i * 0.1,
            "low": 100.0 + i * 0.1,
            "close": 100.0 + i * 0.1,
        }
        for i in range(n_bars)
    ]


class EngineM15BaseTests(SimpleTestCase):
    def test_uploaded_h1_wins_over_aggregation_from_m15_base(self):
        # 5 M15 bars = 75 minutes -> one H1 bar (00:00-01:00) closes at 01:00.
        candles = _candles_at("AUDJPY", 5, 15)
        real_h1 = [
            {"symbol": "AUDJPY", "timeframe": 3600,
             "open_time": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
             "open": 1.0, "high": 999.0, "low": 1.0, "close": 1.0, "volume": 5},
        ]
        engine = BacktestEngine(signal_engine=_InertSignalEngine())
        engine.run(candles, "AUDJPY", initial_balance=1000.0, risk_per_trade=0.01,
                   base_timeframe=900, higher_tf_candles={3600: real_h1})
        self.assertEqual(engine._hot_cache.get("AUDJPY", 3600), real_h1)
        # base TF aggregation still runs (M15 bars present in cache)
        self.assertTrue(engine._hot_cache.get("AUDJPY", 900))

    def test_m15_base_aggregates_h1_without_upload(self):
        candles = _candles_at("AUDJPY", 5, 15)
        engine = BacktestEngine(signal_engine=_InertSignalEngine())
        engine.run(candles, "AUDJPY", initial_balance=1000.0, risk_per_trade=0.01,
                   base_timeframe=900)
        h1 = engine._hot_cache.get("AUDJPY", 3600)
        self.assertTrue(h1)
        self.assertGreater(h1[0]["high"], 100.0)


# ── (g) real-format multi-file upload view ───────────────────────────────
class RealFormatUploadViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(username="mtfreal", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_m15_base_plus_h1_upload_reports_sources(self):
        m15 = f"{REAL_HEADER}\n{REAL_ROW}\n2012-11-14 05:30:00,83049.0,83076.0,83044.0,83072.0,347\n"
        h1 = (
            "Date,open,high,low,close,tick_volume\n"
            "2012-11-14 05:00:00,83050.0,83100.0,83040.0,83090.0,900\n"
            "2012-11-14 06:00:00,83090.0,83120.0,83070.0,83110.0,800\n"
        )
        fbase = SimpleUploadedFile("AUDJPYm15.csv", m15.encode("utf-8"), content_type="text/csv")
        fh1 = SimpleUploadedFile("AUDJPYh1.csv", h1.encode("utf-8"), content_type="text/csv")

        canned = BacktestResult(
            symbol="AUDJPY", initial_balance=10000.0, final_balance=10000.0,
            total_pnl=0.0, overall_win_rate=0.0,
            per_strategy=[], trades=[], best_strategy=None,
        )

        from unittest import mock
        with mock.patch("forex.backtest.views.BacktestEngine") as MockEngine:
            MockEngine.return_value.run.return_value = canned
            resp = self.client.post(
                "/api/backtest/upload/",
                {"file": fbase, "symbol": "AUDJPY", "higher_tf_files": fh1},
                format="multipart",
            )

        self.assertEqual(resp.status_code, 200)
        tfs = resp.json()["data"]["timeframes_used"]
        by_tf = {t["timeframe"]: t["source"] for t in tfs}
        self.assertEqual(by_tf[900], "aggregated")   # base M15
        self.assertEqual(by_tf[3600], "uploaded")    # real H1 wins

        call = MockEngine.return_value.run.call_args
        self.assertEqual(call.kwargs.get("base_timeframe"), 900)
        htf = call.kwargs.get("higher_tf_candles")
        self.assertIn(3600, htf)
        self.assertEqual(htf[3600][0]["volume"], 900)  # real H1 tick_volume survived


# ── (h) REAL-DATA SMOKE TEST ─────────────────────────────────────────────
class RealDataSmokeTests(SimpleTestCase):
    def test_real_audjpy_m15_runs_full_pipeline(self):
        if not os.path.exists(REAL_M15_PATH):
            self.skipTest("real AUDJPY M15 data not present")
        with open(REAL_M15_PATH, encoding="utf-8") as fh:
            text = "\n".join(fh.readlines()[:5000])

        candles = parse_tf_csv(StringIO(text), symbol="AUDJPY", timeframe=900)
        self.assertGreater(len(candles), 100)
        self.assertEqual(candles[0]["timeframe"], 900)

        engine = BacktestEngine()  # real pipeline (no injected signal engine)
        result = engine.run(
            candles, "AUDJPY",
            strategies=["malysian", "Moving_averages"],
            initial_balance=10000.0, risk_per_trade=0.01,
            base_timeframe=900,
        )
        self.assertIsInstance(result, BacktestResult)
        self.assertEqual(result.symbol, "AUDJPY")
        # M15 base + real H1... here: no H1 upload, so H1 was aggregated.
        self.assertTrue(engine._hot_cache.get("AUDJPY", 3600))
        print(f"[smoke] AUDJPY M15 x{len(candles)}: trades={len(result.trades)} "
              f"best={result.best_strategy} pnl={result.total_pnl:.2f} "
              f"per_strategy={[s.strategy for s in result.per_strategy]}")