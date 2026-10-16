"""
/api/sync/* — tick synchronization engine API endpoints (contracts §7).

All endpoints are JWT-protected (``IsAuthenticated``) and use the standard
envelope ``{status, message, data, errors}`` (broker/views.py pattern).
Timestamps serialize as ISO-8601 UTC strings (contracts §1.3).

Contract (all under ``/api/sync/``):

    GET  /api/sync/status/    Engine + per-symbol window status (§7.1).
    GET  /api/sync/ticks/     Query the engine window (§7.2).
    POST /api/sync/backfill/  Trigger historical backfill (staff only, §7.3).

Error taxonomy (contracts §7.4): provider failures reuse ``BrokerErrorCode``
mapped exactly like ``_broker_error_response`` (broker/views.py); the sync
layer adds ``SyncErrorCode`` (``invalid_request`` → 400,
``engine_not_running`` → 503, ``backfill_in_progress`` → 409).

Import-safety (spec §10 Phase 0): the heavy engine chain
(``forex.sync.engine`` → numpy-backed storage) is imported lazily inside the
view functions so this module never pulls it at Django app-load time — the
sync package stays dead code until ``SYNC_ENGINE_ENABLED=true``.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from enum import Enum

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from forex.broker.config import TradingConfig
from forex.broker.errors import BrokerError, BrokerErrorCode
from forex.broker.factory import TradingProviderFactory
from forex.sync.config import SyncConfig
from forex.sync.errors import SyncError, SyncErrorCode

logger = logging.getLogger("forex.sync.views")

#: Symbol allowlist — mirrors ``forex.ticks_views.SYMBOL_PATTERN``.
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]{1,20}$")

TICKS_LIMIT_DEFAULT = 500
TICKS_LIMIT_MAX = 2000
BACKFILL_DAYS_DEFAULT = 60  # == SYNC_WINDOW_MAX_DAYS (spec §1.3)
BACKFILL_DAYS_MAX = 60  # backfilling beyond the window is pointless (§7.3)

# ---------------------------------------------------------------------------
# Serialization helpers (mirror broker/views.py)
# ---------------------------------------------------------------------------


def _json_safe(value):
    """Recursively coerce dataclass/Enum values into JSON-serializable primitives.

    ``dataclasses.asdict`` keeps enums as enum members and datetimes as
    ``datetime`` objects; DRF's JSONRenderer can serialize neither, so they are
    converted to their primitive forms here (ISO-8601 UTC strings).
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


def _sync_error_response(exc: SyncError) -> Response:
    """Map a :class:`SyncError` into the standard error envelope.

    HTTP mapping (contracts §7.4):
      * ``INVALID_REQUEST``        → 400 ``invalid_request``
      * ``ENGINE_NOT_RUNNING`` / ``ENGINE_DISABLED`` → 503
        ``engine_not_running`` (a disabled engine is not running)
      * ``BACKFILL_IN_PROGRESS``   → 409 ``backfill_in_progress``
      * ``INVALID_STATE``          → 409 ``invalid_state`` (operation not
        allowed in the current engine state — closest §7.4 semantic;
        documented deviation, the contract table does not list it)
    """
    payload = {
        "status": "error",
        "message": exc.message,
        "error_code": exc.code.value,
        "data": None,
        "errors": None,
    }
    if exc.code == SyncErrorCode.INVALID_REQUEST:
        http_code = status.HTTP_400_BAD_REQUEST
    elif exc.code in (
        SyncErrorCode.ENGINE_NOT_RUNNING,
        SyncErrorCode.ENGINE_DISABLED,
    ):
        http_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif exc.code in (
        SyncErrorCode.BACKFILL_IN_PROGRESS,
        SyncErrorCode.INVALID_STATE,
    ):
        http_code = status.HTTP_409_CONFLICT
    else:
        http_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    return Response(payload, status=http_code)


def _broker_error_response(exc: BrokerError) -> Response:
    """Map a :class:`BrokerError` into the standard error envelope.

    Identical mapping to broker/views.py (contracts §7.4): ``BROKER_UNAVAILABLE``
    / ``NETWORK_ERROR`` surface as HTTP 503; every other broker error returns
    HTTP 200 with a machine-readable ``error_code`` + ``provider``.
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
    logger.exception("Unexpected sync API failure")
    return Response(
        {
            "status": "error",
            "message": "Internal error",
            "data": None,
            "errors": None,
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _invalid_request_response(message: str) -> Response:
    """400 envelope carrying the ``invalid_request`` sync error code (§7.4)."""
    return Response(
        {
            "status": "error",
            "message": message,
            "error_code": SyncErrorCode.INVALID_REQUEST.value,
            "data": None,
            "errors": None,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def _engine_not_running_response() -> Response:
    """503 envelope — engine absent/stopped (contracts §7.4)."""
    return Response(
        {
            "status": "error",
            "message": "Sync engine is not running",
            "error_code": SyncErrorCode.ENGINE_NOT_RUNNING.value,
            "data": None,
            "errors": None,
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
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


def _parse_timestamp(value) -> datetime:
    """Parse an epoch-ms or ISO-8601 timestamp (mirrors ticks_views.py:85-118).

    Raises:
        ValueError: when the value is not epoch milliseconds or ISO-8601, or
            is out of range for the platform ``time_t``.
    """
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            # e.g. float("1e999") — out of range for the platform time_t.
            raise ValueError("timestamp is out of range") from exc
    if isinstance(value, str):
        # Numeric string (epoch milliseconds) — e.g. "1750000000000".
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "timestamp must be epoch milliseconds or ISO-8601"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raise ValueError("timestamp must be epoch milliseconds or ISO-8601")


def _parse_optional_timestamp(value) -> datetime | None:
    """Parse an optional epoch-ms / ISO-8601 timestamp; ``None`` when absent."""
    if value is None or value == "":
        return None
    return _parse_timestamp(value)


def _parse_limit(
    query_params, default: int, maximum: int, name: str = "limit"
) -> int:
    """Parse and clamp an integer query parameter to ``1..maximum``."""
    try:
        value = int(query_params.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _get_engine():
    """Return the registered engine instance (lazy import — Phase 0 dead code).

    The engine module pulls the numpy-backed storage chain; importing it here
    lazily keeps ``import forex.sync.views`` light at Django app-load time.
    """
    from forex.sync.engine import get_engine

    return get_engine()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sync_status(request):
    """
    GET /api/sync/status/ — engine + per-symbol window status (contracts §7.1).

    Never crashes when the engine is off or the provider is unresolvable:
    the envelope is returned with ``engine_running: false`` and/or the
    provider error in the error channel (mirrors ``provider_status``,
    broker/views.py:171-224).
    """
    config = SyncConfig.from_env()
    trading_config = TradingConfig.from_env()
    engine = _get_engine()
    engine_running = engine is not None and engine.running

    data = {
        "engine_running": engine_running,
        "active_provider": trading_config.active_provider,
        "mode": trading_config.mode.value,
        "window": {
            "max_ticks": config.window_max_ticks,
            "max_age_days": config.window_max_days,
            "segment_capacity": config.segment_capacity,
            "record_layout": config.record_layout,
        },
        "symbols": {},
    }

    # Resolve the active provider (best-effort — the engine may be off).
    try:
        provider = TradingProviderFactory.get_active(config=trading_config)
        data["active_provider"] = provider.name
        data["mode"] = provider.mode.value
    except BrokerError as exc:
        # Unknown provider (or an infrastructure-level failure) — surface the
        # error envelope alongside the env-derived status summary.
        return Response(
            {
                "status": "error",
                "message": str(exc),
                "error_code": exc.code.value,
                "provider": exc.provider,
                "data": data,
                "errors": None,
            },
            status=status.HTTP_200_OK,
        )
    except Exception:  # noqa: BLE001 — never crash the status endpoint
        return _internal_error_response()

    if engine is not None:
        states = engine.state
        stats = engine.stats()
        for symbol in states:
            window = stats.get(symbol)
            data["symbols"][symbol] = {
                "state": states[symbol].value,
                "count": window.count if window else 0,
                "oldest_ts": _json_safe(window.oldest_ts) if window else None,
                "newest_ts": _json_safe(window.newest_ts) if window else None,
                "segment_count": window.segment_count if window else 0,
                "evictions": window.evictions if window else 0,
                "duplicates": window.duplicates if window else 0,
            }

    return Response(
        {
            "status": "success",
            "message": "Sync engine status",
            "data": data,
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sync_ticks(request):
    """
    GET /api/sync/ticks/?symbol=&start=&end=&limit= — query the engine window
    (contracts §7.2).

    ``symbol`` is required (``^[A-Z0-9_]{1,20}$``); ``start``/``end`` accept
    epoch ms or ISO-8601 (defaults: oldest/newest in the symbol's window);
    ``limit`` defaults to 500 and is clamped to 1..2000. 400 on invalid
    params; 503 ``engine_not_running`` when the engine is absent/stopped.
    """
    symbol = _validate_symbol(request.query_params.get("symbol", ""))
    if symbol is None:
        return _invalid_request_response(
            "symbol is required (1-20 chars of A-Z, 0-9 or underscore)"
        )

    engine = _get_engine()
    if engine is None or not engine.running:
        return _engine_not_running_response()

    try:
        start = _parse_optional_timestamp(request.query_params.get("start"))
    except ValueError as exc:
        return _invalid_request_response(f"start: {exc}")
    try:
        end = _parse_optional_timestamp(request.query_params.get("end"))
    except ValueError as exc:
        return _invalid_request_response(f"end: {exc}")

    limit = _parse_limit(
        request.query_params, TICKS_LIMIT_DEFAULT, TICKS_LIMIT_MAX
    )

    # Defaults: oldest/newest in the symbol's window (contracts §7.2). When
    # the window is empty, fall back to the window boundaries (spec §1.3).
    config = SyncConfig.from_env()
    stats = engine.stats().get(symbol)
    now = datetime.now(timezone.utc)
    if start is None:
        start = (
            stats.oldest_ts
            if stats is not None and stats.oldest_ts is not None
            else now - config.max_age
        )
    if end is None:
        end = (
            stats.newest_ts
            if stats is not None and stats.newest_ts is not None
            else now
        )
    if end < start:
        return _invalid_request_response("end must be >= start")

    try:
        ticks = engine.query_range(start, end, limit, symbol)
    except SyncError as exc:
        return _sync_error_response(exc)
    except Exception:  # noqa: BLE001 — log full detail server-side
        return _internal_error_response()

    return Response(
        {
            "status": "success",
            "message": "Sync window ticks",
            "data": {
                "symbol": symbol,
                "start": _json_safe(start),
                "end": _json_safe(end),
                "limit": limit,
                "returned": len(ticks),
                "ticks": _records(ticks),
            },
            "errors": None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sync_backfill(request):
    """
    POST /api/sync/backfill/ — trigger historical backfill (staff only,
    contracts §7.3).

    Body: ``{"symbol": "EURUSD", "days": 60}``. ``days`` defaults to
    ``SYNC_WINDOW_MAX_DAYS`` (60) and is clamped to 1..60. ``engine.backfill``
    is blocking (spec §5.3), so the worker is kicked on a background thread
    and the endpoint returns 202 Accepted immediately with the accepted-state
    snapshot (progress is observable via ``backfill_progress`` events, §6.5).

    409 ``backfill_in_progress`` when a backfill is already active for the
    symbol; 503 ``engine_not_running`` when the engine is off; 403 for
    non-staff (mirrors ``provider_switch``, broker/views.py:465-476).
    """
    if not getattr(request.user, "is_staff", False):
        return Response(
            {
                "status": "error",
                "message": "Staff privileges are required to trigger backfill",
                "data": None,
                "errors": None,
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    symbol = _validate_symbol(request.data.get("symbol"))
    if symbol is None:
        return _invalid_request_response(
            "symbol is required (1-20 chars of A-Z, 0-9 or underscore)"
        )

    days = BACKFILL_DAYS_DEFAULT
    raw_days = request.data.get("days")
    if raw_days is not None:
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return _invalid_request_response("days must be an integer")
        if not 1 <= days <= BACKFILL_DAYS_MAX:
            return _invalid_request_response(
                f"days must be between 1 and {BACKFILL_DAYS_MAX}"
            )

    engine = _get_engine()
    if engine is None or not engine.running:
        return _engine_not_running_response()

    # Fast-path state gate — surfaces 409/409 synchronously. The engine
    # re-checks inside ``backfill()`` (the authoritative gate); this pre-check
    # only avoids accepting a backfill that is guaranteed to fail.
    from forex.sync.engine import SyncState

    state = engine.state.get(symbol)
    if state is None:
        return _invalid_request_response(
            f"symbol {symbol!r} is not tracked by the sync engine"
        )
    if state in (SyncState.SYNCING_HISTORY, SyncState.BACKFILLING):
        return _sync_error_response(
            SyncError(
                SyncErrorCode.BACKFILL_IN_PROGRESS,
                f"backfill already active for {symbol} (state={state.value})",
            )
        )
    if state == SyncState.LIVE:
        return _sync_error_response(
            SyncError(
                SyncErrorCode.INVALID_STATE,
                f"backfill not allowed for {symbol} in state {state.value} "
                "(contracts §5.2: LIVE → SYNCING_HISTORY is illegal)",
            )
        )

    # Accepted-state snapshot (contracts §7.3). pages_total mirrors the
    # engine's own estimate (window_seconds / backfill_batch_size, rounded up).
    config = SyncConfig.from_env()
    pages_total = max(1, -(-(days * 86_400) // config.backfill_batch_size))
    target_state = (
        "backfilling" if state == SyncState.GAP else "syncing_history"
    )

    def _run() -> None:
        try:
            engine.backfill(symbol, days)
        except Exception:  # noqa: BLE001 — background kick is best-effort
            logger.exception(
                "sync backfill failed for %s (%d days)", symbol, days
            )

    threading.Thread(
        target=_run,
        name=f"sync-api-backfill-{symbol}",
        daemon=True,
    ).start()

    return Response(
        {
            "status": "success",
            "message": f"Backfill accepted for {symbol} ({days} days)",
            "data": {
                "symbol": symbol,
                "days": days,
                "state": target_state,
                "pages_done": 0,
                "pages_total": pages_total,
                "ticks_committed": 0,
                "oldest_ts": None,
                "newest_ts": None,
            },
            "errors": None,
        },
        status=status.HTTP_202_ACCEPTED,
    )