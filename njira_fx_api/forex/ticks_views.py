"""
Pepperstone pipeline API endpoints (JWT-protected).

All endpoints require a valid Bearer token (global DRF default is
``IsAuthenticated``; it is also declared explicitly per view for clarity).

Contract (all under ``/api/``):

    POST /api/ticks/        Ingest a tick (fallback ingestion path).
    GET  /api/chart-data/   Recent ticks for charting.
    GET  /api/mtf-status/   Multi-timeframe alignment + master signal.
    POST /api/trade/        Place a trade through the risk gate + the active
                            provider (provider-independent, via
                            TradingProviderFactory).

Response envelope: ``{status, message, data, errors}`` — ``status`` is
``success`` | ``error`` | ``risk_blocked``.
"""
from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, time, timedelta, timezone

from django.core.cache import cache
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .broker.errors import BrokerError
from .broker.factory import TradingProviderFactory
from .clickhouse.connection import get_clickhouse_client
from .clickhouse.schema import get_ohlc_bars, get_recent_ticks, insert_ticks_batch
from .mtf_strategy import MultiTimeframeStrategy, TIMEFRAMES, SMA_PERIOD
from .permissions import IsStaffOrServiceAccount
from .realtime import broadcast_tick
from .risk_manager import RiskManager

logger = logging.getLogger("forex.ticks")

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]{1,20}$")

# Timeframe label -> aggregation window in minutes (mirrors mtf_strategy).
MTF_TIMEFRAMES: dict[str, int] = {
    "1M": 1,
    "5M": 5,
    "15M": 15,
    "30M": 30,
    "1H": 60,
    "4H": 240,
    "1D": 1440,
}

CHART_DATA_LIMIT_DEFAULT = 500
CHART_DATA_LIMIT_MAX = 2000
MTF_BARS_LIMIT = 30

# ---------------------------------------------------------------------------
# Env-driven trade limits (auth-engineer — Security Gate 1 #6 remediation).
# Volumes are expressed in 0.01-lot units (100000 units = 1000 lots).
# ---------------------------------------------------------------------------
TRADE_MAX_VOLUME = int(os.environ.get("TRADE_MAX_VOLUME", "100000"))
TRADE_DAILY_LIMIT = int(os.environ.get("TRADE_DAILY_LIMIT", "20"))
TRADE_DAILY_VOLUME_LIMIT = int(
    os.environ.get("TRADE_DAILY_VOLUME_LIMIT", "100000")
)


def _validate_symbol(value: str) -> str | None:
    """Return the symbol when it matches the allowlist, else ``None``."""
    if not isinstance(value, str):
        return None
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        return None
    return symbol


def _parse_timestamp(value) -> datetime:
    """
    Parse an optional tick timestamp: epoch milliseconds (int/float or a
    numeric string — DRF CharField coerces JSON numbers to str) or an ISO-8601
    string. Falls back to now (UTC) when absent.
    """
    if value is None or value == "":
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            # e.g. float("1e999") — out of range for the platform time_t.
            raise serializers.ValidationError(
                "timestamp is out of range"
            ) from exc
    if isinstance(value, str):
        # Numeric string (epoch milliseconds) — e.g. "1750000000000".
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise serializers.ValidationError(
                "timestamp must be epoch milliseconds or ISO-8601"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raise serializers.ValidationError(
        "timestamp must be epoch milliseconds or ISO-8601"
    )


# ---------------------------------------------------------------------------
# Request serializers
# ---------------------------------------------------------------------------
#: Maximum allowed future skew for an ingested tick timestamp (clock drift).
MAX_TIMESTAMP_SKEW = timedelta(minutes=5)


class TickIngestSerializer(serializers.Serializer):
    """Validates ``POST /api/ticks/`` payloads."""

    symbol = serializers.CharField(max_length=20)
    bid = serializers.FloatField()
    ask = serializers.FloatField()
    spread = serializers.FloatField()
    digits = serializers.IntegerField()
    timestamp = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def validate_symbol(self, value: str) -> str:
        symbol = _validate_symbol(value)
        if symbol is None:
            raise serializers.ValidationError(
                "symbol must be 1-20 chars of A-Z, 0-9 or underscore"
            )
        return symbol

    def validate(self, attrs: dict) -> dict:
        bid = attrs.get("bid")
        ask = attrs.get("ask")
        spread = attrs.get("spread")
        digits = attrs.get("digits")

        # Reject non-finite floats explicitly: NaN passes ``bid <= 0`` and
        # ``bid >= ask`` (all NaN comparisons are False) and would poison the
        # spread risk gate / MTF strategy with garbage rows.
        if not all(math.isfinite(x) for x in (bid, ask, spread)):
            raise serializers.ValidationError(
                "bid, ask and spread must be finite numbers"
            )
        if bid <= 0:
            raise serializers.ValidationError("bid must be greater than 0")
        if ask <= 0:
            raise serializers.ValidationError("ask must be greater than 0")
        if bid >= ask:
            raise serializers.ValidationError("bid must be less than ask")
        if spread < 0:
            raise serializers.ValidationError("spread must be non-negative")
        if not 1 <= digits <= 8:
            raise serializers.ValidationError("digits must be between 1 and 8")

        ts = _parse_timestamp(attrs.get("timestamp"))
        if ts > datetime.now(timezone.utc) + MAX_TIMESTAMP_SKEW:
            raise serializers.ValidationError(
                "timestamp must not be in the future (max 5 minutes skew)"
            )
        attrs["timestamp"] = ts
        return attrs


class TradeRequestSerializer(serializers.Serializer):
    """Validates ``POST /api/trade/`` payloads."""

    symbol = serializers.CharField(max_length=20)
    side = serializers.ChoiceField(choices=["BUY", "SELL"])
    volume = serializers.IntegerField(min_value=1, max_value=TRADE_MAX_VOLUME)

    def validate_symbol(self, value: str) -> str:
        symbol = _validate_symbol(value)
        if symbol is None:
            raise serializers.ValidationError(
                "symbol must be 1-20 chars of A-Z, 0-9 or underscore"
            )
        return symbol


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def _check_and_record_trade_caps(user, volume: int) -> str | None:
    """
    Enforce per-user daily trade/volume caps, returning the block reason or
    ``None`` when the trade is within limits.

    Tracked via Django cache under ``trade_caps_<user.id>_<date>`` with an
    end-of-day (UTC) TTL so the caps reset every day automatically.

    - ``TRADE_DAILY_LIMIT`` (default 20)  — max trades per day.
    - ``TRADE_DAILY_VOLUME_LIMIT`` (default 100000, 0.01-lot units) — max
      total volume per day.

    The counter is incremented only after the trade passes the risk gate and
    is submitted to the execution engine, so blocked attempts do not consume
    quota.
    """
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    key = f"trade_caps_{user.id}_{today}"
    # Cache entry expires at midnight UTC so caps reset each day.
    end_of_day = datetime.combine(
        now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    timeout = max(60, int((end_of_day - now).total_seconds()))

    caps = cache.get(key)
    if caps is None:
        caps = {"trades": 0, "volume": 0}
        cache.add(key, caps, timeout)

    if caps["trades"] >= TRADE_DAILY_LIMIT:
        return (
            f"Daily trade limit reached ({TRADE_DAILY_LIMIT} trades/day)"
        )
    if caps["volume"] + volume > TRADE_DAILY_VOLUME_LIMIT:
        return (
            "Daily volume limit would be exceeded "
            f"({TRADE_DAILY_VOLUME_LIMIT} units/day)"
        )

    caps["trades"] += 1
    caps["volume"] += volume
    cache.set(key, caps, timeout)
    return None


@api_view(["POST"])
@permission_classes([IsStaffOrServiceAccount])
def ingest_tick(request):
    """
    POST /api/ticks/ — ingest a single tick (fallback path).

    Restricted to the tick-ingest service account (``X-API-Key`` matching
    ``TICKS_INGEST_API_KEY``) or staff users (Security Gate 1 HIGH #7).

    Body: {symbol, bid, ask, spread, digits, timestamp?}
    The symbol is allowlist-validated, prices/spread/digits/timestamp are
    sanity-checked, and the ClickHouse helpers parameterize all values — the
    symbol is never interpolated into SQL.
    """
    serializer = TickIngestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                "status": "error",
                "message": "Invalid tick payload",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    data = serializer.validated_data
    try:
        client = get_clickhouse_client()
        insert_ticks_batch(
            client,
            [
                (
                    data["symbol"],
                    data["bid"],
                    data["ask"],
                    data["spread"],
                    data["digits"],
                    data["timestamp"],
                )
            ],
        )
    except Exception:  # noqa: BLE001 — log full detail server-side, leak nothing
        logger.exception("Tick ingest failed")
        return Response(
            {"status": "error", "message": "Internal error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # Fire-and-forget realtime broadcast (throttled to 1/sec per symbol so a
    # high-frequency ingest stream cannot flood Pusher). Failures are logged
    # inside broadcast_tick and never affect the ingest response.
    broadcast_tick(
        data["symbol"],
        {
            "symbol": data["symbol"],
            "bid": data["bid"],
            "ask": data["ask"],
            "spread": data["spread"],
            "digits": data["digits"],
            "timestamp": data["timestamp"].isoformat()
            if hasattr(data["timestamp"], "isoformat")
            else str(data["timestamp"]),
        },
    )

    return Response({"status": "success"}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def chart_data(request):
    """
    GET /api/chart-data/?symbol=EURUSD&limit=500 — recent ticks for charting.

    ``limit`` is clamped to 1..2000. Returns
    ``{status, symbol, count, data: [{bid, ask, spread, time}]}``.
    """
    symbol = _validate_symbol(request.query_params.get("symbol", ""))
    if symbol is None:
        return Response(
            {
                "status": "error",
                "message": "symbol is required (1-20 chars of A-Z, 0-9 or underscore)",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        limit = int(request.query_params.get("limit", CHART_DATA_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = CHART_DATA_LIMIT_DEFAULT
    limit = max(1, min(limit, CHART_DATA_LIMIT_MAX))

    client = get_clickhouse_client()
    ticks = get_recent_ticks(client, symbol, limit)
    data = [
        {
            "bid": tick["bid"],
            "ask": tick["ask"],
            "spread": tick["spread"],
            "time": tick["time_str"],
        }
        for tick in ticks
    ]
    return Response(
        {
            "status": "success",
            "symbol": symbol,
            "count": len(data),
            "data": data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mtf_status(request):
    """
    GET /api/mtf-status/?symbol=EURUSD — multi-timeframe alignment snapshot.

    Reads the cached master signal (``macro_signal_<SYMBOL>``, published by
    the ``mtf_aggregator`` command) and computes per-timeframe SMA alignment
    against live ClickHouse bars.
    """
    symbol = _validate_symbol(request.query_params.get("symbol", ""))
    if symbol is None:
        return Response(
            {
                "status": "error",
                "message": "symbol is required (1-20 chars of A-Z, 0-9 or underscore)",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    master_signal = cache.get(f"macro_signal_{symbol}", "HOLD")
    strategy = MultiTimeframeStrategy()
    client = get_clickhouse_client()
    if client is None:
        return Response(
            {"ok": False, "code": "no_clickhouse"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    timeframes = {}
    for label, minutes in MTF_TIMEFRAMES.items():
        bars = get_ohlc_bars(client, symbol, minutes, limit=MTF_BARS_LIMIT)
        sma = strategy.calculate_simple_moving_average(bars, SMA_PERIOD)
        current_close = bars[-1]["close"] if bars else 0
        if master_signal == "BUY":
            aligned = sma is not None and current_close > sma
        elif master_signal == "SELL":
            aligned = sma is not None and current_close < sma
        else:
            aligned = False
        timeframes[label] = {
            "sma": sma,
            "current_value": current_close,
            "aligned": aligned,
            "chart_history": [{"time": b["time"], "close": b["close"]} for b in bars],
        }

    return Response(
        {
            "status": "success",
            "symbol": symbol,
            "masterSignal": master_signal,
            "timeframes": timeframes,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsStaffOrServiceAccount])
def place_trade(request):
    """
    POST /api/trade/ — place a trade through the risk gate.

    Restricted to staff or the service account (Security Gate 1 HIGH #6) —
    regular registered users get 403.

    Body: {symbol, side: BUY|SELL, volume: 1..TRADE_MAX_VOLUME}

    Per-user daily trade/volume caps are enforced (``trade_caps_<user.id>_<date>``
    in the Django cache). The risk gate runs AFTER the caps (spread from the
    latest tick + daily drawdown). When blocked:
    ``{status: 'risk_blocked', reason}`` with HTTP 200. Otherwise the order
    is routed through the active provider via ``TradingProviderFactory``
    (provider-independent — provider classes are never imported here).
    Broker failures return the error envelope with a machine-readable
    ``error_code``; unexpected failures are logged and returned as
    ``Internal error`` (never a hang, never a traceback).
    """
    serializer = TradeRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                "status": "error",
                "message": "Invalid trade payload",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    data = serializer.validated_data
    symbol: str = data["symbol"]
    side: str = data["side"]
    volume: int = data["volume"]

    # --- Per-user daily caps (Security Gate 1 HIGH #6) ---------------------
    reason = _check_and_record_trade_caps(request.user, volume)
    if reason is not None:
        return Response(
            {"status": "risk_blocked", "reason": reason},
            status=status.HTTP_200_OK,
        )

    # --- Risk gate: spread from the newest tick, then daily drawdown. ------
    client = get_clickhouse_client()
    ticks = get_recent_ticks(client, symbol, 1)
    if not ticks:
        return Response(
            {
                "status": "risk_blocked",
                "reason": f"No market data available for {symbol} to evaluate spread",
            },
            status=status.HTTP_200_OK,
        )
    current_spread_pips = ticks[-1]["spread"]  # newest tick is last (chronological)

    risk_manager = RiskManager()
    allowed, reason = risk_manager.is_trading_allowed(current_spread_pips)
    if not allowed:
        return Response(
            {"status": "risk_blocked", "reason": reason},
            status=status.HTTP_200_OK,
        )

    # --- Execution via the active provider (TradingProviderFactory). -------
    # Trades always route through the provider-neutral factory — provider
    # classes are never imported directly in the API layer (backend-dev).
    try:
        provider = TradingProviderFactory.get_active()
        order = provider.place_order(symbol, side, volume)
    except BrokerError as exc:
        return Response(
            {
                "status": "error",
                "message": str(exc),
                "error_code": exc.code.value,
            },
            status=status.HTTP_200_OK,
        )
    except Exception:  # noqa: BLE001 — log full detail server-side, leak nothing
        logger.exception("Trade execution failed")
        return Response(
            {"status": "error", "message": "Internal error"},
            status=status.HTTP_200_OK,
        )

    return Response(
        {
            "status": "success",
            "data": {
                "order_id": order.external_order_id,
                "status": order.status.value,
            },
        },
        status=status.HTTP_200_OK,
    )