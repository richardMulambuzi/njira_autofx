"""
Provider-independent API endpoints for the broker abstraction layer.

Every endpoint resolves the active provider through
:class:`forex.broker.factory.TradingProviderFactory` — provider classes are
NEVER imported directly here. The active provider is selected via
``TRADING_PROVIDER`` (default ``mock``) and can be switched at runtime through
``POST /api/provider/switch/`` (staff only).

Contract (all under ``/api/provider/``, JWT-protected):

    GET   /api/provider/status/         Provider health + config status.
    GET   /api/provider/account/        Account descriptor + balance snapshot.
    GET   /api/provider/positions/      Open positions.
    GET   /api/provider/orders/         Working orders.
    GET   /api/provider/trade-history/  Recent closed trades (?limit=50).
    GET   /api/provider/symbols/        Tradable instrument catalog.
    GET   /api/provider/candles/        OHLC candles (?symbol=&timeframe=&limit=).
    GET   /api/provider/market-data/    Latest tick (?symbol=).
    POST  /api/provider/switch/         Runtime provider switch (staff only).

Response envelope: ``{status, message, data, errors}``. Every provider call
wraps :class:`BrokerError` into the error envelope (HTTP 200, or 503 for
``BROKER_UNAVAILABLE`` / ``NETWORK_ERROR``); unexpected failures are logged
server-side and returned as ``Internal error`` with HTTP 500. Secrets are
never leaked — only environment variable names.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict
from datetime import datetime
from enum import Enum

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from forex.broker.base import CANDLE_TIMEFRAMES
from forex.broker.config import TradingConfig
from forex.broker.errors import BrokerError, BrokerErrorCode
from forex.broker.factory import TradingProviderFactory

logger = logging.getLogger("forex.broker.views")

#: Symbol allowlist — mirrors ``forex.ticks_views.SYMBOL_PATTERN``.
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]{1,20}$")

CANDLES_LIMIT_DEFAULT = 100
CANDLES_LIMIT_MAX = 2000
TRADE_HISTORY_LIMIT_DEFAULT = 50
TRADE_HISTORY_LIMIT_MAX = 1000

# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _json_safe(value):
    """Recursively coerce dataclass/Enum values into JSON-serializable primitives.

    ``dataclasses.asdict`` keeps enums as enum members and datetimes as
    ``datetime`` objects; DRF's JSONRenderer can serialize neither, so they are
    converted to their primitive forms here.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _asdict(record) -> dict:
    """``dataclasses.asdict`` with JSON-safe coercion of enum/datetime fields."""
    return _json_safe(asdict(record))


def _records(records) -> list[dict]:
    """Convert an iterable of dataclass records into JSON-safe dicts."""
    return [_asdict(record) for record in records]


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _broker_error_response(exc: BrokerError) -> Response:
    """Map a :class:`BrokerError` into the standard error envelope.

    ``BROKER_UNAVAILABLE`` / ``NETWORK_ERROR`` surface as HTTP 503 (the
    upstream broker is unreachable); every other broker error returns HTTP
    200 with a machine-readable ``error_code`` so callers can react without
    conflating transport failures with business rejections.
    """
    payload = {
        "status": "error",
        "message": str(exc),
        "error_code": exc.code.value,
        "provider": exc.provider,
        "data": None,
        "errors": None,
    }
    http_code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if exc.code
        in (BrokerErrorCode.BROKER_UNAVAILABLE, BrokerErrorCode.NETWORK_ERROR)
        else status.HTTP_200_OK
    )
    return Response(payload, status=http_code)


def _internal_error_response() -> Response:
    """Standard 500 envelope for unexpected failures.

    Must be called from inside an ``except Exception`` block so
    ``logger.exception`` records the active exception (full detail stays
    server-side; the client only sees ``Internal error``).
    """
    logger.exception("Unexpected provider API failure")
    return Response(
        {
            "status": "error",
            "message": "Internal error",
            "data": None,
            "errors": None,
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_symbol(value) -> str | None:
    """Return the uppercase symbol when it matches the allowlist, else ``None``."""
    if not isinstance(value, str):
        return None
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        return None
    return symbol


def _parse_limit(
    query_params, default: int, maximum: int, name: str = "limit"
) -> int:
    """Parse and clamp an integer query parameter to ``1..maximum``."""
    try:
        value = int(query_params.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_status(request):
    """
    GET /api/provider/status/ — active provider health + config status.

    Never crashes on missing credentials: ``health()`` is a local snapshot
    (no network) and the config summary is derived from the environment. If
    the configured provider cannot be resolved (e.g. an unknown
    ``TRADING_PROVIDER``), the error envelope is returned together with the
    config summary in ``data``.
    """
    config = TradingConfig.from_env()
    provider_config = config.get_provider(config.active_provider)
    summary = {
        "provider": config.active_provider,
        "environment": config.mode.value,
        "health": None,
        "config": config.to_dict(),
        "safety_warnings": config.safety_check(),
        "credentials_valid": bool(
            provider_config is not None and provider_config.credentials_valid
        ),
    }

    try:
        provider = TradingProviderFactory.get_active(config=config)
        summary["provider"] = provider.name
        summary["environment"] = provider.mode.value
        summary["health"] = provider.health().to_dict()
    except BrokerError as exc:
        # Unknown provider (or an infrastructure-level failure) — surface the
        # error envelope alongside the env-derived config summary.
        return Response(
            {
                "status": "error",
                "message": str(exc),
                "error_code": exc.code.value,
                "provider": exc.provider,
                "data": summary,
                "errors": None,
            },
            status=status.HTTP_200_OK,
        )
    except Exception:  # noqa: BLE001 — never crash the status endpoint
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider status",
            "data": summary,
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_account(request):
    """GET /api/provider/account/ — account descriptor + balance snapshot."""
    try:
        provider = TradingProviderFactory.get_active()
        account = _asdict(provider.get_account())
        balance = _asdict(provider.get_balance())
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider account",
            "data": {"account": account, "balance": balance},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_positions(request):
    """GET /api/provider/positions/ — all open positions for the account."""
    try:
        provider = TradingProviderFactory.get_active()
        positions = _records(provider.get_positions())
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider positions",
            "data": {"positions": positions},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_orders(request):
    """GET /api/provider/orders/ — all working (non-terminal) orders."""
    try:
        provider = TradingProviderFactory.get_active()
        orders = _records(provider.get_orders())
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider orders",
            "data": {"orders": orders},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_trade_history(request):
    """
    GET /api/provider/trade-history/?limit=50 — recent closed trades.

    ``limit`` is clamped to 1..1000.
    """
    limit = _parse_limit(
        request.query_params,
        TRADE_HISTORY_LIMIT_DEFAULT,
        TRADE_HISTORY_LIMIT_MAX,
    )
    try:
        provider = TradingProviderFactory.get_active()
        trades = _records(provider.get_trade_history(limit))
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider trade history",
            "data": {"trades": trades},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_symbols(request):
    """GET /api/provider/symbols/ — the tradable instrument catalog."""
    try:
        provider = TradingProviderFactory.get_active()
        symbols = _records(provider.get_symbols())
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider symbols",
            "data": {"symbols": symbols},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_candles(request):
    """
    GET /api/provider/candles/?symbol=EURUSD&timeframe=1H&limit=100.

    ``symbol`` must match ``^[A-Z0-9_]{1,20}$``, ``timeframe`` must be one of
    the provider-neutral candle labels (1M..1D), and ``limit`` is clamped to
    1..2000.
    """
    symbol = _validate_symbol(request.query_params.get("symbol", ""))
    if symbol is None:
        return Response(
            {
                "status": "error",
                "message": (
                    "symbol is required (1-20 chars of A-Z, 0-9 or underscore)"
                ),
                "data": None,
                "errors": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    timeframe = (request.query_params.get("timeframe", "") or "").strip().upper()
    if timeframe not in CANDLE_TIMEFRAMES:
        return Response(
            {
                "status": "error",
                "message": (
                    f"timeframe must be one of: {', '.join(CANDLE_TIMEFRAMES)}"
                ),
                "data": None,
                "errors": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    limit = _parse_limit(
        request.query_params, CANDLES_LIMIT_DEFAULT, CANDLES_LIMIT_MAX
    )
    try:
        provider = TradingProviderFactory.get_active()
        candles = _records(provider.get_candles(symbol, timeframe, limit))
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider candles",
            "data": {"symbol": symbol, "timeframe": timeframe, "candles": candles},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_market_data(request):
    """GET /api/provider/market-data/?symbol=EURUSD — the latest tick."""
    symbol = _validate_symbol(request.query_params.get("symbol", ""))
    if symbol is None:
        return Response(
            {
                "status": "error",
                "message": (
                    "symbol is required (1-20 chars of A-Z, 0-9 or underscore)"
                ),
                "data": None,
                "errors": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        provider = TradingProviderFactory.get_active()
        tick = _asdict(provider.get_market_data(symbol))
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Provider market data",
            "data": {"symbol": symbol, "tick": tick},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def provider_switch(request):
    """
    POST /api/provider/switch/ — switch the active provider at runtime.

    Staff only (``request.user.is_staff``). Body: ``{"provider": "mock"}`` —
    valid providers: ``mock``, ``pepperstone``. Unknown providers
    are rejected by the factory with the error envelope.

    The switch is process-wide (the factory caches the active provider), so
    every subsequent request on this process uses the new provider.
    Env-var-driven switching (``TRADING_PROVIDER``) still requires a process
    restart; this endpoint switches the in-process singleton where the
    architecture permits.
    """
    if not getattr(request.user, "is_staff", False):
        return Response(
            {
                "status": "error",
                "message": (
                    "Staff privileges are required to switch providers"
                ),
                "data": None,
                "errors": None,
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    provider_name = request.data.get("provider")
    if not isinstance(provider_name, str) or not provider_name.strip():
        return Response(
            {
                "status": "error",
                "message": "provider is required (mock or pepperstone)",
                "data": None,
                "errors": None,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    provider_name = provider_name.strip().lower()

    try:
        provider = TradingProviderFactory.switch(provider_name)
        health = provider.health().to_dict()
    except BrokerError as exc:
        return _broker_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": f"Switched active provider to {provider.name}",
            "data": {"provider": provider.name, "health": health},
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )
