"""
Authz hardening tests for the Pepperstone pipeline endpoints (auth-engineer).

Covers Security Gate 1 remediation:

    HIGH #6    POST /api/trade/ — staff/service-account only + daily caps
    HIGH #7    POST /api/ticks/ — X-API-Key / staff only + payload validation
    MEDIUM #15 — no exception detail leakage to clients
    MEDIUM #16 — OverflowError on hostile timestamps ("1e999")

Run: python manage.py test forex -v 2
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from forex.broker.factory import TradingProviderFactory

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
def _mock_tick_insert():
    """Mock ClickHouse so ingest_tick runs end-to-end without network."""
    with (
        mock.patch("forex.ticks_views.get_clickhouse_client"),
        mock.patch("forex.ticks_views.insert_ticks_batch"),
    ):
        yield


@contextmanager
def _mock_trade_pipeline():
    """
    Mock ClickHouse reads + executor so place_trade runs end-to-end without
    network. Risk gate passes (spread 0.1 < max 0.3, PnL 0.0) and the
    executor returns a success dict.
    """
    with (
        mock.patch("forex.ticks_views.get_clickhouse_client"),
        mock.patch(
            "forex.ticks_views.get_recent_ticks",
            return_value=[{"spread": 0.1, "bid": 1.1050, "ask": 1.1052}],
        ),
        mock.patch("forex.risk_manager.get_clickhouse_client"),
        mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
        # Patch where pepperstone.py actually uses the name (it does
        # `from ...execution_engine import TradeExecutor` at import time, so
        # patching execution_engine.TradeExecutor is a no-op). The real
        # executor is one-shot per process (Twisted reactor) — running it
        # here after broker tests consumed it would fail.
        mock.patch("forex.broker.providers.pepperstone.TradeExecutor") as m_exec,
    ):
        m_exec.return_value.execute_order.return_value = {
            "status": "success",
            "trade_id": 123,
        }
        yield m_exec


class TickEndpointBase(TestCase):
    """Shared helpers: users, JWT, service key, cache hygiene."""

    def setUp(self):
        cache.clear()
        # TradingProviderFactory._active is a class-level singleton — earlier
        # test modules can leave a stale provider cached, which makes
        # place_trade fail here. Reset so each test starts clean.
        TradingProviderFactory.reset()
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

    def _auth_headers(self, token: str | None) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}


class TickPermissionTests(TickEndpointBase):
    """HIGH #7 — ingest requires the service key or a staff user."""

    def test_normal_user_without_key_gets_403(self):
        resp = self.client.post(
            "/api/ticks/",
            VALID_TICK,
            format="json",
            **self._auth_headers(self.access_token),
        )
        self.assertEqual(resp.status_code, 403)

    def test_normal_user_with_wrong_key_gets_403(self):
        resp = self.client.post(
            "/api/ticks/",
            VALID_TICK,
            format="json",
            HTTP_X_API_KEY="wrong-key",
            **self._auth_headers(self.access_token),
        )
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_without_credentials_gets_denied(self):
        # DRF semantics: no credentials at all -> 401 NotAuthenticated
        # (a logged-in normal user without the key gets 403 instead).
        resp = self.client.post("/api/ticks/", VALID_TICK, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_service_account_with_correct_key_ingests(self):
        with _mock_tick_insert():
            resp = self.client.post(
                "/api/ticks/",
                VALID_TICK,
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), {"status": "success"})

    def test_staff_user_ingests_without_key(self):
        with _mock_tick_insert():
            resp = self.client.post(
                "/api/ticks/",
                VALID_TICK,
                format="json",
                **self._auth_headers(self.staff_token),
            )
        self.assertEqual(resp.status_code, 201)


class TickValidationTests(TickEndpointBase):
    """HIGH #7 — hostile payloads must be rejected with 400."""

    def _post(self, payload: dict):
        return self.client.post(
            "/api/ticks/", payload, format="json", HTTP_X_API_KEY=TEST_API_KEY
        )

    def _assert_invalid(self, payload: dict):
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["status"], "error")

    def test_bid_above_ask_rejected(self):
        payload = {**VALID_TICK, "bid": 1.1060, "ask": 1.1052}
        self._assert_invalid(payload)

    def test_non_positive_bid_rejected(self):
        self._assert_invalid({**VALID_TICK, "bid": 0})
        self._assert_invalid({**VALID_TICK, "bid": -1.5})

    def test_non_positive_ask_rejected(self):
        self._assert_invalid({**VALID_TICK, "ask": 0})

    def test_negative_spread_rejected(self):
        self._assert_invalid({**VALID_TICK, "spread": -0.5})

    def test_digits_out_of_range_rejected(self):
        self._assert_invalid({**VALID_TICK, "digits": 0})
        self._assert_invalid({**VALID_TICK, "digits": 9})

    def test_non_finite_prices_rejected(self):
        # Raw NaN/Inf is already rejected by DRF's JSON parser, but a string
        # "NaN"/"Infinity" passes FloatField (float("NaN") == nan) and must be
        # caught by the serializer's finite check.
        for bad_bid, bad_ask in (("NaN", None), (None, "Infinity"), ("1e999", None)):
            payload = dict(VALID_TICK)
            if bad_bid is not None:
                payload["bid"] = bad_bid
            else:
                payload["ask"] = bad_ask
            resp = self.client.post(
                "/api/ticks/",
                payload,
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["status"], "error")

    def test_bad_symbol_rejected(self):
        self._assert_invalid({**VALID_TICK, "symbol": "EURUSD!"})

    def test_huge_timestamp_string_rejected(self):
        # MEDIUM #16 — "1e999" raised OverflowError before this fix.
        self._assert_invalid({**VALID_TICK, "timestamp": "1e999"})

    def test_future_timestamp_rejected(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        self._assert_invalid(
            {**VALID_TICK, "timestamp": future.isoformat().replace("+00:00", "Z")}
        )

    def test_valid_tick_without_timestamp_ingests(self):
        payload = {k: v for k, v in VALID_TICK.items() if k != "timestamp"}
        with _mock_tick_insert():
            resp = self._post(payload)
        self.assertEqual(resp.status_code, 201)


class TickErrorDisclosureTests(TickEndpointBase):
    """MEDIUM #15 — exception details must never reach the client."""

    def test_ingest_failure_returns_generic_message(self):
        with (
            mock.patch("forex.ticks_views.get_clickhouse_client"),
            mock.patch(
                "forex.ticks_views.insert_ticks_batch",
                side_effect=RuntimeError("secret-clickhouse-detail"),
            ),
        ):
            resp = self.client.post(
                "/api/ticks/",
                VALID_TICK,
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body, {"status": "error", "message": "Internal error"})
        self.assertNotIn("secret-clickhouse-detail", resp.content.decode())


class TradePermissionTests(TickEndpointBase):
    """HIGH #6 — trade execution requires staff or the service account."""

    def test_normal_user_gets_403(self):
        resp = self.client.post(
            "/api/trade/",
            TRADE_PAYLOAD,
            format="json",
            **self._auth_headers(self.access_token),
        )
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_gets_denied(self):
        # DRF semantics: no credentials at all -> 401 NotAuthenticated
        # (a logged-in normal user gets 403 instead).
        resp = self.client.post("/api/trade/", TRADE_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_staff_user_can_trade(self):
        with _mock_trade_pipeline():
            resp = self.client.post(
                "/api/trade/",
                TRADE_PAYLOAD,
                format="json",
                **self._auth_headers(self.staff_token),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")

    def test_service_account_can_trade(self):
        with _mock_trade_pipeline():
            resp = self.client.post(
                "/api/trade/",
                TRADE_PAYLOAD,
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")


class TradeCapTests(TickEndpointBase):
    """HIGH #6 — per-user daily trade/volume caps via the Django cache."""

    def test_volume_above_max_rejected(self):
        with mock.patch("forex.ticks_views.TRADE_MAX_VOLUME", 100000):
            resp = self.client.post(
                "/api/trade/",
                {**TRADE_PAYLOAD, "volume": 100001},
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
        self.assertEqual(resp.status_code, 400)

    def test_daily_trade_limit_enforced(self):
        with (
            mock.patch("forex.ticks_views.TRADE_DAILY_LIMIT", 2),
            _mock_trade_pipeline(),
        ):
            for _ in range(2):
                resp = self.client.post(
                    "/api/trade/",
                    TRADE_PAYLOAD,
                    format="json",
                    HTTP_X_API_KEY=TEST_API_KEY,
                )
                self.assertEqual(resp.status_code, 200)
            blocked = self.client.post(
                "/api/trade/",
                TRADE_PAYLOAD,
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
        self.assertEqual(blocked.status_code, 200)
        body = blocked.json()
        self.assertEqual(body["status"], "risk_blocked")
        self.assertIn("Daily trade limit reached", body["reason"])

    def test_daily_volume_limit_enforced(self):
        with (
            mock.patch("forex.ticks_views.TRADE_DAILY_VOLUME_LIMIT", 150),
            _mock_trade_pipeline(),
        ):
            first = self.client.post(
                "/api/trade/",
                {**TRADE_PAYLOAD, "volume": 100},
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
            self.assertEqual(first.status_code, 200)
            blocked = self.client.post(
                "/api/trade/",
                {**TRADE_PAYLOAD, "volume": 100},
                format="json",
                HTTP_X_API_KEY=TEST_API_KEY,
            )
        self.assertEqual(blocked.status_code, 200)
        body = blocked.json()
        self.assertEqual(body["status"], "risk_blocked")
        self.assertIn("Daily volume limit", body["reason"])

    def test_caps_are_per_user(self):
        """One staff user's quota must not block another staff user."""
        other_staff = User.objects.create_user(
            username="staff2@example.com",
            email="staff2@example.com",
            password="Str0ng!Passw0rd",
            is_staff=True,
        )
        other_token = str(RefreshToken.for_user(other_staff).access_token)
        with (
            mock.patch("forex.ticks_views.TRADE_DAILY_LIMIT", 1),
            _mock_trade_pipeline(),
        ):
            first = self.client.post(
                "/api/trade/",
                TRADE_PAYLOAD,
                format="json",
                **self._auth_headers(self.staff_token),
            )
            self.assertEqual(first.status_code, 200)
            # Different staff user, different cap bucket -> own quota remains.
            resp = self.client.post(
                "/api/trade/",
                TRADE_PAYLOAD,
                format="json",
                **self._auth_headers(other_token),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")
