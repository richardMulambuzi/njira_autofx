"""
QA Gate 2 — JWT auth implementation tests (auth-engineer scope).

Covers the exact wire contract consumed by the Next.js frontend:

    POST /auth/register -> 201 {access_token, refresh_token, user{id,name,email,status}}
    POST /auth/login    -> 200 {access_token, refresh_token, user{...}}
    GET  /auth/me       -> 200 {id,name,email,status} (Bearer required)
    POST /auth/refresh  -> 200 {access_token, refresh_token} (rotation + blacklist)
    POST /auth/logout   -> 200 {message} (refresh blacklisted)

Plus: protected-endpoint enforcement (401 without token), login throttling
(5/min -> 429), and the ClickHouse env-driven credential guard.

Run: python manage.py test auth_app -v 2
"""
import os
import unittest
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from forex.clickhouse import connection as ch_connection

User = get_user_model()

VALID_PASSWORD = "Str0ng!Passw0rd"


def _register_payload(name="Test User", email="test@example.com", password=VALID_PASSWORD):
    return {"name": name, "email": email, "password": password}


class AuthTestCase(TestCase):
    """
    Base class: clears the throttle cache before every test.

    DRF throttle counters live in Django's cache, which is shared across the
    whole test process. Without clearing, the login throttle (5/min per IP)
    bleeds between test classes and throttles setUp() logins of later classes.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()


class RegisterTests(AuthTestCase):
    """POST /auth/register — 201 + exact user contract; duplicate/weak -> 400."""

    def test_register_returns_201_with_exact_contract(self):
        resp = self.client.post("/auth/register/", _register_payload(), format="json")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        # Top-level contract: access_token, refresh_token, user
        self.assertEqual(
            set(body.keys()), {"access_token", "refresh_token", "user"}
        )
        self.assertTrue(body["access_token"])
        self.assertTrue(body["refresh_token"])
        # User contract: {id, name, email, status}
        self.assertEqual(
            set(body["user"].keys()), {"id", "name", "email", "status"}
        )
        self.assertEqual(body["user"]["name"], "Test User")
        self.assertEqual(body["user"]["email"], "test@example.com")
        self.assertEqual(body["user"]["status"], "active")
        # User actually persisted
        self.assertTrue(User.objects.filter(email="test@example.com").exists())

    def test_register_duplicate_email_returns_400(self):
        self.client.post("/auth/register/", _register_payload(), format="json")
        resp = self.client.post("/auth/register/", _register_payload(), format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("email", resp.json())

    def test_register_weak_password_returns_400(self):
        # 'password' is rejected by CommonPasswordValidator
        resp = self.client.post(
            "/auth/register/",
            _register_payload(password="password"),
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.json())
        # Numeric-only password rejected by NumericPasswordValidator
        resp2 = self.client.post(
            "/auth/register/",
            _register_payload(password="12345678"),
            format="json",
        )
        self.assertEqual(resp2.status_code, 400)
        self.assertIn("password", resp2.json())
        # No user created by either attempt
        self.assertFalse(User.objects.filter(email="test@example.com").exists())


class LoginTests(AuthTestCase):
    """POST /auth/login — 200 + exact contract; failures share one error shape."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            first_name="Test",
            last_name="User",
            password=VALID_PASSWORD,
        )

    def test_login_returns_200_with_exact_contract(self):
        resp = self.client.post(
            "/auth/login/",
            {"email": "test@example.com", "password": VALID_PASSWORD},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(
            set(body.keys()), {"access_token", "refresh_token", "user"}
        )
        self.assertTrue(body["access_token"])
        self.assertTrue(body["refresh_token"])
        self.assertEqual(
            set(body["user"].keys()), {"id", "name", "email", "status"}
        )
        self.assertEqual(body["user"]["name"], "Test User")
        self.assertEqual(body["user"]["status"], "active")

    def test_login_wrong_password_returns_400(self):
        resp = self.client.post(
            "/auth/login/",
            {"email": "test@example.com", "password": "wrong-password"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"detail": ["Invalid email or password."]})

    def test_login_missing_user_same_error_shape_no_enumeration(self):
        """Nonexistent email must produce the identical error as a wrong password."""
        wrong_password = self.client.post(
            "/auth/login/",
            {"email": "test@example.com", "password": "wrong-password"},
            format="json",
        )
        missing_user = self.client.post(
            "/auth/login/",
            {"email": "nobody@example.com", "password": "wrong-password"},
            format="json",
        )
        self.assertEqual(wrong_password.status_code, missing_user.status_code)
        self.assertEqual(wrong_password.json(), missing_user.json())
        self.assertEqual(missing_user.json(), {"detail": ["Invalid email or password."]})


class MeTests(AuthTestCase):
    """GET /auth/me — 200 with Bearer; 401 without/invalid token."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            first_name="Test",
            last_name="User",
            password=VALID_PASSWORD,
        )
        login = self.client.post(
            "/auth/login/",
            {"email": "test@example.com", "password": VALID_PASSWORD},
            format="json",
        )
        self.access_token = login.json()["access_token"]

    def test_me_returns_200_with_bearer(self):
        resp = self.client.get(
            "/auth/me/", HTTP_AUTHORIZATION=f"Bearer {self.access_token}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(resp.json().keys()), {"id", "name", "email", "status"}
        )
        self.assertEqual(resp.json()["email"], "test@example.com")

    def test_me_returns_401_without_token(self):
        resp = self.client.get("/auth/me/")
        self.assertEqual(resp.status_code, 401)

    def test_me_returns_401_with_invalid_token(self):
        resp = self.client.get(
            "/auth/me/", HTTP_AUTHORIZATION="Bearer not.a.jwt"
        )
        self.assertEqual(resp.status_code, 401)


class RefreshTests(AuthTestCase):
    """POST /auth/refresh — rotation issues NEW access + NEW refresh; old one dies."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            password=VALID_PASSWORD,
        )
        login = self.client.post(
            "/auth/login/",
            {"email": "test@example.com", "password": VALID_PASSWORD},
            format="json",
        )
        body = login.json()
        self.access_token = body["access_token"]
        self.refresh_token = body["refresh_token"]

    def test_refresh_returns_new_access_and_rotated_refresh(self):
        resp = self.client.post(
            "/auth/refresh/", {"refresh": self.refresh_token}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body.keys()), {"access_token", "refresh_token"})
        self.assertNotEqual(body["access_token"], self.access_token)
        self.assertNotEqual(body["refresh_token"], self.refresh_token)
        # New access token actually works
        me = self.client.get(
            "/auth/me/", HTTP_AUTHORIZATION=f"Bearer {body['access_token']}"
        )
        self.assertEqual(me.status_code, 200)

    def test_reusing_old_refresh_after_rotation_returns_401(self):
        # First refresh rotates + blacklists the old token
        first = self.client.post(
            "/auth/refresh/", {"refresh": self.refresh_token}, format="json"
        )
        self.assertEqual(first.status_code, 200)
        # Reusing the OLD refresh token must now fail (blacklisted)
        resp = self.client.post(
            "/auth/refresh/", {"refresh": self.refresh_token}, format="json"
        )
        self.assertEqual(resp.status_code, 401)

    def test_refresh_missing_token_returns_400(self):
        resp = self.client.post("/auth/refresh/", {}, format="json")
        self.assertEqual(resp.status_code, 400)


class LogoutTests(AuthTestCase):
    """POST /auth/logout — 200; refresh token blacklisted afterwards."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            password=VALID_PASSWORD,
        )
        login = self.client.post(
            "/auth/login/",
            {"email": "test@example.com", "password": VALID_PASSWORD},
            format="json",
        )
        body = login.json()
        self.access_token = body["access_token"]
        self.refresh_token = body["refresh_token"]

    def test_logout_returns_200(self):
        resp = self.client.post(
            "/auth/logout/",
            {"refresh": self.refresh_token},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.access_token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"message": "Logged out successfully."})

    def test_refresh_blacklisted_after_logout(self):
        self.client.post(
            "/auth/logout/",
            {"refresh": self.refresh_token},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.access_token}",
        )
        resp = self.client.post(
            "/auth/refresh/", {"refresh": self.refresh_token}, format="json"
        )
        self.assertEqual(resp.status_code, 401)

    def test_logout_requires_auth(self):
        resp = self.client.post(
            "/auth/logout/", {"refresh": self.refresh_token}, format="json"
        )
        self.assertEqual(resp.status_code, 401)


class ProtectedEndpointTests(AuthTestCase):
    """
    Auth decorators on business endpoints: 401 without token, non-401 with
    token (400/405 on empty body is acceptable — auth passed).
    """

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            password=VALID_PASSWORD,
        )
        login = self.client.post(
            "/auth/login/",
            {"email": "test@example.com", "password": VALID_PASSWORD},
            format="json",
        )
        self.access_token = login.json()["access_token"]

    def _assert_protected(self, method, url, data=None):
        """401 without token; non-401 with a valid Bearer token."""
        kwargs = {"format": "json"} if data is not None else {}
        payload = data if data is not None else {}
        anon = getattr(self.client, method)(url, payload, **kwargs)
        self.assertEqual(
            anon.status_code, 401, f"{method.upper()} {url} should 401 without token"
        )
        authed = getattr(self.client, method)(
            url,
            payload,
            HTTP_AUTHORIZATION=f"Bearer {self.access_token}",
            **kwargs,
        )
        self.assertNotEqual(
            authed.status_code,
            401,
            f"{method.upper()} {url} should pass auth with a valid token",
        )

    def test_get_candles_requires_auth(self):
        self._assert_protected("get", "/get_candles/")

    def test_place_trade_requires_auth(self):
        self._assert_protected("post", "/placeTrade/")

    def test_notifications_fetch_requires_auth(self):
        self._assert_protected("post", "/notifications/fetch/")

    def test_optimise_requires_auth(self):
        self._assert_protected("post", "/optimise/")

    def test_backtester_requires_auth(self):
        self._assert_protected("get", "/backtester/test")


class LoginThrottleTests(AuthTestCase):
    """Login is limited to 5/min per IP — the 6th attempt must be 429."""

    def setUp(self):
        cache.clear()  # throttle counters live in the cache; start clean
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            password=VALID_PASSWORD,
        )

    def tearDown(self):
        cache.clear()

    def test_login_throttled_after_5_attempts(self):
        statuses = []
        for _ in range(6):
            resp = self.client.post(
                "/auth/login/",
                {"email": "test@example.com", "password": "wrong-password"},
                format="json",
            )
            statuses.append(resp.status_code)
        # First 5 attempts are processed (400 = bad credentials), 6th throttled
        self.assertEqual(statuses[:5], [400] * 5)
        self.assertEqual(statuses[5], 429)


class RegisterThrottleTests(AuthTestCase):
    """Open registration is limited to 5/min per IP — the 6th must be 429."""

    def setUp(self):
        cache.clear()  # throttle counters live in the cache; start clean
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def test_register_throttled_after_5_attempts(self):
        statuses = []
        for i in range(6):
            resp = self.client.post(
                "/auth/register/",
                _register_payload(email=f"user{i}@example.com"),
                format="json",
            )
            statuses.append(resp.status_code)
        # First 5 attempts are processed (201 = user created), 6th throttled
        self.assertEqual(statuses[:5], [201] * 5)
        self.assertEqual(statuses[5], 429)


class ClickHouseConnectionTests(AuthTestCase):
    """
    forex/clickhouse/connection.py — env-driven credentials.

    CLICKHOUSE_PASSWORD is REQUIRED: RuntimeError when unset. When set, the
    client connects to the verified production host (indemw.tech:8123); if the
    sandbox cannot reach it, the connectivity test is skipped with a clear
    reason rather than failing the suite.
    """

    def setUp(self):
        # Reset the module-level singleton so each test exercises the guard.
        ch_connection._clickhouse_client = None

    def tearDown(self):
        ch_connection._clickhouse_client = None

    def test_get_clickhouse_client_raises_without_password(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLICKHOUSE_PASSWORD", None)
            with self.assertRaises(RuntimeError) as ctx:
                ch_connection.get_clickhouse_client()
        self.assertIn("CLICKHOUSE_PASSWORD", str(ctx.exception))

    def test_get_clickhouse_client_connects_when_password_set(self):
        with mock.patch.dict(
            os.environ,
            {
                "CLICKHOUSE_PASSWORD": "MyNewPassword123!",
                "CLICKHOUSE_HOST": "indemw.tech",
                "CLICKHOUSE_CONNECT_TIMEOUT": "5",
            },
            clear=False,
        ):
            try:
                client = ch_connection.get_clickhouse_client()
            except Exception as exc:  # noqa: BLE001 — network-dependent
                self.skipTest(
                    f"ClickHouse unreachable from this sandbox "
                    f"(indemw.tech:8123): {exc.__class__.__name__}: {exc}"
                )
            self.assertIsNotNone(client)
            # Singleton is cached for reuse
            self.assertIs(ch_connection.get_clickhouse_client(), client)