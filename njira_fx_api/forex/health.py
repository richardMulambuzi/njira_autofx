"""Open health-check endpoint for the njira_auto_fx forex system.

Pings ClickHouse on request and reports the global METRICS snapshot. No auth
(JWT) required so orchestrators / load balancers can probe it freely. The
TicksEngine is intentionally NOT referenced here — it runs in a management
command thread and is not globally accessible; this endpoint works without it.
"""
import time

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from forex.analysis.metrics import METRICS
from forex.clickhouse.connection import get_clickhouse_client

_START = time.monotonic()


class HealthCheckView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        clickhouse = self._clickhouse_status()
        snapshot = METRICS.snapshot()
        counters = snapshot.get("counters", {})

        # Global METRICS tracks persistence failures as clickhouse_insert_failures.
        persistence_failures = counters.get("clickhouse_insert_failures", 0)
        buffer_utilization = counters.get("buffer_utilization", 0)

        if clickhouse["enabled"] and not clickhouse["connected"]:
            status = "unhealthy"
        elif persistence_failures > 0 or buffer_utilization > 80:
            status = "degraded"
        else:
            status = "healthy"

        return Response(
            {
                "status": status,
                "timestamp": _utc_now(),
                "uptime_seconds": round(time.monotonic() - _START, 3),
                "clickhouse": clickhouse,
                "metrics": snapshot,
            }
        )

    @staticmethod
    def _clickhouse_status():
        client = get_clickhouse_client()
        if client is None:
            return {"enabled": False, "connected": False, "latency_ms": 0.0}
        try:
            start = time.monotonic()
            client.query("SELECT 1")
            return {
                "enabled": True,
                "connected": True,
                "latency_ms": round((time.monotonic() - start) * 1000, 3),
            }
        except Exception:
            return {"enabled": True, "connected": False, "latency_ms": 0.0}


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
