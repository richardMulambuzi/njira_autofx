"""
Volume Profile API endpoints for the njira_auto_fx forex dashboard.

Five read-only GET endpoints, all best-effort (never raise; return the
contract's empty shape on failure so the UI renders an empty state). They read
live profiles from the engine registered via ``set_global_engine`` (None when
the engine is not running) and historical/event data from ClickHouse.

    GET /api/volume-profile/current/?symbol=EURUSD&methodology=tick_count
    GET /api/volume-profile/confluence/?symbol=EURUSD&price=1.08520
    GET /api/volume-profile/diagnostics/?symbol=EURUSD
    GET /api/volume-profile/events/?symbol=EURUSD&limit=50
    GET /api/volume-profile/historical/?symbol=EURUSD&start=...&end=...&profile_type=daily

Contract notes (mirrors forex.market_api.views):
    * Responses are plain JSON (no DRF envelope) so the dashboard consumes them
      directly.
    * Reads degrade gracefully: engine not running -> 200 with empty shape;
      ClickHouse unreachable -> 200 with empty list.
    * Symbol normalization strips the legacy ``frx``/``FRX`` prefix and
      upper-cases (see ``forex.common.normalize_symbol``).
    * Methodology is validated against ``ProfileMethodology`` values, default
      ``tick_count``.
"""
from __future__ import annotations

import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from forex.analysis.volume_profile import (
    get_global_engine,
    ProfileMethodology,
)
from forex.analysis.volume_profile_clickhouse import (
    VOLUME_PROFILE_EVENTS_TABLE,
    get_profile_history,
)
from forex.clickhouse.connection import get_clickhouse_client
from forex.common import is_valid_symbol, normalize_symbol

logger = logging.getLogger("forex.market_api.volume_profile")


# ---------------------------------------------------------------------------
# Helpers (best-effort; never raise to the caller)
# ---------------------------------------------------------------------------
def _safe_client():
    """Return a ClickHouse client or ``None`` when one cannot be obtained."""
    try:
        return get_clickhouse_client()
    except Exception as exc:  # noqa: BLE001 — best-effort, degrade gracefully
        logger.warning("ClickHouse client unavailable: %s", exc)
        return None


def _resolve_methodology(raw: str | None) -> ProfileMethodology:
    """Validate a methodology string against the enum; default ``tick_count``."""
    raw = (raw or "tick_count").strip().lower()
    try:
        return ProfileMethodology(raw)
    except ValueError:
        return ProfileMethodology.TICK_COUNT


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
def volume_profile_current(request):
    """
    GET /api/volume-profile/current/?symbol=EURUSD&methodology=tick_count

    Live developing profile for a symbol/methodology. Engine not running ->
    200 ``{"detail": ..., "profile": None}`` (UI-friendly, not 404). No profile
    -> ``{"symbol": ..., "profile": None}``.
    """
    symbol = normalize_symbol(request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    methodology = _resolve_methodology(request.query_params.get("methodology"))
    engine = get_global_engine()
    if engine is None:
        return Response(
            {"detail": "Volume profile engine not running", "profile": None},
            status=200,
        )

    profile = engine.get_profile(symbol, methodology)
    if profile is None:
        return Response({"symbol": symbol, "profile": None}, status=200)

    return Response({"symbol": symbol, "profile": profile.to_dict()}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def volume_profile_confluence(request):
    """
    GET /api/volume-profile/confluence/?symbol=EURUSD&price=1.08520

    Multi-timeframe confluence zones where price meets POC/VAH/VAL. ``price``
    optional — defaults to the profile POC. Engine not running -> empty list.
    """
    symbol = normalize_symbol(request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    engine = get_global_engine()
    if engine is None:
        return Response({"confluence_zones": []}, status=200)

    price: float | None = None
    price_raw = request.query_params.get("price")
    if price_raw:
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = None
    if price is None:
        prof = engine.get_profile(symbol)
        price = prof.poc if prof else None
    if price is None:
        return Response(
            {"symbol": symbol, "price": None, "confluence_zones": []}, status=200
        )

    zones = engine.get_confluence_zones(symbol, price)
    return Response(
        {"symbol": symbol, "price": price, "confluence_zones": zones}, status=200
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def volume_profile_diagnostics(request):
    """
    GET /api/volume-profile/diagnostics/?symbol=EURUSD

    Engine health: active profiles, total bins, metrics. Engine not running ->
    200 with zeros and empty metrics.
    """
    symbol = normalize_symbol(request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    engine = get_global_engine()
    if engine is None:
        return Response(
            {
                "symbol": symbol,
                "profiles_active": 0,
                "total_bins": 0,
                "memory_bytes": 0,
                "metrics": {},
            },
            status=200,
        )

    profiles = engine.get_all_profiles(symbol)
    total_bins = sum(len(p.bins) for p in profiles.values())
    return Response(
        {
            "symbol": symbol,
            "profiles_active": len(profiles),
            "total_bins": total_bins,
            "memory_bytes": 0,  # ponytail: no cheap accurate measure
            "metrics": engine.get_metrics(),
        },
        status=200,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def volume_profile_events(request):
    """
    GET /api/volume-profile/events/?symbol=EURUSD&limit=50

    Most recent discrete profile events (POC/VAH/VAL breaches, HVN/LVN
    touches) from ClickHouse. Unreachable CH / no rows -> ``{"events": []}``.
    """
    symbol = normalize_symbol(request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    try:
        limit = int(request.query_params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 1000))

    client = _safe_client()
    if client is None:
        return Response({"symbol": symbol, "events": []}, status=200)

    try:
        sql = (
            f"SELECT event_id, symbol, event_type, profile_type, price, "
            f"old_value, new_value, evidence, confidence, timestamp "
            f"FROM {VOLUME_PROFILE_EVENTS_TABLE} "
            f"WHERE symbol = {{sym:String}} "
            f"ORDER BY timestamp DESC LIMIT {{lim:UInt32}}"
        )
        result = client.query(sql, parameters={"sym": symbol, "lim": limit})
        events = [dict(zip(result.column_names, row)) for row in result.result_set]
    except Exception as exc:  # noqa: BLE001 — best-effort read, never crash
        logger.warning("Could not read volume profile events for %s: %s", symbol, exc)
        events = []

    return Response({"symbol": symbol, "events": events}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def volume_profile_historical(request):
    """
    GET /api/volume-profile/historical/?symbol=EURUSD&start=...&end=...&profile_type=daily

    Historical computed profiles from ClickHouse within [start, end]. Missing
    params / unreachable CH -> ``{"profiles": []}``.
    """
    symbol = normalize_symbol(request.query_params.get("symbol", ""))
    if not is_valid_symbol(symbol):
        return Response({"error": "invalid symbol"}, status=400)

    start = request.query_params.get("start")
    end = request.query_params.get("end")
    profile_type = request.query_params.get("profile_type", "daily")
    if not start or not end:
        return Response({"symbol": symbol, "profiles": []}, status=200)

    client = _safe_client()
    if client is None:
        return Response({"symbol": symbol, "profiles": []}, status=200)

    profiles = get_profile_history(client, symbol, start, end, profile_type)
    return Response({"symbol": symbol, "profiles": profiles}, status=200)
