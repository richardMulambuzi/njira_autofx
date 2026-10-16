"""
Tests for startup continuity / gap backfill (improve.md §48, Phase 3).

Covers ``forex.analysis.startup.ensure_continuity``:
  (a) fresh data (gap <= 5 min) -> "fresh", no backfill
  (b) stale data (gap > 5 min)  -> "backfilled", backfill helper called
  (c) no data                   -> "no_data"
  (d) backfill failure          -> "error", no crash

The provider and ClickHouse client are mocked; no network / DB is touched.
"""
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase

from forex.analysis import startup


def _candle(minutes_ago: int) -> dict:
    open_time = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "symbol": "EURUSD",
        "timeframe": 60,
        "open_time": open_time,
        "close_time": open_time + timedelta(minutes=1),
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.05,
        "volume_ticks": 10,
        "is_closed": True,
        "gap_filled": False,
    }


class EnsureContinuityTests(SimpleTestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.provider = mock.MagicMock()

    # (a) fresh ---------------------------------------------------------
    @mock.patch("forex.analysis.startup.reconcile_last_candle")
    def test_fresh_no_backfill(self, mock_reconcile):
        mock_reconcile.return_value = _candle(minutes_ago=2)
        with mock.patch("forex.analysis.backfill.backfill_1m_range") as mock_bf:
            result = startup.ensure_continuity(self.client, self.provider, "EURUSD")
        self.assertEqual(result["status"], "fresh")
        self.assertIn("gap_minutes", result)
        self.assertLessEqual(result["gap_minutes"], 5)
        mock_bf.assert_not_called()

    # (b) stale ---------------------------------------------------------
    @mock.patch("forex.analysis.startup.reconcile_last_candle")
    def test_stale_backfilled(self, mock_reconcile):
        mock_reconcile.return_value = _candle(minutes_ago=10)
        with mock.patch("forex.analysis.backfill.backfill_1m_range") as mock_bf:
            mock_bf.return_value = {
                "fetched": 600,
                "inserted": 9,
                "skipped_duplicates": 0,
                "invalid": 0,
                "gaps": 0,
                "oldest": None,
                "newest": None,
            }
            result = startup.ensure_continuity(self.client, self.provider, "EURUSD")
        self.assertEqual(result["status"], "backfilled")
        self.assertEqual(result["backfilled_candles"], 9)
        self.assertGreater(result["gap_minutes"], 5)
        mock_bf.assert_called_once()
        args, _ = mock_bf.call_args
        # client, provider, symbol, start_epoch, end_epoch
        self.assertIs(args[0], self.client)
        self.assertIs(args[1], self.provider)
        self.assertEqual(args[2], "EURUSD")
        # window excludes the in-progress minute (end < now's epoch)
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        self.assertLess(args[4], now_epoch)
        self.assertLessEqual(args[3], args[4])

    # (c) no data -------------------------------------------------------
    @mock.patch("forex.analysis.startup.reconcile_last_candle")
    def test_no_data(self, mock_reconcile):
        mock_reconcile.return_value = None
        with mock.patch("forex.analysis.backfill.backfill_1m_range") as mock_bf:
            result = startup.ensure_continuity(self.client, self.provider, "EURUSD")
        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["symbol"], "EURUSD")
        mock_bf.assert_not_called()

    # (d) backfill failure --------------------------------------------
    @mock.patch("forex.analysis.startup.reconcile_last_candle")
    def test_backfill_failure_no_crash(self, mock_reconcile):
        mock_reconcile.return_value = _candle(minutes_ago=10)
        with mock.patch("forex.analysis.backfill.backfill_1m_range") as mock_bf:
            mock_bf.side_effect = RuntimeError("provider exploded")
            result = startup.ensure_continuity(self.client, self.provider, "EURUSD")
        self.assertEqual(result["status"], "error")
        self.assertIn("reason", result)
        self.assertEqual(result["reason"], "provider exploded")
