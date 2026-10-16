"""
Market-data API endpoints for the Next.js dashboard.

Four read-only endpoints, all backed entirely by REAL ClickHouse data (no
fabricated values). They are co-located with the pepperstone pipeline views
(``forex.ticks_views``) and registered in the same urlconf as
``/api/chart-data/``.

    GET /api/market/candles/?symbol=EURUSD&timeframe=15m&limit=200
    GET /api/market/analyze/<symbol>/
    GET /api/signals/live/
    GET /api/signals/history/?limit=50
    GET /api/strategies/performance/

Contract notes:
    * Responses are plain JSON matching the fixed contract (no DRF
      ``{status, message, data, errors}`` envelope) so the dashboard can
      consume them directly.
    * Reads are best-effort: when ClickHouse is unreachable or a table is
      empty/missing, endpoints return the contract's empty shape
      (e.g. ``{"candles": []}``) instead of an error, so the UI renders an
      empty state rather than crashing.
    * Symbol normalization strips the legacy ``frx``/``FRX`` prefix and
      upper-cases (see ``forex.common.normalize_symbol``).
    * Inputs are validated: symbol allowlist regex, timeframe whitelist,
      limit clamped to 1..1000.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from forex.clickhouse.connection import get_clickhouse_client
from forex.clickhouse.schema import TICKS_TABLE, get_candles_from_ch, get_signals_v2
from forex.common import is_valid_symbol, normalize_symbol
from forex.models import ChartDrawing
from forex.realtime import broadcast_event
from forex.analysis.signal_lifecycle import (
    SignalState,
    Signal,
    _STATUS_V1_BY_STATE,
    _SIGNAL_TYPE_BY_STRATEGY,
    instrument_for_symbol,
    execution_defaults,
    risk_defaults,
    compute_rr,
    validation_for_signal,
    get_global_lifecycle,
)

logger = logging.getLogger("forex.market_api")


# ---------------------------------------------------------------------------
# Trading symbol / asset configuration (env-driven, per-process cached)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _load_trading_config() -> dict:
    """
    Resolve the trading symbol list and asset metadata from the environment.

    ``TRADING_SYMBOLS`` is the primary source (comma-separated), falling back
    to the legacy ``PEPPERSTONE_SYMBOLS``. ``TRADING_ASSETS`` is a JSON array of
    metadata objects; on any parse failure it degrades to an empty list rather
    than raising, so the endpoint always returns a valid contract shape.

    Cached per-process (``lru_cache``) because the values are static for the
    lifetime of a worker.
    """
    symbols_raw = os.environ.get("TRADING_SYMBOLS") or os.environ.get(
        "PEPPERSTONE_SYMBOLS", ""
    )
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    assets: list = []
    assets_raw = os.environ.get("TRADING_ASSETS", "")
    if assets_raw:
        try:
            parsed = json.loads(assets_raw)
            if isinstance(parsed, list):
                assets = parsed
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("TRADING_ASSETS is not valid JSON, using []: %s", exc)
            assets = []

    return {"symbols": symbols, "assets": assets}

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------
# Timeframe label -> bucket size in minutes (for ClickHouse INTERVAL).
# Whitelisted per contract: "1m","5m","15m","1h","4h".
TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
}

CANDLES_LIMIT_MIN = 1
CANDLES_LIMIT_MAX = 1000
CANDLES_LIMIT_DEFAULT = 200

# Number of 15m buckets used for SMA20/SMA50 in /api/market/analyze/.
ANALYZE_BUCKETS = 200

SIGNALS_LIVE_LIMIT = 50
SIGNALS_HISTORY_DEFAULT = 50
SIGNALS_HISTORY_MAX = 1000

# ClickHouse signals table — v2 is the only signals store.
SIGNALS_V2_TABLE = os.environ.get(
    "CLICKHOUSE_SIGNALS_V2_TABLE", "njra_markets.trading_signals_v2"
)


# ---------------------------------------------------------------------------
# ClickHouse helpers (best-effort; never raise to the caller)
# ---------------------------------------------------------------------------
def _safe_client():
    """
    Return a ClickHouse client or ``None`` when one cannot be obtained.

    Wrapping ``get_clickhouse_client`` lets the endpoints degrade to the
    contract's empty shape when ClickHouse is unreachable or unconfigured,
    instead of returning a 500.
    """
    try:
        return get_clickhouse_client()
    except Exception as exc:  # noqa: BLE001 — best-effort, degrade gracefully
        logger.warning("ClickHouse client unavailable: %s", exc)
        return None


def _fetch_ohlc(client, symbol: str, minutes: int, limit: int) -> list[dict]:
    """
    Aggregate OHLC bars for ``symbol`` from the ticks table, bucketed into
    ``minutes`` intervals, most-recent ``limit`` bars, in chronological order.

    Uses the ``bid`` column for OHLC (consistent with
    ``forex.clickhouse.schema.get_ohlc_bars``). ``time`` is returned as an
    epoch-seconds integer. Returns ``[]`` on any failure.
    """
    # TICKS_TABLE is a trusted environment constant (never user input); the
    # symbol is bound via parameters.
    sql = (
        "SELECT "
        "toUnixTimestamp(toStartOfInterval(timestamp, "
        "INTERVAL {tf:UInt32} MINUTE)) AS time, "
        "argMin(bid, timestamp) AS open, "
        "max(bid) AS high, "
        "min(bid) AS low, "
        "argMax(bid, timestamp) AS close "
        f"FROM {TICKS_TABLE} "
        "WHERE symbol = {sym:String} "
        "GROUP BY time "
        "ORDER BY time DESC "
        "LIMIT {lim:UInt32}"
    )
    try:
        result = client.query(
            sql, parameters={"tf": minutes, "sym": symbol, "lim": limit}
        )
    except Exception as exc:  # noqa: BLE001 — best-effort read, never crash
        logger.warning("Could not read OHLC candles for %s: %s", symbol, exc)
        return []
    candles: list[dict] = []
    # Query returns newest-first; reverse for chronological order.
    for time_epoch, o, h, l, c in reversed(result.result_set):
        candles.append(
            {
                "time": int(time_epoch),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
            }
        )
    return candles


def _fetch_signals(client, limit: int) -> list[dict]:
    """
    Read the most recent ``limit`` rows from the v2 signals table.
    No legacy fallback — v2 is the only signals store.
    Returns ``[]`` when the table is missing/empty or the query fails.
    """
    sql = (
        "SELECT signal_id, symbol, direction, state, created_at, "
        "entry, stop_loss, take_profit, strategy "
        f"FROM {SIGNALS_V2_TABLE} "
        "ORDER BY created_at DESC "
        "LIMIT {lim:UInt32}"
    )
    try:
        result = client.query(sql, parameters={"lim": limit})
    except Exception as exc:  # noqa: BLE001 — best-effort read, never crash
        logger.warning("Could not read signals from %s: %s", SIGNALS_V2_TABLE, exc)
        return []
    signals: list[dict] = []
    for row in result.result_set:
        signals.append(
            {
                "timestamp": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4]),
                "pair": row[1],
                "signal": row[2],
                "entry": float(row[5]) if row[5] is not None else None,
                "stop_loss": float(row[6]) if row[6] is not None else None,
                "take_profit": float(row[7]) if row[7] is not None else None,
                "strategy": row[8] or "",
            }
        )
    return signals


def _fetch_strategy_performance(client) -> list[dict]:
    """
    Group signals by strategy and count them. Wins/losses are reported as 0
    because the signals table carries no outcome column (honest zeros only).
    No legacy fallback — v2 is the only signals store.
    Returns ``[]`` when there are no signals or the query fails.
    """
    sql = (
        "SELECT strategy, count() AS cnt "
        f"FROM {SIGNALS_V2_TABLE} "
        "GROUP BY strategy "
        "ORDER BY cnt DESC"
    )
    try:
        result = client.query(sql)
    except Exception as exc:  # noqa: BLE001 — best-effort read, never crash
        logger.warning(
            "Could not read strategy performance from %s: %s", SIGNALS_V2_TABLE, exc
        )
        return []
    strategies: list[dict] = []
    for strategy, cnt in result.result_set:
        strategies.append(
            {
                "strategy": strategy,
                "signals": int(cnt),
                "wins": 0,
                "losses": 0,
            }
        )
    return strategies


# Malaysian SNR analyzers that should be included in the SNR endpoint
SNR_ANALYZERS = frozenset({"snr", "breakout", "marriage", "engulfing", "flip", "qml", "confluence"})


def _signal_dict_to_v1(sig: dict) -> dict:
    """Convert a raw signal dict (from get_signals_v2) to v1 envelope format."""
    # Parse confidence_evidence JSON if it's a string
    evidence = sig.get("confidence_evidence", {})
    if isinstance(evidence, str):
        import json
        try:
            evidence = json.loads(evidence)
        except (json.JSONDecodeError, TypeError):
            evidence = {}

    # Determine the signal type from strategy
    strategy = (sig.get("strategy") or "").strip().lower()
    signal_type = _SIGNAL_TYPE_BY_STRATEGY.get(strategy, strategy or "price_breakout")

    # Build setup key for idempotency
    setup = f"{sig['symbol']}:{sig['direction']}:{strategy or 'unknown'}"

    # Parse created_at
    created_at_str = sig.get("created_at", "")
    try:
        from datetime import datetime
        if isinstance(created_at_str, str):
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        else:
            created_at = datetime.now(timezone.utc)
    except Exception:
        created_at = datetime.now(timezone.utc)

    # Convert to UTC and format with Z suffix
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    created_z = created_at.isoformat().replace("+00:00", "Z")
    unix_ts = int(created_at.timestamp())

    # Map state to v1 status
    state_val = sig.get("state", "CANDIDATE")
    try:
        state_enum = SignalState(state_val)
    except ValueError:
        state_enum = SignalState.CANDIDATE
    status = _STATUS_V1_BY_STATE.get(state_enum, "PENDING")

    # Instrument
    inst = instrument_for_symbol(sig["symbol"])
    precision = int(inst.get("precision", 5))

    # Execution defaults
    ex = execution_defaults(created_at)

    # Risk defaults
    rk = risk_defaults()

    # RR
    rr = sig.get("risk_reward_ratio") or compute_rr(
        sig.get("entry", 0), sig.get("stop_loss", 0), sig.get("take_profit", 0)
    )

    # Timeframe
    timeframe = (sig.get("timeframe") or "").strip() or "multi"

    # Source tags
    tags = list(dict.fromkeys(
        t for t in [sig.get("strategy"), sig.get("timeframe"), sig.get("market_regime")] if t
    ))

    return {
        "schemaVersion": "1.0",
        "id": sig["signal_id"],
        "idempotencyKey": f"{setup}:{timeframe}:{unix_ts}",
        "createdAt": created_z,
        "status": status,
        "source": {
            "platform": "njira_fx",
            "signalType": signal_type,
            "tags": tags,
        },
        "target": {
            "platform": "njira_fx",
            "account": "default",
            "clientOrderId": sig["signal_id"],
        },
        "instrument": {
            "coin": inst.get("coin"),
            "symbol": inst.get("symbol"),
            "exchange": inst.get("exchange"),
            "pricePrecision": precision,
            "quantityPrecision": 6 if inst.get("exchange") == "binance" else 2,
        },
        "bias": {
            "direction": sig["direction"],
            "side": sig["direction"],
            "confidence": sig.get("confidence_score", 0),
            "strength": sig.get("strength") or "NORMAL",
        },
        "execution": {
            "orderType": ex.get("orderType"),
            "limitPrice": sig.get("entry", 0),
            "estimatedEntryTime": created_z,
            "timeInForce": ex.get("timeInForce"),
            "expiresAt": str(ex.get("expiresAt")).replace("+00:00", "Z"),
            "postOnly": ex.get("postOnly"),
            "reduceOnly": False,
        },
        "risk": {
            "stopLoss": sig.get("stop_loss", 0),
            "takeProfit": sig.get("take_profit", 0),
            "riskRewardRatio": rr,
            "leverage": rk.get("leverage"),
            "positionSize": rk.get("positionSize"),
            "positionSizeType": rk.get("positionSizeUnit"),
            "maxSlippageBps": rk.get("maxSlippageBps"),
        },
        "validation": validation_for_signal(
            sig["direction"],
            sig.get("entry", 0),
            sig.get("stop_loss", 0),
            sig.get("take_profit", 0),
            rr,
        ),
    }


def _fetch_snr_signals(client, symbol: str | None, limit: int) -> list[dict]:
    """
    Fetch recent signals and filter for Malaysian SNR analyzers.
    Returns signals in v1 envelope format.
    """
    from forex.clickhouse.schema import get_signals_v2

    # Fetch more signals to filter down (SNR signals may be interspersed)
    raw_signals = get_signals_v2(client, symbol, limit * 5) if client else []

    # Filter for SNR analyzers via confidence_evidence.analyzer
    snr_signals = []
    for sig in raw_signals:
        evidence = sig.get("confidence_evidence", {})
        if isinstance(evidence, str):
            import json
            try:
                evidence = json.loads(evidence)
            except (json.JSONDecodeError, TypeError):
                evidence = {}

        analyzer = (evidence.get("analyzer") or "").lower()
        # Also check source_strategies for combined signals
        source_strategies = sig.get("source_strategies", [])
        if isinstance(source_strategies, str):
            import json
            try:
                source_strategies = json.loads(source_strategies)
            except (json.JSONDecodeError, TypeError):
                source_strategies = []

        is_snr = analyzer in SNR_ANALYZERS or any(s in SNR_ANALYZERS for s in source_strategies)
        if is_snr:
            snr_signals.append(sig)

    # Limit to requested count
    snr_signals = snr_signals[:limit]

    # Convert to v1 format
    return [_signal_dict_to_v1(s) for s in snr_signals]


def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values, or ``None``."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _trend(sma20: float | None, sma50: float | None) -> str:
    """Derive trend from the two SMAs; ``None`` inputs yield ``"flat"``."""
    if sma20 is None or sma50 is None:
        return "flat"
    if sma20 > sma50:
        return "up"
    if sma20 < sma50:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
def market_candles(request):
    """
    GET /api/market/candles/?symbol=EURUSD&timeframe=15m&limit=200

    Aggregated OHLC candles from the ticks table. Unknown symbol / no data /
    unreachable ClickHouse -> 200 with ``"candles": []`` (UI empty state).
    Invalid symbol or unsupported timeframe -> 400.
    """
    symbol = normalize_symbol(request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    timeframe = request.query_params.get("timeframe", "15m")
    if timeframe not in TIMEFRAME_MINUTES:
        return Response({"error": "unsupported timeframe"}, status=400)

    try:
        limit = int(request.query_params.get("limit", CANDLES_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = CANDLES_LIMIT_DEFAULT
    limit = max(CANDLES_LIMIT_MIN, min(limit, CANDLES_LIMIT_MAX))

    client = _safe_client()
    candles = _fetch_ohlc(client, symbol, TIMEFRAME_MINUTES[timeframe], limit) if client else []

    return Response(
        {"symbol": symbol, "timeframe": timeframe, "candles": candles},
        status=200,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def market_analyze(request, symbol: str):
    """
    GET /api/market/analyze/<symbol>/

    Computes SMA20/SMA50 over the last ~200 15m buckets and a trend label.
    No data / unreachable ClickHouse -> 200 with nulls and ``"flat"`` trend.
    Invalid symbol -> 400.
    """
    sym = normalize_symbol(symbol)
    if not is_valid_symbol(sym):
        return Response({"error": "invalid symbol"}, status=400)

    client = _safe_client()
    bars = (
        _fetch_ohlc(client, sym, TIMEFRAME_MINUTES["15m"], ANALYZE_BUCKETS)
        if client
        else []
    )
    closes = [float(b["close"]) for b in bars]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    last_close = closes[-1] if closes else None

    return Response(
        {
            "symbol": sym,
            "last_close": last_close,
            "sma20": sma20,
            "sma50": sma50,
            "trend": _trend(sma20, sma50),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        status=200,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def signals_live(request):
    """
    GET /api/signals/live/

    Most recent signals (default 50). Missing/empty table -> ``{"signals": []}``.
    """
    client = _safe_client()
    signals = _fetch_signals(client, SIGNALS_LIVE_LIMIT) if client else []
    return Response({"signals": signals}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def signals_history(request):
    """
    GET /api/signals/history/?limit=50

    Most recent ``limit`` signals (clamped 1..1000). Missing/empty table ->
    ``{"signals": []}``.
    """
    try:
        limit = int(request.query_params.get("limit", SIGNALS_HISTORY_DEFAULT))
    except (TypeError, ValueError):
        limit = SIGNALS_HISTORY_DEFAULT
    limit = max(1, min(limit, SIGNALS_HISTORY_MAX))

    client = _safe_client()
    signals = _fetch_signals(client, limit) if client else []
    return Response({"signals": signals}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def strategies_performance(request):
    """
    GET /api/strategies/performance/

    Per-strategy signal counts with honest wins/losses (0 — the signals table
    has no outcome column). No signals -> ``{"strategies": []}``.
    """
    client = _safe_client()
    strategies = _fetch_strategy_performance(client) if client else []
    return Response({"strategies": strategies}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def symbols_config(request):
    """
    GET /api/symbols/

    Returns the provider-independent trading symbol list and asset metadata
    consumed by the dashboard (symbol selector, asset cards). Both values are
    resolved from ``TRADING_SYMBOLS`` / ``TRADING_ASSETS`` (with the legacy
    ``PEPPERSTONE_SYMBOLS`` fallback) and cached per-process.

    Contract: ``{"symbols": ["EURUSD", ...], "assets": [<TRADING_ASSETS>]}``.
    """
    cfg = _load_trading_config()
    return Response(
        {"symbols": cfg["symbols"], "assets": cfg["assets"]},
        status=200,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def realtime_test(request):
    """
    GET /api/realtime/test/

    Debug endpoint: broadcasts a ``ping`` event with ``{"message": "test"}`` on
    the ``njira-market`` channel and reports whether Pusher accepted it. Used to
    prove the Pusher credentials + auth signature are wired correctly. Returns
    ``{"sent": true|false}`` (never raises on broadcast failure).
    """
    sent = broadcast_event("njira-market", "ping", {"message": "test"})
    return Response(
        {
            "sent": sent,
            "hint": "subscribe to channel 'njira-market', events 'tick'",
        },
        status=200,
    )


# ---------------------------------------------------------------------------
# Chart drawings (TradingView-style annotations, persisted per symbol)
# ---------------------------------------------------------------------------
# ponytail: global per-symbol drawings (no user scoping) — this deployment is
# single-tenant; add a FK to the user when multi-user workspaces are needed.

_DRAWING_KINDS = {ChartDrawing.KIND_TREND, ChartDrawing.KIND_RECT}
_MAX_SHAPES_PER_SYMBOL = 500


def _shape_from_payload(raw: dict) -> dict | None:
    """Validate one shape dict; return a clean field dict or ``None``."""
    try:
        kind = str(raw.get("kind", ""))
        if kind not in _DRAWING_KINDS:
            return None
        coords = [float(raw[k]) for k in ("t1", "p1", "t2", "p2")]
        if any(c != c or c in (float("inf"), float("-inf")) for c in coords):
            return None
    except (TypeError, ValueError):
        return None
    return {"kind": kind, "t1": coords[0], "p1": coords[1], "t2": coords[2], "p2": coords[3]}


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def chart_drawings(request):
    """
    GET  /api/chart-drawings/?symbol=EURUSD  -> {"drawings": [<shape>...]}
    POST /api/chart-drawings/                body: {"symbol": "...", "shapes": [...]}

    Replace-all semantics: a POST stores exactly the submitted shape list for
    the symbol (clearing = POST an empty list). Invalid shapes are dropped;
    if none survive, the symbol's stored set is cleared (the client is the
    source of truth). Malformed requests -> 400.
    """
    symbol = normalize_symbol(request.data.get("symbol", "") if request.method == "POST"
                              else request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    if request.method == "GET":
        rows = ChartDrawing.objects.filter(symbol=symbol)
        return Response(
            {"symbol": symbol,
             "drawings": [
                 {"kind": r.kind, "t1": r.t1, "p1": r.p1, "t2": r.t2, "p2": r.p2}
                 for r in rows
             ]},
            status=200,
        )

    raw_shapes = request.data.get("shapes")
    if not isinstance(raw_shapes, list) or len(raw_shapes) > _MAX_SHAPES_PER_SYMBOL:
        return Response({"error": "shapes must be a list (max %d)" % _MAX_SHAPES_PER_SYMBOL}, status=400)

    cleaned = [s for s in (_shape_from_payload(r) for r in raw_shapes
                           if isinstance(r, dict)) if s is not None]
    ChartDrawing.objects.filter(symbol=symbol).delete()
    ChartDrawing.objects.bulk_create(
        [ChartDrawing(symbol=symbol, **s) for s in cleaned]
    )
    return Response({"symbol": symbol, "saved": len(cleaned)}, status=200)


# ---------------------------------------------------------------------------
# Phase 5: Enhanced endpoints for the new candle engine + signal engine
# ---------------------------------------------------------------------------

# Timeframe label → seconds (for the new candles table which stores tf as uint32)
_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
}


@api_view(["GET"])
@permission_classes([AllowAny])
def market_candles_v2(request):
    """
    GET /api/market/candles-v2/?symbol=EURUSD&timeframe=15m&limit=200

    Only 1-minute candles are stored in ClickHouse.  Higher timeframes
    are aggregated on-the-fly from 1m candles.
    """
    symbol = normalize_symbol(request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    timeframe = request.query_params.get("timeframe", "15m")
    if timeframe not in _TIMEFRAME_SECONDS:
        return Response({"error": "unsupported timeframe"}, status=400)

    try:
        limit = int(request.query_params.get("limit", CANDLES_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = CANDLES_LIMIT_DEFAULT
    limit = max(CANDLES_LIMIT_MIN, min(limit, CANDLES_LIMIT_MAX))

    client = _safe_client()
    raw = get_candles_from_ch(client, symbol, _TIMEFRAME_SECONDS[timeframe], limit) if client else []
    candles = [
        {"time": int(c["open_time"].timestamp()), "open": c["open"],
         "high": c["high"], "low": c["low"], "close": c["close"]}
        for c in raw
    ]

    return Response(
        {"symbol": symbol, "timeframe": timeframe, "candles": candles},
        status=200,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def signals_live_v2(request):
    """
    GET /api/signals/live-v2/?symbol=EURUSD&limit=20

    Reads confirmed signals from the ``trading_signals_v2`` table.
    No legacy fallback.
    """
    symbol = request.query_params.get("symbol", "")
    symbol = normalize_symbol(symbol) if symbol else None

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    client = _safe_client()
    signals = []
    if client:
        from forex.clickhouse.schema import get_signals_v2
        signals = get_signals_v2(client, symbol, limit)

    return Response({"signals": signals}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def signals_snr(request):
    """
    GET /api/v1/signals/snr/?symbol=EURUSD&limit=20&live=true

    Returns Malaysian SNR strategy signals (snr, breakout, marriage,
    engulfing, flip, qml, confluence) in v1 envelope format.

    Query params:
        symbol: optional filter by symbol
        limit: max signals to return (1..100, default 20)
        live: if "true", return only active in-memory signals from
              SignalLifecycleManager (states: CONFIRMED, WAITING, TRIGGERED),
              ordered newest first, deduped by setup_key. If "false" or omitted,
              fall back to ClickHouse history.
    """
    symbol = request.query_params.get("symbol", "")
    symbol = normalize_symbol(symbol) if symbol else None

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    live = request.query_params.get("live", "").lower() == "true"

    if live:
        # Fetch from in-memory SignalLifecycleManager
        lifecycle = get_global_lifecycle()
        if lifecycle:
            # Get active signals, filter by SNR analyzers and live states
            active_signals = lifecycle.get_active(symbol)
            live_states = {SignalState.CONFIRMED, SignalState.WAITING, SignalState.TRIGGERED}
            snr_signals = []
            seen_keys = set()
            for sig in active_signals:
                if sig.state not in live_states:
                    continue
                # Filter for SNR analyzers
                evidence = sig.confidence_evidence or {}
                analyzer = (evidence.get("analyzer") or "").lower()
                source_strategies = sig.source_strategies or []
                is_snr = analyzer in SNR_ANALYZERS or any(s in SNR_ANALYZERS for s in source_strategies)
                if not is_snr:
                    continue
                # Dedup by setup_key (newest first wins)
                setup_key = SignalLifecycleManager.setup_key(sig.symbol, sig.direction, sig.strategy)
                if setup_key in seen_keys:
                    continue
                seen_keys.add(setup_key)
                snr_signals.append(sig.to_dict_v1())
                if len(snr_signals) >= limit:
                    break
            return Response({"signals": snr_signals}, status=200)

    # Fallback to ClickHouse history
    client = _safe_client()
    signals = _fetch_snr_signals(client, symbol, limit) if client else []

    return Response({"signals": signals}, status=200)
