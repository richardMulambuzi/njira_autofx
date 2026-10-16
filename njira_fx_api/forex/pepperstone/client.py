"""
Pepperstone / cTrader Open API — shared connection helpers.

This module owns the connection-layer primitives for the Pepperstone
integration:

* ``get_credentials``        — environment-driven credentials (never hardcoded).
* ``build_client``           — a cTrader Open API ``Client`` bound to the demo
                               protobuf endpoint.
* ``PepperstoneSession``     — the full authentication + symbol-detail handshake
                               state machine, plus per-message routing.
* ``compute_real_price``     — raw protobuf price -> real price using the
                               symbol's digit precision.
* ``compute_spread_pips``    — bid/ask spread expressed in pips.

Verified SDK facts (ctrader-open-api 0.9.2 installed in myvenv):

* ``from ctrader_open_api import Client, EndPoints, TcpProtocol``
* ``Client(host, port, protocol, retryPolicy=..., ...)`` — ``protocol`` MUST be
  a protocol *class* (``Factory.forProtocol`` assigns it to ``factory.protocol``);
  a boolean like ``True`` would break the connection. We pass ``TcpProtocol``.
* ``EndPoints.PROTOBUF_DEMO_HOST`` exists; there is NO ``PROTOBUF_DEMO_PORT`` —
  the shared port constant is ``EndPoints.PROTOBUF_PORT`` (5035).
* Auth messages live in ``ctrader_open_api.messages.OpenApiMessages_pb2``:
  ``ProtoOAApplicationAuthReq`` (clientId, clientSecret) and
  ``ProtoOAAccountAuthReq`` (ctidTraderAccountId, accessToken).
* Symbol list messages also live in ``OpenApiMessages_pb2``:
  ``ProtoOASymbolsListReq`` / ``ProtoOASymbolsListRes`` /
  ``ProtoOASymbolByIdReq`` / ``ProtoOASymbolByIdRes``.
* ``ProtoOASymbolsListRes.symbol`` is a list of ``ProtoOALightSymbol`` which
  carries ``symbolName`` + ``symbolId`` but **no** ``digits``; digits only exist
  on ``ProtoOASymbol`` (``OpenApiModelMessages_pb2``) returned by
  ``ProtoOASymbolByIdRes``. The handshake therefore requests symbol details
  after the list so the ``{symbolName: (symbolId, digits)}`` map is complete.

Secrets: only environment variable NAMES are ever logged — never values.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from ctrader_open_api import Client, EndPoints, TcpProtocol
from ctrader_open_api.protobuf import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOAErrorRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAPayloadType,
    ProtoOAErrorCode,
)

logger = logging.getLogger("forex.pepperstone.client")

# Spotware OAuth token endpoint (per help.ctrader.com/open-api/account-authentication).
# POST {TOKEN_URL}?grant_type=refresh_token&refresh_token=...&client_id=...&client_secret=...
TOKEN_URL = os.environ.get(
    "PEPPERSTONE_TOKEN_URL", "https://openapi.ctrader.com/apps/token"
)
TOKEN_TIMEOUT_SECONDS = 10
# Error codes meaning "access token dead" -> single refresh + one AccountAuth retry.
TOKEN_EXPIRED_CODES = frozenset({"CH_ACCESS_TOKEN_INVALID", "OA_AUTH_TOKEN_EXPIRED"})

# ---------------------------------------------------------------------------
# Environment-driven configuration
# ---------------------------------------------------------------------------
DEFAULT_PEPPERSTONE_ACCOUNT_ID = 5327660
PROTOBUF_DEMO_HOST = EndPoints.PROTOBUF_DEMO_HOST
# NOTE: the SDK exposes only a single shared port constant — there is no
# PROTOBUF_DEMO_PORT attribute (verified against the installed 0.9.2 package).
PROTOBUF_DEMO_PORT = EndPoints.PROTOBUF_PORT

# Friendly explanations for the error codes the API sends in ProtoOAErrorRes
# (string errorCode, e.g. "CH_ACCESS_TOKEN_INVALID"). Kept as plain strings so
# a server-side code we have not seen is still logged verbatim.
_FRIENDLY_ERRORS = {
    "CH_ACCESS_TOKEN_INVALID": (
        "the access token was rejected — check PEPPERSTONE_ACCESS_TOKEN"
    ),
    "OA_AUTH_TOKEN_EXPIRED": (
        "the access token has expired — generate a new one for the account"
    ),
    "ACCOUNT_NOT_AUTHORIZED": (
        "this account id is not authorized for the access token — check "
        "PEPPERSTONE_ACCOUNT_ID"
    ),
    "CH_CTID_TRADER_ACCOUNT_NOT_FOUND": (
        "ctidTraderAccountId was not found — check PEPPERSTONE_ACCOUNT_ID"
    ),
    "CH_CLIENT_AUTH_FAILURE": (
        "client authentication failed — check PEPPERSTONE_CLIENT_ID / "
        "PEPPERSTONE_CLIENT_SECRET"
    ),
    "CH_CLIENT_NOT_AUTHENTICATED": (
        "client not authenticated — check PEPPERSTONE_CLIENT_ID / "
        "PEPPERSTONE_CLIENT_SECRET"
    ),
    "CH_CLIENT_ALREADY_AUTHENTICATED": (
        "client already authenticated on this connection"
    ),
}


def _error_code_label(code: str) -> str:
    """Best-effort: map a numeric error-code string to its enum name."""
    try:
        return ProtoOAErrorCode.Name(int(code))
    except Exception:  # noqa: BLE001 — non-numeric codes pass through
        return code


def _friendly_error_message(code: str, description: str) -> str:
    code_upper = str(code).upper()
    label = _error_code_label(code) if code else "UNKNOWN"
    message = f"cTrader error {label}: {description or 'no description'}"
    hint = _FRIENDLY_ERRORS.get(code_upper) or _FRIENDLY_ERRORS.get(label)
    if hint:
        message = f"{message} ({hint})"
    return message


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def get_credentials() -> dict:
    """
    Read Pepperstone credentials from the environment.

    Required: ``PEPPERSTONE_CLIENT_ID``, ``PEPPERSTONE_CLIENT_SECRET`` and
    ``PEPPERSTONE_ACCESS_TOKEN``. ``PEPPERSTONE_ACCOUNT_ID`` defaults to
    ``DEFAULT_PEPPERSTONE_ACCOUNT_ID`` (5327660). ``PEPPERSTONE_REFRESH_TOKEN``
    is optional here (needed for auto-refresh on expiry) and is included in
    the returned dict only when set, so existing callers are unaffected.

    Raises ``RuntimeError`` with the names of the missing variables — never
    with their (secret) values — so callers can fail fast with a clear message.
    """
    values = {
        "client_id": os.environ.get("PEPPERSTONE_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("PEPPERSTONE_CLIENT_SECRET", "").strip(),
        "access_token": os.environ.get("PEPPERSTONE_ACCESS_TOKEN", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        env_names = ", ".join(
            f"PEPPERSTONE_{name.upper()}" for name in missing
        )
        raise RuntimeError(
            "Pepperstone credentials missing: " + env_names + ". "
            "Set them in the environment (or the project .env file) and "
            "re-run."
        )

    account_raw = os.environ.get(
        "PEPPERSTONE_ACCOUNT_ID", str(DEFAULT_PEPPERSTONE_ACCOUNT_ID)
    ).strip()
    try:
        account_id = int(account_raw)
    except ValueError as exc:
        raise RuntimeError(
            f"PEPPERSTONE_ACCOUNT_ID must be an integer, got {account_raw!r}"
        ) from exc

    return {
        "client_id": values["client_id"],
        "client_secret": values["client_secret"],
        "access_token": values["access_token"],
        "account_id": account_id,
        **(
            {"refresh_token": refresh_token}
            if (refresh_token := os.environ.get("PEPPERSTONE_REFRESH_TOKEN", "").strip())
            else {}
        ),
    }


def _default_env_path() -> Optional[Path]:
    """Locate the project .env (njira_fx_api/.env); None if absent."""
    override = os.environ.get("PEPPERSTONE_ENV_PATH", "").strip()
    if override:
        return Path(override)
    candidate = Path(__file__).resolve().parent.parent.parent / ".env"
    return candidate if candidate.is_file() else None


def _persist_tokens_env_file(
    access_token: str, refresh_token: str, env_path: Optional[Path] = None
) -> None:
    """Atomically update .env with rotated tokens; keep a .bak backup."""
    path = env_path or _default_env_path()
    if path is None:
        return
    try:
        existing = path.read_text().splitlines() if path.is_file() else []
    except OSError as exc:
        logger.warning("Skipping .env persist (%s unreadable): %s", path.name, exc)
        return
    updated = dict(PEPPERSTONE_ACCESS_TOKEN=access_token, PEPPERSTONE_REFRESH_TOKEN=refresh_token)
    seen = set()
    out_lines = []
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in updated:
            out_lines.append(f"{key}={updated[key]}")
            seen.add(key)
        else:
            out_lines.append(line)
    for key, value in updated.items():
        if key not in seen:
            out_lines.append(f"{key}={value}")
    try:
        # NOTE: never Path.with_suffix() here — on a dotfile like ".env" it
        # yields ".env.env.bak". Append explicitly instead.
        backup = Path(f"{path}.bak")
        if path.is_file():
            backup.write_text("\n".join(existing) + ("\n" if existing else ""))
        tmp = Path(f"{path}.tmp")
        tmp.write_text("\n".join(out_lines) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Skipping .env persist (%s unwritable): %s", path.name, exc)


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    token_url: Optional[str] = None,
    persist: bool = False,
    env_path: Optional[Path] = None,
    timeout: int = TOKEN_TIMEOUT_SECONDS,
) -> str:
    """
    Exchange a Spotware refresh token for a new access token (token rotation).

    POSTs ``{token_url}?grant_type=refresh_token&...`` with a 10s timeout,
    always persists the rotated access+refresh tokens to ``os.environ``
    (in-memory only by default). Disk write to .env (atomic, with .bak
    backup) happens ONLY when ``persist=True``; ``env_path`` (or
    ``PEPPERSTONE_ENV_PATH`` / project .env via ``_default_env_path``)
    selects the file. Default ``persist=False`` so unit tests can never
    overwrite real credentials on disk.
    Returns the new access token. Never logs secret values (names only).
    Pure helper: HTTP layer is ``requests.post`` (mock ``requests.post`` /
    ``forex.pepperstone.client.requests.post`` in unit tests).
    """
    import requests  # local import: keeps module importable without requests

    url = token_url or TOKEN_URL
    logger.info("Refreshing cTrader access token via token endpoint")
    try:
        response = requests.post(
            url,
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — network failure surfaces cleanly
        logger.error("Token refresh request failed: %s", type(exc).__name__)
        raise RuntimeError("cTrader token refresh failed (network error)") from exc
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("Token refresh returned non-JSON (status=%s)", response.status_code)
        raise RuntimeError("cTrader token refresh failed (bad response)") from exc
    if response.status_code != 200 or not payload.get("accessToken"):
        code = payload.get("errorCode") if isinstance(payload, dict) else None
        logger.error(
            "Token refresh rejected (status=%s, errorCode=%s) — check "
            "PEPPERSTONE_CLIENT_ID / PEPPERSTONE_CLIENT_SECRET / "
            "PEPPERSTONE_REFRESH_TOKEN",
            response.status_code,
            code,
        )
        raise RuntimeError(f"cTrader token refresh failed: {code or 'unknown error'}")
    new_access = payload["accessToken"]
    new_refresh = payload.get("refreshToken") or refresh_token
    os.environ["PEPPERSTONE_ACCESS_TOKEN"] = new_access
    os.environ["PEPPERSTONE_REFRESH_TOKEN"] = new_refresh
    if persist:
        _persist_tokens_env_file(new_access, new_refresh, env_path=env_path)
    logger.info("cTrader access token refreshed and persisted")
    return new_access


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------
def build_client(retry_policy: Optional[Callable[[int], float]] = None) -> Client:
    """
    Build a cTrader Open API ``Client`` for the demo protobuf endpoint.

    ``retry_policy`` is forwarded to ``ClientService``: a callable taking the
    number of consecutive failed connection attempts and returning the seconds
    to wait before retrying. ``None`` uses Twisted's default exponential
    backoff (retries indefinitely).
    """
    return Client(
        PROTOBUF_DEMO_HOST,
        PROTOBUF_DEMO_PORT,
        TcpProtocol,
        retryPolicy=retry_policy,
    )


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------
# cTrader sends metals (XAUUSD/XAGUSD) with raw prices scaled by 1e5 while
# reporting small `digits` (e.g. 2) — decoding with 10**digits yields prices
# 1000x too large. FX majors decode correctly with 10**digits.
_FIXED_METAL_DIVISOR = 100_000
_MAGNITUDE_INFER_THRESHOLD = 100_000  # raw/100 > this -> assume 1e5


def price_scale(symbol: str | None, digits: int = 5, raw=None) -> int:
    """Return the raw->real divisor for ``symbol``.

    * XAU*/XAG* (any suffix/case) -> 100000 (metals are raw/1e5).
    * Unknown symbols with a huge ``raw`` (``abs(raw)/100 > 100000``,
      i.e. raw > 10M) -> 100000 (magnitude inference for new metals).
    * Everything else -> ``10 ** max(digits, 0)`` (FX majors, JPY-safe).
    """
    sym = (symbol or "").strip().upper()
    if sym.startswith("XAU") or sym.startswith("XAG"):
        return _FIXED_METAL_DIVISOR
    if raw is not None:
        try:
            raw_int = int(raw)
        except (TypeError, ValueError):
            raw_int = 0
        if abs(raw_int) // 100 > _MAGNITUDE_INFER_THRESHOLD:
            return _FIXED_METAL_DIVISOR
    return 10 ** max(int(digits), 0)


def compute_real_price(raw_price, digits: int, symbol: str | None = None) -> float:
    """
    Convert a raw protobuf price (fixed-point integer) to the real price.

    Without ``symbol``: legacy ``raw / 10 ** digits`` (JPY-safe, never a
    hardcoded 100000 divisor). With ``symbol``: ``raw / price_scale(...)``
    so XAUUSD/XAGUSD decode via 1e5 while FX majors keep 10**digits.
    """
    if symbol is not None:
        return raw_price / price_scale(symbol, digits, raw_price)
    return raw_price / (10 ** max(digits, 0))


def compute_spread_pips(bid: float, ask: float, digits: int) -> float:
    """
    Express a (real-price) bid/ask spread in pips.

    * 5-digit symbols (e.g. EURUSD): ``(ask - bid) * 10000``.
    * General case: ``(ask - bid) * 10 ** (digits - 4)``.

    ``bid`` / ``ask`` must already be real prices (see ``compute_real_price``).
    """
    if digits == 5:
        return (ask - bid) * 10000
    return (ask - bid) * (10 ** (digits - 4))


# ---------------------------------------------------------------------------
# Handshake session
# ---------------------------------------------------------------------------
class PepperstoneSession:
    """
    Manages the cTrader authentication and symbol-detail handshake.

    Chain (each step fires on the matching response payload type):

        onConnected
          -> send ProtoOAApplicationAuthReq (clientId, clientSecret)
        APPLICATION_AUTH_RES
          -> send ProtoOAAccountAuthReq (ctidTraderAccountId, accessToken)
        ACCOUNT_AUTH_RES
          -> send ProtoOASymbolsListReq
        SYMBOLS_LIST_RES
          -> send ProtoOASymbolByIdReq for the requested symbols
        SYMBOL_BY_ID_RES
          -> build ``{symbolName: (symbolId, digits)}``, call ``on_ready``

    Errors arrive as ``ProtoOAErrorRes`` (e.g. bad token); they are decoded and
    routed to ``on_error`` with a human-readable hint. Every other payload
    (spot events, execution events, …) is forwarded untouched to ``on_message``
    so the engines can process them without re-parsing the handshake.
    """

    def __init__(
        self,
        credentials: dict,
        symbols: Optional[list[str]] = None,
        on_ready: Optional[Callable[[dict], None]] = None,
        on_message: Optional[Callable[[object, object], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_disconnected: Optional[Callable[[object, object], None]] = None,
        retry_policy: Optional[Callable[[int], float]] = None,
    ) -> None:
        self.credentials = credentials
        # Symbols to resolve details for. ``None`` means "all symbols".
        self.symbols = [s.strip().upper() for s in symbols] if symbols else None
        self.on_ready = on_ready
        self.on_message = on_message
        self.on_error = on_error
        self.on_disconnected = on_disconnected

        self.client = build_client(retry_policy=retry_policy)

        # symbolName -> (symbolId, digits)
        self.symbols_map: dict = {}
        # symbolId -> (symbolName, digits) — reverse lookup for spot events
        self.symbols_by_id: dict = {}
        self._light_symbol_names: dict = {}  # symbolId -> symbolName
        self._authenticated = False
        self._symbols_loaded = False
        self._token_refresh_attempted = False  # single refresh+retry per session

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Register callbacks and start the connection service."""
        self.client.setConnectedCallback(self._on_connected)
        self.client.setMessageReceivedCallback(self._on_message)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.startService()

    def stop(self) -> None:
        """Stop the connection service (safe to call when not connected)."""
        try:
            self.client.stopService()
        except Exception as exc:  # noqa: BLE001 — shutdown must never raise
            logger.debug("Ignoring error while stopping session: %s", exc)

    # -- outbound helpers ---------------------------------------------------
    def subscribe_spots(self, symbol_ids) -> None:
        """Subscribe to live spot updates for ``symbol_ids`` (list of ints)."""
        ids = list(symbol_ids)
        if not ids:
            logger.warning("subscribe_spots called with an empty symbol id list")
            return
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASubscribeSpotsReq,
        )

        request = ProtoOASubscribeSpotsReq(
            ctidTraderAccountId=self.credentials["account_id"],
            symbolId=ids,
        )
        self.client.send(request)
        logger.info("Subscribed to spot updates for %d symbol(s)", len(ids))

    # -- connected ----------------------------------------------------------
    def _on_connected(self, client: object) -> None:
        logger.info("Connected to cTrader Open API at %s", PROTOBUF_DEMO_HOST)
        self._authenticated = False
        self._symbols_loaded = False
        self._token_refresh_attempted = False
        request = ProtoOAApplicationAuthReq(
            clientId=self.credentials["client_id"],
            clientSecret=self.credentials["client_secret"],
        )
        client.send(request)
        logger.info("Sent ProtoOAApplicationAuthReq")

    # -- message router -----------------------------------------------------
    def _on_message(self, client: object, message: object) -> None:
        payload_type = message.payloadType
        if payload_type == ProtoOAPayloadType.PROTO_OA_APPLICATION_AUTH_RES:
            self._on_application_auth_res(client)
        elif payload_type == ProtoOAPayloadType.PROTO_OA_ACCOUNT_AUTH_RES:
            self._on_account_auth_res(client)
        elif payload_type == ProtoOAPayloadType.PROTO_OA_SYMBOLS_LIST_RES:
            self._on_symbols_list_res(client, Protobuf.extract(message))
        elif payload_type == ProtoOAPayloadType.PROTO_OA_SYMBOL_BY_ID_RES:
            self._on_symbol_by_id_res(client, Protobuf.extract(message))
        elif payload_type == ProtoOAPayloadType.PROTO_OA_ERROR_RES:
            self._on_error_res(Protobuf.extract(message), client=client)
        elif self.on_message is not None:
            self.on_message(client, message)

    # -- handshake stages ---------------------------------------------------
    def _on_application_auth_res(self, client: object) -> None:
        request = ProtoOAAccountAuthReq(
            ctidTraderAccountId=self.credentials["account_id"],
            accessToken=self.credentials["access_token"],
        )
        client.send(request)
        logger.info(
            "Application auth OK — sent ProtoOAAccountAuthReq for account %s",
            self.credentials["account_id"],
        )

    def _on_account_auth_res(self, client: object) -> None:
        self._authenticated = True
        request = ProtoOASymbolsListReq(
            ctidTraderAccountId=self.credentials["account_id"],
            includeArchivedSymbols=False,
        )
        client.send(request)
        logger.info("Account auth OK — requested symbol list")

    def _on_symbols_list_res(self, client: object, response) -> None:
        # ProtoOALightSymbol has symbolName + symbolId but NO digits, so we
        # remember the names and request full details (digits) by id.
        self._light_symbol_names = {
            symbol.symbolId: symbol.symbolName
            for symbol in response.symbol
            if symbol.symbolName
        }

        if self.symbols:
            wanted = set(self.symbols)
            symbol_ids = [
                symbol_id
                for symbol_id, name in self._light_symbol_names.items()
                if name.upper() in wanted
            ]
            missing = sorted(
                wanted - {name.upper() for name in self._light_symbol_names.values()}
            )
            if missing:
                logger.warning(
                    "Symbols not present in the account list: %s", missing
                )
        else:
            symbol_ids = list(self._light_symbol_names.keys())

        if not symbol_ids:
            self._fail(
                "No symbols available: none of the requested symbols exist in "
                "the account (check PEPPERSTONE_SYMBOLS)."
            )
            return

        request = ProtoOASymbolByIdReq(
            ctidTraderAccountId=self.credentials["account_id"],
            symbolId=symbol_ids,
        )
        client.send(request)
        logger.info(
            "Symbol list received (%d symbols) — requesting details for %d",
            len(self._light_symbol_names),
            len(symbol_ids),
        )

    def _on_symbol_by_id_res(self, client: object, response) -> None:
        self.symbols_map.clear()
        self.symbols_by_id.clear()
        for symbol in response.symbol:
            name = self._light_symbol_names.get(symbol.symbolId)
            if name:
                self.symbols_map[name] = (symbol.symbolId, symbol.digits)
                self.symbols_by_id[symbol.symbolId] = (name, symbol.digits)
        self._symbols_loaded = True
        logger.info(
            "Symbol details loaded: %d symbol(s) with digit precision",
            len(self.symbols_map),
        )
        # Log the raw->real divisor once per symbol (names + divisor only —
        # never raw price values) so a 1000x metals mis-scale is visible.
        try:
            scales = ", ".join(
                f"{name}/div={price_scale(name, digits)}"
                for name, (_sid, digits) in sorted(self.symbols_map.items())
            )
            logger.info("Price scales: %s", scales)
        except Exception:  # noqa: BLE001 — logging must never break handshake
            pass
        if self.on_ready is not None:
            self.on_ready(dict(self.symbols_map))

    # -- errors -------------------------------------------------------------
    def _try_refresh_and_retry_account_auth(self, client: object) -> bool:
        """Single refresh + one AccountAuth retry. Returns True if retried."""
        if self._token_refresh_attempted:
            return False
        self._token_refresh_attempted = True  # no loop, ever
        refresh_token = self.credentials.get("refresh_token", "") or os.environ.get(
            "PEPPERSTONE_REFRESH_TOKEN", ""
        ).strip()
        if not refresh_token:
            logger.error(
                "Access token expired and no PEPPERSTONE_REFRESH_TOKEN is set — "
                "cannot auto-refresh."
            )
            return False
        try:
            # Live session: persist rotation to disk (sandboxed in tests via
            # PEPPERSTONE_ENV_PATH). Direct refresh_access_token() calls default
            # to persist=False (in-memory os.environ only).
            new_access = refresh_access_token(
                self.credentials["client_id"],
                self.credentials["client_secret"],
                refresh_token,
                persist=True,
                env_path=_default_env_path(),
            )
        except Exception:  # noqa: BLE001 — refresh failure surfaces as fatal below
            logger.error("Access token refresh failed — surfacing original error")
            return False
        self.credentials["access_token"] = new_access
        self.credentials["refresh_token"] = os.environ.get(
            "PEPPERSTONE_REFRESH_TOKEN", ""
        ).strip() or refresh_token
        client.send(
            ProtoOAAccountAuthReq(
                ctidTraderAccountId=self.credentials["account_id"],
                accessToken=new_access,
            )
        )
        logger.info("Access token refreshed — retried ProtoOAAccountAuthReq once")
        return True

    def _on_error_res(self, error: ProtoOAErrorRes, client: object = None) -> None:
        message = _friendly_error_message(error.errorCode, error.description)
        logger.error("Pepperstone API error: %s", message)
        code_upper = str(error.errorCode or "").upper()
        if code_upper in TOKEN_EXPIRED_CODES and client is not None:
            if self._try_refresh_and_retry_account_auth(client):
                return  # retry sent; wait for ACCOUNT_AUTH_RES or next ERROR_RES
        if error.errorCode and code_upper in {
            "CH_ACCESS_TOKEN_INVALID",
            "OA_AUTH_TOKEN_EXPIRED",
            "ACCOUNT_NOT_AUTHORIZED",
            "CH_CLIENT_AUTH_FAILURE",
        }:
            logger.error(
                "Authentication failed — the session will not proceed. "
                "Fix the Pepperstone credentials in the environment and retry."
            )
        self._fail(message)

    def _fail(self, message: str) -> None:
        """Route a fatal session error to the engine's error handler."""
        if self.on_error is not None:
            self.on_error(message)

    # -- disconnection ------------------------------------------------------
    def _on_disconnected(self, client: object, reason: object) -> None:
        reason_text = (
            reason.getErrorMessage() if hasattr(reason, "getErrorMessage") else ""
        )
        logger.warning("Disconnected from cTrader: %s", reason_text or "unknown")
        self._authenticated = False
        self._symbols_loaded = False
        if self.on_disconnected is not None:
            self.on_disconnected(client, reason)
