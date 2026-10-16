"""
QA Gate 2 — Pepperstone integration offline tests (no network, no reactor).

Covers forex/pepperstone/:

* client.py        — price/spread math (incl. JPY digits), credentials guard,
                     friendly error decoding, handshake state machine
                     (application auth -> account auth -> symbols list ->
                     symbol details -> on_ready), bad-token routing,
                     per-message routing, subscribe_spots.
* ticks_engine.py  — spot event -> queue conversion (real prices), ClickHouse
                     + HTTP flush, batching, drain, retry backoff, settings.
* execution_engine.py — order validation, reactor one-shot guard, timeout.

The live Pepperstone endpoint and Twisted reactor are NEVER started: the
client is mocked at build_client() and the executor's reactor worker is
mocked at _run_in_reactor().
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAErrorRes,
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListRes,
    ProtoOASpotEvent,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAPayloadType,
    ProtoOATrendbarPeriod,
)

from forex.broker.errors import BrokerErrorCode
from forex.pepperstone import client as ps_client
from forex.pepperstone import execution_engine as ps_exec
from forex.pepperstone import ticks_engine as ps_ticks
from forex.pepperstone.client import (
    PepperstoneSession,
    compute_real_price,
    compute_spread_pips,
    get_credentials,
    price_scale,
)
from forex.pepperstone.execution_engine import TradeExecutor
from forex.pepperstone.query_worker import QueryWorker
from forex.pepperstone.ticks_engine import TicksEngine
from forex.analysis.persistence_buffer import PersistenceBuffer
from forex.analysis.batch_writer import BatchWriter


class FakeMessage:
    """Minimal stand-in for the SDK's wrapped protobuf message."""

    def __init__(self, payload_type: int, payload: bytes = b""):
        self.payloadType = payload_type
        self.payload = payload


class FakeClient:
    """Records send() calls; never connects anywhere."""

    def __init__(self):
        self.sent = []

    def send(self, request):
        self.sent.append(request)
        return SimpleNamespace(addErrback=lambda cb: None)

    def setConnectedCallback(self, cb):
        self.on_connected = cb

    def setMessageReceivedCallback(self, cb):
        self.on_message = cb

    def setDisconnectedCallback(self, cb):
        self.on_disconnected = cb

    def startService(self):
        pass

    def stopService(self):
        pass


# ---------------------------------------------------------------------------
# Price / spread math
# ---------------------------------------------------------------------------
class PriceMathTests(SimpleTestCase):
    """compute_real_price / compute_spread_pips — incl. JPY digit handling."""

    def test_real_price_five_digit_symbol(self):
        # EURUSD raw 110500 with digits=5 -> 1.10500
        self.assertEqual(compute_real_price(110500, 5), 1.105)

    def test_real_price_jpy_three_digit_symbol(self):
        # USDJPY raw 155123 with digits=3 -> 155.123 (NOT 1.55123)
        self.assertEqual(compute_real_price(155123, 3), 155.123)

    def test_real_price_four_digit_symbol(self):
        self.assertEqual(compute_real_price(12345, 4), 1.2345)

    def test_real_price_negative_digits_guard(self):
        # max(digits, 0) prevents a ZeroDivisionError / negative exponent.
        self.assertEqual(compute_real_price(100, -2), 100.0)

    def test_spread_pips_five_digit_symbol(self):
        # (ask - bid) * 10000 — float representation, compare with tolerance.
        self.assertAlmostEqual(compute_spread_pips(1.1050, 1.1052, 5), 2.0, places=9)

    def test_spread_pips_jpy_three_digit_symbol(self):
        # (ask - bid) * 10 ** (3 - 4) = (ask - bid) / 10
        self.assertAlmostEqual(compute_spread_pips(155.123, 155.133, 3), 0.001, places=9)

    def test_spread_pips_four_digit_symbol(self):
        # (ask - bid) * 10 ** 0 = ask - bid
        self.assertAlmostEqual(compute_spread_pips(1.2345, 1.2355, 4), 0.001, places=9)

    def test_spread_pips_zero_spread(self):
        self.assertEqual(compute_spread_pips(1.1050, 1.1050, 5), 0.0)


class PriceScaleTests(SimpleTestCase):
    """price_scale / compute_real_price(symbol=...) — the 1000x metals fix.

    cTrader sends XAU/XAG with a raw fixed-point scaled by 1e5 while reporting
    small ``digits`` (2). Decoding as raw/10**digits produced prices 1000x too
    large. These pin the metals divisor AND prove FX majors are untouched.
    """

    def test_xau_raw_scales_to_real_price(self):
        # Real captured XAU tick: raw 419435000, digits=2 -> 4194.35
        self.assertEqual(price_scale("XAUUSD", 2), 100000)
        self.assertAlmostEqual(
            compute_real_price(419435000, 2, "XAUUSD"), 4194.35, places=6
        )

    def test_xag_raw_scales_to_real_price(self):
        self.assertEqual(price_scale("XAGUSD", 2), 100000)
        self.assertAlmostEqual(
            compute_real_price(41943500, 2, "XAGUSD"), 419.435, places=6
        )

    def test_metals_case_and_suffix_insensitive(self):
        for sym in ("xauusd", "XAUUSD", " XauUsd ", "XAUUSD.m"):
            self.assertEqual(price_scale(sym, 2), 100000, sym)

    def test_eur_pair_unchanged(self):
        # EURUSD raw 110500, digits=5 -> 1.105 (no 1e5 divisor)
        self.assertEqual(price_scale("EURUSD", 5), 10 ** 5)
        self.assertAlmostEqual(
            compute_real_price(110500, 5, "EURUSD"), 1.105, places=9
        )

    def test_jpy_pair_unchanged(self):
        # USDJPY raw 155123, digits=3 -> 155.123, NOT 0.155123
        self.assertEqual(price_scale("USDJPY", 3), 10 ** 3)
        self.assertAlmostEqual(
            compute_real_price(155123, 3, "USDJPY"), 155.123, places=9
        )

    def test_unknown_small_raw_uses_digits(self):
        # Sub-threshold raw must NOT trigger magnitude inference.
        self.assertEqual(price_scale("SPX500", 2, raw=2624950), 10 ** 2)

    def test_unknown_huge_raw_infers_metal_divisor(self):
        # New metal symbol not in the XAU*/XAG* list, but raw > 10M -> 1e5.
        self.assertEqual(price_scale("COPPER", 2, raw=419435000), 100000)

    def test_symbol_none_keeps_legacy_digits_path(self):
        # Back-compat: no symbol -> raw / 10**digits, never 1e5.
        self.assertEqual(compute_real_price(110500, 5), 1.105)
        self.assertEqual(compute_real_price(155123, 3), 155.123)


class QueryWorkerTimeoutTests(SimpleTestCase):
    """QueryWorker rejects sub-second timeouts instead of silently clamping."""

    def test_timeout_below_one_second_raises(self):
        with self.assertRaises(ValueError) as ctx:
            QueryWorker("get_ticks_range", {}, timeout=0.5)
        self.assertIn("timeout", str(ctx.exception))

    def test_zero_timeout_raises(self):
        with self.assertRaises(ValueError):
            QueryWorker("get_ticks_range", {}, timeout=0)

    def test_previously_clamped_value_now_raises(self):
        # Old code did max(1.0, ...) -> 0.5 silently became 1.0.
        with self.assertRaises(ValueError):
            QueryWorker("get_ticks_range", {}, timeout=0.99)

    def test_exactly_one_second_is_accepted(self):
        # Boundary: 1.0 is valid. Must not raise ValueError (credentials may).
        with mock.patch(
            "forex.pepperstone.query_worker.get_credentials",
            return_value={"account_id": 5327660},
        ):
            worker = QueryWorker("get_ticks_range", {}, timeout=1.0)
        self.assertEqual(worker.timeout, 1.0)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
# Every Pepperstone env var get_credentials() reads. Scrubbed in setUp so the
# developer's real .env values (loaded into os.environ by forex/__init__) can
# never leak into a test result — real tokens in assertion diffs = secret in CI logs.
_PS_ENV_KEYS = (
    "PEPPERSTONE_CLIENT_ID",
    "PEPPERSTONE_CLIENT_SECRET",
    "PEPPERSTONE_ACCESS_TOKEN",
    "PEPPERSTONE_REFRESH_TOKEN",
    "PEPPERSTONE_ACCOUNT_ID",
    "PEPPERSTONE_TOKEN_URL",
    "PEPPERSTONE_ENV_PATH",
)


class CredentialsTests(SimpleTestCase):
    """get_credentials — env-driven, never hardcoded, names-only errors."""

    def setUp(self):
        super().setUp()
        self._saved_env = {k: os.environ[k] for k in _PS_ENV_KEYS if k in os.environ}
        for k in _PS_ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in _PS_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)
        super().tearDown()

    def test_missing_credentials_raise_with_env_names(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "",
                "PEPPERSTONE_CLIENT_SECRET": "",
                "PEPPERSTONE_ACCESS_TOKEN": "",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                get_credentials()
        message = str(ctx.exception)
        self.assertIn("PEPPERSTONE_CLIENT_ID", message)
        self.assertIn("PEPPERSTONE_CLIENT_SECRET", message)
        self.assertIn("PEPPERSTONE_ACCESS_TOKEN", message)
        # Secret VALUES must never appear in the error.
        self.assertNotIn("super-secret", message)

    def test_partial_missing_credentials(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "client-1",
                "PEPPERSTONE_CLIENT_SECRET": "secret-1",
                "PEPPERSTONE_ACCESS_TOKEN": "",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                get_credentials()
        self.assertIn("PEPPERSTONE_ACCESS_TOKEN", str(ctx.exception))
        self.assertNotIn("PEPPERSTONE_CLIENT_ID", str(ctx.exception))

    def test_valid_credentials_returned_with_default_account(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "client-1",
                "PEPPERSTONE_CLIENT_SECRET": "secret-1",
                "PEPPERSTONE_ACCESS_TOKEN": "token-1",
            },
            clear=False,
        ):
            # Explicitly unset so a developer shell export can't leak in.
            os.environ.pop("PEPPERSTONE_ACCOUNT_ID", None)
            creds = get_credentials()
        self.assertEqual(
            creds,
            {
                "client_id": "client-1",
                "client_secret": "secret-1",
                "access_token": "token-1",
                "account_id": 5327660,
            },
        )

    def test_account_id_from_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "c",
                "PEPPERSTONE_CLIENT_SECRET": "s",
                "PEPPERSTONE_ACCESS_TOKEN": "t",
                "PEPPERSTONE_ACCOUNT_ID": "12345",
            },
            clear=False,
        ):
            self.assertEqual(get_credentials()["account_id"], 12345)

    def test_non_integer_account_id_raises(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "c",
                "PEPPERSTONE_CLIENT_SECRET": "s",
                "PEPPERSTONE_ACCESS_TOKEN": "t",
                "PEPPERSTONE_ACCOUNT_ID": "not-a-number",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                get_credentials()
        self.assertIn("PEPPERSTONE_ACCOUNT_ID", str(ctx.exception))

    def test_refresh_token_omitted_when_unset(self):
        """Back-compat: existing callers get the 4-key dict unchanged."""
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "c",
                "PEPPERSTONE_CLIENT_SECRET": "s",
                "PEPPERSTONE_ACCESS_TOKEN": "t",
            },
            clear=False,
        ):
            creds = get_credentials()
        self.assertNotIn("refresh_token", creds)

    def test_refresh_token_included_when_set(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "c",
                "PEPPERSTONE_CLIENT_SECRET": "s",
                "PEPPERSTONE_ACCESS_TOKEN": "t",
                "PEPPERSTONE_REFRESH_TOKEN": "r-secret",
            },
            clear=False,
        ):
            creds = get_credentials()
        self.assertEqual(creds["refresh_token"], "r-secret")

    def test_refresh_token_whitespace_only_is_treated_as_unset(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "c",
                "PEPPERSTONE_CLIENT_SECRET": "s",
                "PEPPERSTONE_ACCESS_TOKEN": "t",
                "PEPPERSTONE_REFRESH_TOKEN": "   ",
            },
            clear=False,
        ):
            creds = get_credentials()
        self.assertNotIn("refresh_token", creds)


class TokenRefreshTests(SimpleTestCase):
    """refresh_access_token — mocked HTTP, no network, no real token rotation."""

    def setUp(self):
        super().setUp()
        self._saved_env = {k: os.environ[k] for k in _PS_ENV_KEYS if k in os.environ}
        for k in _PS_ENV_KEYS:
            os.environ.pop(k, None)
        # CRITICAL: belt-and-braces sandbox — refresh_access_token() defaults
        # to persist=False (in-memory os.environ only), but the persist=True
        # path resolves PEPPERSTONE_ENV_PATH. Redirect it to a throwaway file
        # so a test run can never overwrite real credentials on disk.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.env_path = Path(self._tmpdir.name) / "test.env"
        os.environ["PEPPERSTONE_ENV_PATH"] = str(self.env_path)

    def tearDown(self):
        for k in _PS_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)
        super().tearDown()

    @staticmethod
    def _response(status=200, payload=None):
        resp = mock.Mock()
        resp.status_code = status
        resp.json.return_value = payload if payload is not None else {}
        return resp

    def test_successful_refresh_returns_and_persists_tokens(self):
        resp = self._response(
            payload={"accessToken": "new-access", "refreshToken": "new-refresh"}
        )
        with mock.patch("requests.post", return_value=resp) as post:
            new_access = ps_client.refresh_access_token(
                "cid", "csecret", "old-refresh", persist=False
            )
        self.assertEqual(new_access, "new-access")
        self.assertEqual(os.environ["PEPPERSTONE_ACCESS_TOKEN"], "new-access")
        self.assertEqual(os.environ["PEPPERSTONE_REFRESH_TOKEN"], "new-refresh")
        # persist=False default: in-memory only — no disk write even with
        # PEPPERSTONE_ENV_PATH sandboxed.
        self.assertFalse(self.env_path.exists())
        # Correct OAuth grant params sent to the token endpoint.
        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            post.call_args.kwargs["params"],
            {
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh",
                "client_id": "cid",
                "client_secret": "csecret",
            },
        )

    def test_refresh_failure_raises_and_names_only_env_vars(self):
        resp = self._response(
            status=401, payload={"errorCode": "invalid_grant"}
        )
        with mock.patch("requests.post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                ps_client.refresh_access_token(
                    "cid", "csecret", "bad-refresh", persist=False
                )
        message = str(ctx.exception)
        self.assertIn("invalid_grant", message)
        # Never the secret VALUES.
        self.assertNotIn("csecret", message)
        self.assertNotIn("bad-refresh", message)

    def test_network_error_surfaces_as_runtime_error_without_secrets(self):
        with mock.patch("requests.post", side_effect=OSError("boom")):
            with self.assertRaises(RuntimeError) as ctx:
                ps_client.refresh_access_token(
                    "cid", "csecret", "old-refresh", persist=False
                )
        message = str(ctx.exception)
        self.assertIn("network error", message)
        self.assertNotIn("csecret", message)
        self.assertNotIn("old-refresh", message)

    def test_non_json_response_raises_without_secrets(self):
        resp = mock.Mock()
        resp.status_code = 502
        resp.json.side_effect = ValueError("not json")
        with mock.patch("requests.post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                ps_client.refresh_access_token(
                    "cid", "csecret", "old-refresh", persist=False
                )
        self.assertNotIn("csecret", str(ctx.exception))
        self.assertNotIn("old-refresh", str(ctx.exception))

    def test_tokens_persist_to_sandbox_file_never_project_env(self):
        """persist=True writes to PEPPERSTONE_ENV_PATH, not the real .env."""
        resp = self._response(
            payload={"accessToken": "new-access", "refreshToken": "new-refresh"}
        )
        with mock.patch("requests.post", return_value=resp):
            ps_client.refresh_access_token(
                "cid", "csecret", "old-refresh", persist=True
            )

        written = self.env_path.read_text()
        self.assertIn("PEPPERSTONE_ACCESS_TOKEN=new-access", written)
        self.assertIn("PEPPERSTONE_REFRESH_TOKEN=new-refresh", written)
        # Backup uses explicit ".bak" suffix — never ".env.env.bak".
        self.assertFalse(Path(f"{self.env_path}.env.bak").exists())

        # The real project .env must be byte-identical (no fixture tokens).
        project_env = Path(ps_client.__file__).resolve().parents[2] / ".env"
        if project_env.is_file():
            self.assertNotIn(
                "PEPPERSTONE_ACCESS_TOKEN=new-access", project_env.read_text()
            )

    def test_default_is_memory_only_no_disk_write(self):
        """Default persist=False: os.environ updated, no file created."""
        resp = self._response(
            payload={"accessToken": "mem-access", "refreshToken": "mem-refresh"}
        )
        with mock.patch("requests.post", return_value=resp):
            ps_client.refresh_access_token("cid", "csecret", "old-refresh")
        self.assertEqual(os.environ["PEPPERSTONE_ACCESS_TOKEN"], "mem-access")
        self.assertFalse(self.env_path.exists())

    def test_refresh_logs_never_contain_secret_values(self):
        """The success path logs names/steps only — never the token values."""
        resp = self._response(
            payload={"accessToken": "new-access", "refreshToken": "new-refresh"}
        )
        with self.assertLogs("forex.pepperstone.client", level="DEBUG") as logs:
            with mock.patch("requests.post", return_value=resp):
                ps_client.refresh_access_token(
                    "super-secret-client",
                    "super-secret-refresh",
                    "old",
                    persist=False,
                )
        blob = "\n".join(logs.output)
        self.assertNotIn("super-secret-client", blob)
        self.assertNotIn("super-secret-refresh", blob)
        self.assertNotIn("new-access", blob)
        self.assertNotIn("new-refresh", blob)


class FriendlyErrorTests(SimpleTestCase):
    """_friendly_error_message — known codes get hints, unknown pass through."""

    def test_known_code_gets_hint(self):
        message = ps_client._friendly_error_message(
            "CH_ACCESS_TOKEN_INVALID", "token rejected"
        )
        self.assertIn("CH_ACCESS_TOKEN_INVALID", message)
        self.assertIn("check PEPPERSTONE_ACCESS_TOKEN", message)

    def test_unknown_code_passes_through_verbatim(self):
        message = ps_client._friendly_error_message("SOME_NEW_CODE", "weird")
        self.assertIn("SOME_NEW_CODE", message)
        self.assertIn("weird", message)

    def test_numeric_code_mapped_to_enum_name(self):
        # ProtoOAErrorCode values are ints; the label lookup must not crash.
        message = ps_client._friendly_error_message("1", "desc")
        self.assertIn("desc", message)


# ---------------------------------------------------------------------------
# Handshake state machine
# ---------------------------------------------------------------------------
class HandshakeStateMachineTests(SimpleTestCase):
    """PepperstoneSession — auth chain, symbol resolution, error routing."""

    CREDENTIALS = {
        "client_id": "client-1",
        "client_secret": "secret-1",
        "access_token": "token-1",
        "account_id": 5327660,
    }

    def setUp(self):
        self.fake_client = FakeClient()
        self.ready_calls = []
        self.message_calls = []
        self.error_calls = []
        self.build_patch = mock.patch(
            "forex.pepperstone.client.build_client",
            return_value=self.fake_client,
        )
        self.build_patch.start()
        self.session = PepperstoneSession(
            self.CREDENTIALS,
            symbols=["EURUSD", "USDJPY"],
            on_ready=self.ready_calls.append,
            on_message=lambda c, m: self.message_calls.append(m),
            on_error=self.error_calls.append,
        )

    def tearDown(self):
        self.build_patch.stop()

    def _symbols_list_payload(self, pairs):
        res = ProtoOASymbolsListRes()
        res.ctidTraderAccountId = 5327660
        for symbol_id, name in pairs:
            light = res.symbol.add()
            light.symbolId = symbol_id
            light.symbolName = name
        return res.SerializeToString()

    def _symbol_by_id_payload(self, pairs):
        res = ProtoOASymbolByIdRes()
        res.ctidTraderAccountId = 5327660
        for symbol_id, digits in pairs:
            sym = res.symbol.add()
            sym.symbolId = symbol_id
            sym.digits = digits
            sym.pipPosition = 1  # required protobuf field
        return res.SerializeToString()

    def test_on_connected_sends_application_auth(self):
        self.session._on_connected(self.fake_client)
        self.assertEqual(len(self.fake_client.sent), 1)
        req = self.fake_client.sent[0]
        self.assertEqual(req.clientId, "client-1")
        self.assertEqual(req.clientSecret, "secret-1")

    def test_application_auth_res_sends_account_auth(self):
        self.session._on_message(
            self.fake_client,
            FakeMessage(ProtoOAPayloadType.PROTO_OA_APPLICATION_AUTH_RES),
        )
        self.assertEqual(len(self.fake_client.sent), 1)
        req = self.fake_client.sent[0]
        self.assertEqual(req.ctidTraderAccountId, 5327660)
        self.assertEqual(req.accessToken, "token-1")

    def test_account_auth_res_sends_symbols_list(self):
        self.session._on_message(
            self.fake_client,
            FakeMessage(ProtoOAPayloadType.PROTO_OA_ACCOUNT_AUTH_RES),
        )
        self.assertEqual(len(self.fake_client.sent), 1)
        req = self.fake_client.sent[0]
        self.assertEqual(req.ctidTraderAccountId, 5327660)
        self.assertFalse(req.includeArchivedSymbols)

    def test_symbols_list_res_requests_details_for_wanted_symbols(self):
        payload = self._symbols_list_payload(
            [(1001, "EURUSD"), (1002, "USDJPY"), (1003, "GBPUSD")]
        )
        self.session._on_message(
            self.fake_client,
            FakeMessage(ProtoOAPayloadType.PROTO_OA_SYMBOLS_LIST_RES, payload),
        )
        self.assertEqual(len(self.fake_client.sent), 1)
        req = self.fake_client.sent[0]
        self.assertEqual(sorted(req.symbolId), [1001, 1002])  # GBPUSD filtered

    def test_symbol_by_id_res_builds_map_and_calls_on_ready(self):
        self.session._light_symbol_names = {
            1001: "EURUSD",
            1002: "USDJPY",
        }
        payload = self._symbol_by_id_payload([(1001, 5), (1002, 3)])
        self.session._on_message(
            self.fake_client,
            FakeMessage(ProtoOAPayloadType.PROTO_OA_SYMBOL_BY_ID_RES, payload),
        )
        self.assertEqual(
            self.session.symbols_map, {"EURUSD": (1001, 5), "USDJPY": (1002, 3)}
        )
        self.assertEqual(
            self.session.symbols_by_id, {1001: ("EURUSD", 5), 1002: ("USDJPY", 3)}
        )
        self.assertTrue(self.session._symbols_loaded)
        self.assertEqual(self.ready_calls, [{"EURUSD": (1001, 5), "USDJPY": (1002, 3)}])

    def test_bad_token_error_routes_to_on_error_with_hint(self):
        err = ProtoOAErrorRes()
        err.errorCode = "CH_ACCESS_TOKEN_INVALID"
        err.description = "token rejected"
        self.session._on_message(
            self.fake_client,
            FakeMessage(ProtoOAPayloadType.PROTO_OA_ERROR_RES, err.SerializeToString()),
        )
        self.assertEqual(len(self.error_calls), 1)
        self.assertIn("CH_ACCESS_TOKEN_INVALID", self.error_calls[0])
        self.assertIn("check PEPPERSTONE_ACCESS_TOKEN", self.error_calls[0])

    # -- single refresh + single retry ----------------------------------
    def _token_error(self):
        err = ProtoOAErrorRes()
        err.errorCode = "OA_AUTH_TOKEN_EXPIRED"
        err.description = "expired"
        return FakeMessage(
            ProtoOAPayloadType.PROTO_OA_ERROR_RES, err.SerializeToString()
        )

    def test_expired_token_refreshes_once_and_retries_account_auth(self):
        self.session.credentials["refresh_token"] = "r-secret"
        self.fake_client.sent.clear()
        with mock.patch(
            "forex.pepperstone.client.refresh_access_token",
            return_value="fresh-access",
        ) as refresh:
            self.session._on_message(self.fake_client, self._token_error())

        refresh.assert_called_once()
        self.assertEqual(
            refresh.call_args.args, ("client-1", "secret-1", "r-secret")
        )
        # Live session persists rotation to disk (sandboxed via
        # PEPPERSTONE_ENV_PATH); direct calls default to persist=False.
        self.assertTrue(refresh.call_args.kwargs.get("persist") is True)
        # Exactly ONE retry sent, carrying the NEW access token.
        self.assertEqual(len(self.fake_client.sent), 1)
        retry = self.fake_client.sent[0]
        self.assertEqual(retry.accessToken, "fresh-access")
        self.assertEqual(self.session.credentials["access_token"], "fresh-access")
        # The retry is in flight: no fatal error surfaced yet.
        self.assertEqual(self.error_calls, [])

    def test_second_expired_token_does_not_refresh_again(self):
        self.session.credentials["refresh_token"] = "r-secret"
        with mock.patch(
            "forex.pepperstone.client.refresh_access_token",
            return_value="fresh-access",
        ) as refresh:
            self.session._on_message(self.fake_client, self._token_error())
            self.fake_client.sent.clear()
            # Second expiry must NOT trigger another refresh.
            self.session._on_message(self.fake_client, self._token_error())

        refresh.assert_called_once()  # single refresh, ever
        self.assertEqual(self.fake_client.sent, [])  # no second retry
        self.assertEqual(len(self.error_calls), 1)  # fatal, surfaced

    def test_missing_refresh_token_fails_fast_naming_the_env_var(self):
        self.session.credentials.pop("refresh_token", None)
        os.environ.pop("PEPPERSTONE_REFRESH_TOKEN", None)
        with self.assertLogs("forex.pepperstone.client", level="DEBUG") as logs:
            with mock.patch(
                "forex.pepperstone.client.refresh_access_token"
            ) as refresh:
                self.session._on_message(self.fake_client, self._token_error())
        refresh.assert_not_called()  # never attempt a refresh without a token
        self.assertEqual(self.fake_client.sent, [])  # no retry sent
        self.assertEqual(len(self.error_calls), 1)
        self.assertIn("OA_AUTH_TOKEN_EXPIRED", self.error_calls[0])
        # Fails fast with the KEY NAME so the operator knows what to set.
        self.assertIn("PEPPERSTONE_REFRESH_TOKEN", "\n".join(logs.output))

    def test_refresh_failure_surfaces_original_error_not_a_retry_loop(self):
        self.session.credentials["refresh_token"] = "r-secret"
        self.fake_client.sent.clear()
        with mock.patch(
            "forex.pepperstone.client.refresh_access_token",
            side_effect=RuntimeError("cTrader token refresh failed: invalid_grant"),
        ) as refresh:
            self.session._on_message(self.fake_client, self._token_error())

        refresh.assert_called_once()
        self.assertEqual(self.fake_client.sent, [])  # no retry on failure
        self.assertEqual(len(self.error_calls), 1)  # original error surfaced

    def test_refresh_error_message_never_contains_secret_values(self):
        """A rejected refresh must not leak the token into logs/errors."""
        self.session.credentials["refresh_token"] = "super-secret-refresh"
        self.session.credentials["client_secret"] = "super-secret-client"
        with self.assertLogs("forex.pepperstone.client", level="DEBUG") as logs:
            with mock.patch(
                "forex.pepperstone.client.refresh_access_token",
                side_effect=RuntimeError(
                    "cTrader token refresh failed: invalid_grant"
                ),
            ):
                self.session._on_message(self.fake_client, self._token_error())
        blob = "\n".join(logs.output) + "\n".join(self.error_calls)
        self.assertNotIn("super-secret-refresh", blob)
        self.assertNotIn("super-secret-client", blob)

    def test_unknown_payload_forwarded_to_on_message(self):
        spot = ProtoOASpotEvent()
        spot.ctidTraderAccountId = 5327660
        spot.symbolId = 1001
        spot.bid = 110500
        spot.ask = 110520
        spot.timestamp = 1750000000000
        self.session._on_message(
            self.fake_client,
            FakeMessage(ProtoOAPayloadType.PROTO_OA_SPOT_EVENT, spot.SerializeToString()),
        )
        self.assertEqual(len(self.message_calls), 1)
        self.assertEqual(self.message_calls[0].payloadType, ProtoOAPayloadType.PROTO_OA_SPOT_EVENT)

    def test_no_matching_symbols_fails_with_clear_message(self):
        payload = self._symbols_list_payload([(1003, "GBPUSD")])
        self.session._on_message(
            self.fake_client,
            FakeMessage(ProtoOAPayloadType.PROTO_OA_SYMBOLS_LIST_RES, payload),
        )
        self.assertEqual(len(self.error_calls), 1)
        self.assertIn("No symbols available", self.error_calls[0])
        self.assertEqual(self.fake_client.sent, [])  # no detail request sent

    def test_subscribe_spots_sends_request(self):
        self.session.subscribe_spots([1001, 1002])
        self.assertEqual(len(self.fake_client.sent), 1)
        req = self.fake_client.sent[0]
        self.assertEqual(req.ctidTraderAccountId, 5327660)
        self.assertEqual(list(req.symbolId), [1001, 1002])

    def test_subscribe_spots_empty_list_is_noop(self):
        self.session.subscribe_spots([])
        self.assertEqual(self.fake_client.sent, [])

    def test_disconnect_resets_auth_flags_and_notifies(self):
        disconnected = []
        self.session.on_disconnected = lambda c, r: disconnected.append(r)
        self.session._authenticated = True
        self.session._symbols_loaded = True
        self.session._on_disconnected(self.fake_client, "reason")
        self.assertFalse(self.session._authenticated)
        self.assertFalse(self.session._symbols_loaded)
        self.assertEqual(disconnected, ["reason"])


# ---------------------------------------------------------------------------
# TicksEngine — spot conversion, flush, batching, backoff
# ---------------------------------------------------------------------------
class TicksEngineSpotTests(SimpleTestCase):
    """Spot event -> queue conversion with real prices."""

    def setUp(self):
        self.engine = TicksEngine(symbols=["EURUSD", "USDJPY"])
        self.engine.session = SimpleNamespace(
            symbols_by_id={1001: ("EURUSD", 5), 1002: ("USDJPY", 3)}
        )

    def _spot(self, symbol_id, bid, ask, ts=1750000000000):
        spot = ProtoOASpotEvent()
        spot.ctidTraderAccountId = 5327660
        spot.symbolId = symbol_id
        spot.bid = bid
        spot.ask = ask
        spot.timestamp = ts
        return FakeMessage(
            ProtoOAPayloadType.PROTO_OA_SPOT_EVENT, spot.SerializeToString()
        )

    def test_spot_event_converted_to_real_prices(self):
        self.engine._on_message(None, self._spot(1001, 110500, 110520))
        item = self.engine.buffer.drain_batch(1)[0]
        symbol, bid, ask, spread, digits, ts = item
        self.assertEqual(symbol, "EURUSD")
        self.assertEqual(bid, 1.105)
        self.assertEqual(ask, 1.1052)
        self.assertAlmostEqual(spread, 2.0, places=9)
        self.assertEqual(digits, 5)
        self.assertEqual(ts, 1750000000000)

    def test_jpy_spot_event_uses_three_digit_scaling(self):
        self.engine._on_message(None, self._spot(1002, 155123, 155133))
        symbol, bid, ask, spread, digits, _ = self.engine.buffer.drain_batch(1)[0]
        self.assertEqual(symbol, "USDJPY")
        self.assertEqual(bid, 155.123)
        self.assertEqual(ask, 155.133)
        self.assertAlmostEqual(spread, 0.001, places=9)
        self.assertEqual(digits, 3)

    def test_unknown_symbol_id_ignored(self):
        self.engine._on_message(None, self._spot(9999, 1, 2))
        self.assertEqual(self.engine.buffer.get_metrics()["buffer_size"], 0)

    def test_non_spot_payload_ignored(self):
        self.engine._on_message(None, FakeMessage(ProtoOAPayloadType.PROTO_OA_EXECUTION_EVENT))
        self.assertEqual(self.engine.buffer.get_metrics()["buffer_size"], 0)

    def test_malformed_spot_payload_ignored(self):
        # payload bytes that do not parse as a spot event must not crash.
        self.engine._on_message(
            None, FakeMessage(ProtoOAPayloadType.PROTO_OA_SPOT_EVENT, b"\xff\xff\xff")
        )
        self.assertEqual(self.engine.buffer.get_metrics()["buffer_size"], 0)


class TicksEngineFlushTests(SimpleTestCase):
    """BatchWriter.flush_now — ClickHouse insert + optional HTTP sink, non-fatal."""

    ROWS = [
        ("EURUSD", 1.105, 1.1052, 2.0, 5, 1750000000000),
        ("USDJPY", 155.123, 155.133, 0.001, 3, 1750000001000),
    ]

    def setUp(self):
        self.engine = TicksEngine(symbols=["EURUSD"], ingest_url="http://ingest.test/ticks")
        self.engine._ch_client = SimpleNamespace()
        self.engine.writer = BatchWriter(
            self.engine.buffer,
            self.engine._ch_client,
            ingest_url=self.engine.ingest_url,
        )

    def test_flush_inserts_to_clickhouse_and_posts_http(self):
        with (
            mock.patch(
                "forex.clickhouse.schema.insert_ticks_batch"
            ) as m_insert,
            mock.patch("forex.pepperstone.ticks_engine.requests.post") as m_post,
        ):
            m_post.return_value.status_code = 200
            self.engine.writer.flush_now(self.ROWS)

        m_insert.assert_called_once_with(self.engine._ch_client, self.ROWS)
        m_post.assert_called_once()
        args, kwargs = m_post.call_args
        self.assertEqual(args[0], "http://ingest.test/ticks")
        self.assertEqual(kwargs["timeout"], 2)
        payload = kwargs["json"]
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["symbol"], "EURUSD")
        self.assertEqual(payload[0]["bid"], 1.105)
        self.assertEqual(payload[0]["timestamp"], 1750000000000)

    def test_clickhouse_failure_does_not_drop_http_sink(self):
        with (
            mock.patch(
                "forex.clickhouse.schema.insert_ticks_batch",
                side_effect=RuntimeError("ch down"),
            ),
            mock.patch("forex.pepperstone.ticks_engine.requests.post") as m_post,
        ):
            m_post.return_value.status_code = 200
            self.engine.writer.flush_now(self.ROWS)  # must not raise
        m_post.assert_called_once()

    def test_http_failure_is_non_fatal(self):
        with (
            mock.patch("forex.clickhouse.schema.insert_ticks_batch"),
            mock.patch(
                "forex.pepperstone.ticks_engine.requests.post",
                side_effect=RuntimeError("http down"),
            ),
        ):
            self.engine.writer.flush_now(self.ROWS)  # must not raise

    def test_http_non_2xx_logs_warning(self):
        with (
            mock.patch("forex.clickhouse.schema.insert_ticks_batch"),
            mock.patch("forex.pepperstone.ticks_engine.requests.post") as m_post,
        ):
            m_post.return_value.status_code = 500
            self.engine.writer.flush_now(self.ROWS)  # must not raise

    def test_empty_rows_are_noop(self):
        with (
            mock.patch("forex.clickhouse.schema.insert_ticks_batch") as m_insert,
            mock.patch("forex.pepperstone.ticks_engine.requests.post") as m_post,
        ):
            self.engine.writer.flush_now([])
        m_insert.assert_not_called()
        m_post.assert_not_called()

    def test_drain_and_flush_empties_buffer(self):
        for row in self.ROWS:
            self.engine.buffer.put(*row)
        remaining = self.engine.buffer.flush_on_shutdown()
        self.assertEqual(remaining, self.ROWS)
        self.assertEqual(self.engine.buffer.get_metrics()["buffer_size"], 0)


class TicksEngineBackoffTests(SimpleTestCase):
    """_retry_policy — exponential backoff, give-up after MAX attempts."""

    def setUp(self):
        self.engine = TicksEngine(symbols=["EURUSD"])

    def test_backoff_doubles_each_attempt(self):
        expected = [0.5, 1.0, 2.0, 4.0, 8.0]
        for attempt, delay in enumerate(expected, start=1):
            self.assertEqual(self.engine._retry_policy(attempt), delay)

    def test_give_up_after_max_attempts(self):
        delay = self.engine._retry_policy(ps_ticks.MAX_RECONNECT_ATTEMPTS + 1)
        self.assertEqual(delay, 30.0)
        self.assertEqual(self.engine.exit_code, 1)
        self.assertTrue(self.engine.shutdown_event.is_set())

    def test_backoff_capped_at_30_seconds(self):
        # attempts beyond the give-up point return the cap (reactor stopping).
        self.assertEqual(self.engine._retry_policy(10), 30.0)


class TicksEngineSettingsTests(SimpleTestCase):
    """load_settings — env parsing with sane defaults and clamps."""

    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            settings = ps_ticks.load_settings()
        self.assertEqual(settings["symbols"], ["EURUSD"])
        self.assertEqual(settings["batch_size"], 100)
        self.assertEqual(settings["flush_seconds"], 1.0)
        self.assertIsNone(settings["ingest_url"])

    def test_env_parsing_and_clamps(self):
        with mock.patch.dict(
            os.environ,
            {
                "PEPPERSTONE_SYMBOLS": " eurusd , usdjpy , ",
                "TICKS_BATCH_SIZE": "0",  # clamped to 1
                "TICKS_FLUSH_SECONDS": "0.01",  # clamped to 0.1
                "TICKS_INGEST_URL": "http://ingest.test/ticks",
            },
            clear=False,
        ):
            settings = ps_ticks.load_settings()
        self.assertEqual(settings["symbols"], ["EURUSD", "USDJPY"])
        self.assertEqual(settings["batch_size"], 1)
        self.assertEqual(settings["flush_seconds"], 0.1)
        self.assertEqual(settings["ingest_url"], "http://ingest.test/ticks")

    def test_invalid_numbers_fall_back_to_defaults(self):
        with mock.patch.dict(
            os.environ,
            {"TICKS_BATCH_SIZE": "abc", "TICKS_FLUSH_SECONDS": "xyz"},
            clear=False,
        ):
            settings = ps_ticks.load_settings()
        self.assertEqual(settings["batch_size"], 100)
        self.assertEqual(settings["flush_seconds"], 1.0)


class TicksEngineReadyTests(SimpleTestCase):
    """_on_ready — subscribes to wanted symbols, errors when none found."""

    def setUp(self):
        self.engine = TicksEngine(symbols=["EURUSD", "GBPUSD"])
        self.engine.session = SimpleNamespace(
            subscribe_spots=mock.Mock(),
            symbols_map={"EURUSD": (1001, 5), "GBPUSD": (1003, 5)},
            symbols_by_id={1001: ("EURUSD", 5), 1003: ("GBPUSD", 5)},
        )

    def test_subscribes_to_available_symbols(self):
        self.engine._on_ready({"EURUSD": (1001, 5), "USDJPY": (1002, 3)})
        # Engine subscribes on handshake AND again for user symbols.
        self.engine.session.subscribe_spots.assert_any_call([1001])
        self.assertEqual(self.engine._disconnects, 0)

    def test_no_available_symbols_triggers_error(self):
        with mock.patch.object(self.engine, "_on_error") as m_error:
            self.engine._on_ready({"USDJPY": (1002, 3)})
        m_error.assert_called_once()
        self.assertIn("No configured symbols are available", m_error.call_args[0][0])
        self.engine.session.subscribe_spots.assert_not_called()


# ---------------------------------------------------------------------------
# TradeExecutor — validation + reactor one-shot guard
# ---------------------------------------------------------------------------
class TradeExecutorValidationTests(SimpleTestCase):
    """execute_order — invalid inputs rejected before any thread starts."""

    def setUp(self):
        self.executor = TradeExecutor(timeout=1.0)

    def test_invalid_side_rejected(self):
        result = self.executor.execute_order("EURUSD", "HODL", 100)
        self.assertEqual(result["status"], "error")
        self.assertIn("side must be 'BUY' or 'SELL'", result["message"])

    def test_non_numeric_volume_rejected(self):
        result = self.executor.execute_order("EURUSD", "BUY", "lots")
        self.assertEqual(result["status"], "error")
        self.assertIn("volume must be numeric", result["message"])

    def test_fractional_volume_rejected(self):
        result = self.executor.execute_order("EURUSD", "BUY", 1.5)
        self.assertEqual(result["status"], "error")
        self.assertIn("positive integer in 0.01-lot units", result["message"])

    def test_zero_and_negative_volume_rejected(self):
        for volume in (0, -100):
            result = self.executor.execute_order("EURUSD", "BUY", volume)
            self.assertEqual(result["status"], "error")
            self.assertIn("positive integer", result["message"])

    def test_symbol_normalized_to_uppercase(self):
        # Validation happens before the worker; a lowercase symbol is accepted
        # and normalized (the reactor worker is mocked away).
        with mock.patch.object(
            TradeExecutor,
            "_run_in_reactor",
            new=lambda self, reactor, symbol, side, volume, done, result: (
                done.set(),
                result.update({"status": "success", "position_id": 1}),
            ),
        ):
            result = self.executor.execute_order("eurusd", "buy", 100)
        self.assertEqual(result["status"], "success")


class TradeExecutorReactorGuardTests(SimpleTestCase):
    """_claim_reactor — one-shot per process; second call errors cleanly."""

    def setUp(self):
        TradeExecutor._reactor_consumed_flag = False
        self.executor = TradeExecutor(timeout=1.0)

    def tearDown(self):
        TradeExecutor._reactor_consumed_flag = False

    def test_claim_reactor_is_one_shot(self):
        self.assertFalse(TradeExecutor._claim_reactor())
        self.assertTrue(TradeExecutor._claim_reactor())

    def test_second_execute_order_returns_one_shot_error(self):
        def _fake_run(self, reactor, symbol, side, volume, done, result):
            done.set()
            result.update({"status": "success", "position_id": 7})

        with mock.patch.object(TradeExecutor, "_run_in_reactor", new=_fake_run):
            first = self.executor.execute_order("EURUSD", "BUY", 100)
            second = self.executor.execute_order("EURUSD", "BUY", 100)

        self.assertEqual(first["status"], "success")
        self.assertEqual(first["position_id"], 7)
        self.assertEqual(second["status"], "error")
        self.assertIn("one-shot resource", second["message"])

    def test_timeout_returns_clean_error(self):
        # The worker claims the reactor but never produces a result -> the
        # caller must time out with a clean error dict, never a hang.
        with (
            mock.patch.object(
                TradeExecutor, "_run_in_reactor", new=lambda *a, **k: None
            ),
            mock.patch.object(
                ps_exec, "_REACTOR_SHUTDOWN_GRACE_SECONDS", 0.0
            ),
        ):
            result = self.executor.execute_order("EURUSD", "BUY", 100)
        self.assertEqual(result["status"], "error")
        self.assertIn("Execution timed out", result["message"])


# ---------------------------------------------------------------------------
# ticks_range history query (cTrader trendbars) — mocked session, no reactor
# ---------------------------------------------------------------------------
class TicksRangeQueryTests(SimpleTestCase):
    """QueryWorker ticks_range — ProtoOAGetTrendbarsReq send + response mapping.

    Exercises the real request/response path with a mocked session (no Twisted
    reactor, no network). The live end-to-end probe is a separate manual step.
    """

    CREDENTIALS_ENV = {
        "PEPPERSTONE_CLIENT_ID": "c",
        "PEPPERSTONE_CLIENT_SECRET": "s",
        "PEPPERSTONE_ACCESS_TOKEN": "t",
        "PEPPERSTONE_ACCOUNT_ID": "5327660",
    }

    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, self.CREDENTIALS_ENV, clear=False)
        self.env_patch.start()
        self.worker = QueryWorker(
            "ticks_range",
            {
                "symbol": "EURUSD",
                "start": 1_750_000_000_000,  # epoch ms
                "end": 1_750_000_000_000 + 3_600_000,  # +1 hour
                "maxRows": 60,
            },
        )
        self.fake_client = FakeClient()
        self.worker.session = SimpleNamespace(
            symbols_map={"EURUSD": (1001, 5)},
            symbols_by_id={1001: ("EURUSD", 5)},
            client=self.fake_client,
            stop=lambda: None,
        )

    def tearDown(self):
        self.env_patch.stop()

    def _trendbars_res(self, bars):
        res = ProtoOAGetTrendbarsRes()
        res.ctidTraderAccountId = 5327660
        res.symbolId = 1001
        res.period = ProtoOATrendbarPeriod.M1
        res.timestamp = 1_750_003_600_000 // 60_000  # required field
        for minute, low, d_open, d_close, d_high in bars:
            bar = res.trendbar.add()
            bar.low = low
            bar.deltaOpen = d_open
            bar.deltaClose = d_close
            bar.deltaHigh = d_high
            bar.utcTimestampInMinutes = minute
            bar.volume = 123
        return res

    def test_send_history_period_request_shape(self):
        self.worker._on_ready({"EURUSD": (1001, 5)})
        self.assertEqual(len(self.fake_client.sent), 1)
        req = self.fake_client.sent[0]
        self.assertIsInstance(req, ProtoOAGetTrendbarsReq)
        self.assertEqual(req.ctidTraderAccountId, 5327660)
        self.assertEqual(req.symbolId, 1001)
        self.assertEqual(req.period, ProtoOATrendbarPeriod.M1)
        self.assertEqual(req.count, 60)
        # cTrader trendbars window is Unix MILLISECONDS (per cTrader Open API docs)
        self.assertEqual(req.fromTimestamp, 1_750_000_000_000)
        self.assertEqual(req.toTimestamp, 1_750_003_600_000)

    def test_history_response_mapped_to_ticks(self):
        self.worker._on_ready({"EURUSD": (1001, 5)})
        res = self._trendbars_res(
            [
                (29_166_666, 110_500, 10, 20, 30),
                (29_166_667, 110_520, 5, 15, 25),
            ]
        )
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.worker._on_message(
                self.fake_client,
                FakeMessage(
                    ProtoOAPayloadType.PROTO_OA_GET_TRENDBARS_RES,
                    res.SerializeToString(),
                ),
            )
        payload = json.loads(out.getvalue().splitlines()[-1])
        self.assertTrue(payload["ok"])
        ticks = payload["data"]["ticks"]
        self.assertEqual(len(ticks), 2)
        # chronological (ascending) order
        self.assertEqual(ticks[0]["epoch"], 29_166_666 * 60_000)
        self.assertEqual(ticks[1]["epoch"], 29_166_667 * 60_000)
        # close = low + deltaClose; digits=5 -> /100000
        # bar0: 110500 + 20 = 110520 -> 1.10520
        self.assertAlmostEqual(ticks[0]["last"], 1.10520, places=9)
        self.assertAlmostEqual(ticks[0]["bid"], 1.10520, places=9)
        self.assertAlmostEqual(ticks[0]["ask"], 1.10520, places=9)
        # bar1: 110520 + 15 = 110535 -> 1.10535
        self.assertAlmostEqual(ticks[1]["last"], 1.10535, places=9)

    def test_history_response_with_symmetric_spread(self):
        self.worker.params["spread"] = 0.0002
        self.worker._on_ready({"EURUSD": (1001, 5)})
        res = self._trendbars_res([(29_166_666, 110_500, 10, 20, 30)])
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.worker._on_message(
                self.fake_client,
                FakeMessage(
                    ProtoOAPayloadType.PROTO_OA_GET_TRENDBARS_RES,
                    res.SerializeToString(),
                ),
            )
        ticks = json.loads(out.getvalue().splitlines()[-1])["data"]["ticks"]
        close = 1.10520
        self.assertAlmostEqual(ticks[0]["last"], close, places=9)
        self.assertAlmostEqual(ticks[0]["bid"], close - 0.0001, places=9)
        self.assertAlmostEqual(ticks[0]["ask"], close + 0.0001, places=9)

    def test_unknown_symbol_fails_with_clear_code(self):
        self.worker.params["symbol"] = "NOPE"
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.worker._on_ready({"EURUSD": (1001, 5)})
        payload = json.loads(out.getvalue().splitlines()[-1])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], BrokerErrorCode.SYMBOL_NOT_FOUND.value)