"""
QA Gate 2 — endpoint tests for the Pepperstone pipeline views.

Covers the endpoints NOT exercised by forex/tests.py (auth-engineer's file):

    GET  /api/chart-data/  — auth, symbol allowlist/SQLi, limit clamping,
                             success envelope + data mapping.
    GET  /api/mtf-status/  — auth, symbol validation, master-signal alignment,
                             empty-bars handling.
    POST /api/trade/       — symbol/side/volume validation, no-market-data
                             risk_blocked path, spread-gate risk_blocked path,
                             executor failure paths.

All ClickHouse access is mocked — no live server is contacted.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

TEST_API_KEY = "test-service-key-0123456789abcdef"

VALID_TICK = {
    "symbol": "EURUSD",
    "bid": 1.1050,
    "ask": 1.1052,
    "spread": 2.0,
    "digits": 5,
    "timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
}

TRADE_PAYLOAD = {"symbol": "EURUSD", "side": "BUY", "volume": 100}


@contextmanager
def _mock_chart_reads(ticks=None, bars=None):
    """Mock ClickHouse reads for chart-data / mtf-status; yields the patches."""
    with (
        mock.patch("forex.ticks_views.get_clickhouse_client") as m_client,
        mock.patch(
            "forex.ticks_views.get_recent_ticks",
            return_value=ticks if ticks is not None else [],
        ) as m_ticks,
        mock.patch(
            "forex.ticks_views.get_ohlc_bars",
            return_value=bars if bars is not None else [],
        ) as m_bars,
    ):
        yield {
            "get_clickhouse_client": m_client,
            "get_recent_ticks": m_ticks,
            "get_ohlc_bars": m_bars,
        }


class EndpointBase(TestCase):
    """Shared users/JWT/cache hygiene for the endpoint tests."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.env_patch = mock.patch.dict(
            os.environ, {"TICKS_INGEST_API_KEY": TEST_API_KEY}, clear=False
        )
        self.env_patch.start()
        self.user = User.objects.create_user(
            username="normal@example.com",
            email="normal@example.com",
            password="Str0ng!Passw0rd",
        )
        self.staff = User.objects.create_user(
            username="staff@example.com",
            email="staff@example.com",
            password="Str0ng!Passw0rd",
            is_staff=True,
        )
        self.access_token = str(RefreshToken.for_user(self.user).access_token)
        self.staff_token = str(RefreshToken.for_user(self.staff).access_token)

    def tearDown(self):
        self.env_patch.stop()
        cache.clear()

    def _auth(self, token: str | None) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}


class ChartDataEndpointTests(EndpointBase):
    """GET /api/chart-data/ — auth, symbol allowlist, limit clamp, envelope."""

    def test_requires_authentication(self):
        resp = self.client.get("/api/chart-data/", {"symbol": "EURUSD"})
        self.assertEqual(resp.status_code, 401)

    def test_symbol_required(self):
        resp = self.client.get(
            "/api/chart-data/", **self._auth(self.access_token)
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["status"], "error")

    def test_invalid_symbol_rejected(self):
        for bad in ("EURUSD!", "eur usd", "SELECT * FROM ticks", "x" * 21, ""):
            resp = self.client.get(
                "/api/chart-data/",
                {"symbol": bad},
                **self._auth(self.access_token),
            )
            self.assertEqual(resp.status_code, 400, f"symbol={bad!r}")

    def test_sqli_attempt_rejected(self):
        # Symbol is allowlist-validated and bound via parameters= — a SQLi
        # payload must be rejected at the allowlist, never reach ClickHouse.
        resp = self.client.get(
            "/api/chart-data/",
            {"symbol": "EURUSD' OR '1'='1"},
            **self._auth(self.access_token),
        )
        self.assertEqual(resp.status_code, 400)

    def test_limit_clamped_to_max(self):
        with _mock_chart_reads(ticks=[]) as mocks:
            resp = self.client.get(
                "/api/chart-data/",
                {"symbol": "EURUSD", "limit": 99999},
                **self._auth(self.access_token),
            )
        self.assertEqual(resp.status_code, 200)
        # get_recent_ticks receives the clamped limit.
        mocks["get_recent_ticks"].assert_called_once_with(
            mock.ANY, "EURUSD", 2000
        )

    def test_limit_clamped_to_minimum(self):
        with _mock_chart_reads(ticks=[]) as mocks:
            resp = self.client.get(
                "/api/chart-data/",
                {"symbol": "EURUSD", "limit": 0},
                **self._auth(self.access_token),
            )
        self.assertEqual(resp.status_code, 200)
        mocks["get_recent_ticks"].assert_called_once_with(
            mock.ANY, "EURUSD", 1
        )

    def test_non_numeric_limit_falls_back_to_default(self):
        with _mock_chart_reads(ticks=[]) as mocks:
            resp = self.client.get(
                "/api/chart-data/",
                {"symbol": "EURUSD", "limit": "abc"},
                **self._auth(self.access_token),
            )
        self.assertEqual(resp.status_code, 200)
        mocks["get_recent_ticks"].assert_called_once_with(
            mock.ANY, "EURUSD", 500
        )

    def test_success_envelope_and_data_mapping(self):
        ticks = [
            {"bid": 1.1050, "ask": 1.1052, "spread": 2.0, "digits": 5,
             "time_str": "2026-08-17 10:00:00"},
            {"bid": 1.1051, "ask": 1.1053, "spread": 2.0, "digits": 5,
             "time_str": "2026-08-17 10:00:01"},
        ]
        with _mock_chart_reads(ticks=ticks):
            resp = self.client.get(
                "/api/chart-data/",
                {"symbol": "EURUSD", "limit": 500},
                **self._auth(self.access_token),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["symbol"], "EURUSD")
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["data"][0]["bid"], 1.1050)
        self.assertEqual(body["data"][0]["time"], "2026-08-17 10:00:00")
        # Wire format: bid/ask/spread/time only — digits/time_str not leaked.
        self.assertEqual(
            set(body["data"][0].keys()), {"bid", "ask", "spread", "time"}
        )

    def test_symbol_normalized_to_uppercase(self):
        with _mock_chart_reads(ticks=[]) as mocks:
            resp = self.client.get(
                "/api/chart-data/",
                {"symbol": "eurusd"},
                **self._auth(self.access_token),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["symbol"], "EURUSD")
        mocks["get_recent_ticks"].assert_called_once_with(
            mock.ANY, "EURUSD", 500
        )


class MtfStatusEndpointTests(EndpointBase):
    """GET /api/mtf-status/ — auth, symbol validation, alignment, empty bars."""

    def test_requires_authentication(self):
        resp = self.client.get("/api/mtf-status/", {"symbol": "EURUSD"})
        self.assertEqual(resp.status_code, 401)

    def test_symbol_required(self):
        resp = self.client.get(
            "/api/mtf-status/", **self._auth(self.access_token)
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_symbol_rejected(self):
        resp = self.client.get(
            "/api/mtf-status/",
            {"symbol": "EURUSD; DROP TABLE ticks"},
            **self._auth(self.access_token),
        )
        self.assertEqual(resp.status_code, 400)

    def test_success_envelope_with_all_timeframes(self):
        bars = [
            {"time": "2026-08-17T10:00:00Z", "open": 1.10, "high": 1.11,
             "low": 1.09, "close": 1.105},
        ] * 12  # >= SMA_PERIOD so the SMA is defined
        with _mock_chart_reads(bars=bars):
            resp = self.client.get(
                "/api/mtf-status/",
                {"symbol": "EURUSD"},
                **self._auth(self.access_token),
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["symbol"], "EURUSD")
        self.assertEqual(body["masterSignal"], "HOLD")  # no cached signal
        self.assertEqual(
            set(body["timeframes"].keys()),
            {"1M", "5M", "15M", "30M", "1H", "4H", "1D"},
        )
        tf = body["timeframes"]["1M"]
        self.assertEqual(tf["sma"], 1.105)
        self.assertEqual(tf["current_value"], 1.105)
        self.assertFalse(tf["aligned"])  # HOLD master signal -> not aligned
        self.assertEqual(len(tf["chart_history"]), 12)

    def test_empty_bars_yield_null_sma_and_zero_close(self):
        with _mock_chart_reads(bars=[]):
            resp = self.client.get(
                "/api/mtf-status/",
                {"symbol": "EURUSD"},
                **self._auth(self.access_token),
            )
        self.assertEqual(resp.status_code, 200)
        tf = resp.json()["timeframes"]["1M"]
        self.assertIsNone(tf["sma"])
        self.assertEqual(tf["current_value"], 0)
        self.assertFalse(tf["aligned"])
        self.assertEqual(tf["chart_history"], [])

    def test_buy_master_signal_marks_aligned_timeframes(self):
        bars = [
            {"time": "2026-08-17T10:00:00Z", "open": 1.10, "high": 1.11,
             "low": 1.09, "close": 1.105},
        ] * 12
        cache.set("macro_signal_EURUSD", "BUY")
        try:
            with _mock_chart_reads(bars=bars):
                resp = self.client.get(
                    "/api/mtf-status/",
                    {"symbol": "EURUSD"},
                    **self._auth(self.access_token),
                )
        finally:
            cache.delete("macro_signal_EURUSD")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["masterSignal"], "BUY")
        # close (1.105) is NOT > sma (1.105) -> not aligned (touch).
        self.assertFalse(body["timeframes"]["1M"]["aligned"])

    def test_sell_master_signal_marks_aligned_timeframes(self):
        bars = [
            {"time": "2026-08-17T10:00:00Z", "open": 1.10, "high": 1.11,
             "low": 1.09, "close": 1.105},
        ] * 12
        cache.set("macro_signal_EURUSD", "SELL")
        try:
            with _mock_chart_reads(bars=bars):
                resp = self.client.get(
                    "/api/mtf-status/",
                    {"symbol": "EURUSD"},
                    **self._auth(self.access_token),
                )
        finally:
            cache.delete("macro_signal_EURUSD")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["masterSignal"], "SELL")


class TradeValidationAndRiskTests(EndpointBase):
    """POST /api/trade/ — validation + risk_blocked + executor failure paths."""

    def _post(self, payload: dict, token: str | None = None):
        return self.client.post(
            "/api/trade/",
            payload,
            format="json",
            **self._auth(token or self.staff_token),
        )

    def test_bad_symbol_rejected(self):
        resp = self._post({**TRADE_PAYLOAD, "symbol": "EURUSD!"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["status"], "error")

    def test_sqli_symbol_rejected(self):
        resp = self._post({**TRADE_PAYLOAD, "symbol": "EURUSD' OR '1'='1"})
        self.assertEqual(resp.status_code, 400)

    def test_bad_side_rejected(self):
        resp = self._post({**TRADE_PAYLOAD, "side": "HODL"})
        self.assertEqual(resp.status_code, 400)

    def test_volume_bounds_rejected(self):
        with mock.patch("forex.ticks_views.TRADE_MAX_VOLUME", 100000):
            for volume in (0, -5, 100001):
                resp = self._post({**TRADE_PAYLOAD, "volume": volume})
                self.assertEqual(resp.status_code, 400, f"volume={volume}")

    def test_no_market_data_blocks_trade(self):
        # get_recent_ticks -> [] -> risk_blocked (no spread to evaluate).
        with (
            mock.patch("forex.ticks_views.get_clickhouse_client"),
            mock.patch("forex.ticks_views.get_recent_ticks", return_value=[]),
        ):
            resp = self._post(TRADE_PAYLOAD)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "risk_blocked")
        self.assertIn("No market data available", body["reason"])

    def test_spread_gate_blocks_trade(self):
        # Newest tick spread (5.0 pips) exceeds RISK_MAX_SPREAD_PIPS (0.3).
        with (
            mock.patch("forex.ticks_views.get_clickhouse_client"),
            mock.patch(
                "forex.ticks_views.get_recent_ticks",
                return_value=[{"spread": 5.0, "bid": 1.1, "ask": 1.15}],
            ),
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
        ):
            resp = self._post(TRADE_PAYLOAD)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "risk_blocked")
        self.assertIn("Spread 5.00 pips exceeds max", body["reason"])

    def test_drawdown_gate_blocks_trade(self):
        with (
            mock.patch("forex.ticks_views.get_clickhouse_client"),
            mock.patch(
                "forex.ticks_views.get_recent_ticks",
                return_value=[{"spread": 0.1, "bid": 1.1, "ask": 1.1001}],
            ),
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=-5000.0),
        ):
            resp = self._post(TRADE_PAYLOAD)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "risk_blocked")
        self.assertIn("Daily loss limit reached", body["reason"])

    def test_executor_failure_returns_generic_error(self):
        with (
            mock.patch("forex.ticks_views.get_clickhouse_client"),
            mock.patch(
                "forex.ticks_views.get_recent_ticks",
                return_value=[{"spread": 0.1, "bid": 1.1, "ask": 1.1001}],
            ),
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
            mock.patch(
                "forex.ticks_views.TradingProviderFactory.get_active",
                side_effect=RuntimeError("secret-executor-detail"),
            ),
        ):
            resp = self._post(TRADE_PAYLOAD)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["message"], "Internal error")
        self.assertNotIn("secret-executor-detail", resp.content.decode())

    def test_executor_unavailable_returns_clean_error(self):
        from forex.broker.errors import BrokerError, BrokerErrorCode

        with (
            mock.patch("forex.ticks_views.get_clickhouse_client"),
            mock.patch(
                "forex.ticks_views.get_recent_ticks",
                return_value=[{"spread": 0.1, "bid": 1.1, "ask": 1.1001}],
            ),
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
            mock.patch(
                "forex.ticks_views.TradingProviderFactory.get_active",
                side_effect=BrokerError(
                    BrokerErrorCode.BROKER_UNAVAILABLE,
                    "Execution engine not ready",
                ),
            ),
        ):
            resp = self._post(TRADE_PAYLOAD)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "error")
        self.assertIn("Execution engine not ready", body["message"])
        self.assertEqual(body["error_code"], "broker_unavailable")

    def test_successful_trade_returns_executor_result(self):
        from forex.broker.models import BrokerOrder, OrderSide, OrderStatus

        order = BrokerOrder(
            provider="pepperstone",
            external_order_id="999",
            account_id="5327660",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type="market",
            quantity=100,
            status=OrderStatus.FILLED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        with (
            mock.patch("forex.ticks_views.get_clickhouse_client"),
            mock.patch(
                "forex.ticks_views.get_recent_ticks",
                return_value=[{"spread": 0.1, "bid": 1.1, "ask": 1.1001}],
            ),
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
            mock.patch(
                "forex.ticks_views.TradingProviderFactory.get_active"
            ) as m_factory,
        ):
            m_factory.return_value.place_order.return_value = order
            resp = self._post(TRADE_PAYLOAD)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["data"]["order_id"], "999")
        self.assertEqual(body["data"]["status"], "filled")
        m_factory.return_value.place_order.assert_called_once_with(
            "EURUSD", "BUY", 100
        )