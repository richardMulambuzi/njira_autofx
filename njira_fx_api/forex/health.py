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
        connected, latency_ms = self._ping_clickhouse()
        snapshot = METRICS.snapshot()
        counters = snapshot.get("counters", {})

        # Global METRICS tracks persistence failures as clickhouse_insert_failures.
        persistence_failures = counters.get("clickhouse_insert_failures", 0)
        buffer_utilization = counters.get("buffer_utilization", 0)

        if not connected:
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
                "clickhouse": {"connected": connected, "latency_ms": latency_ms},
                "metrics": snapshot,
            }
        )

    @staticmethod
    def _ping_clickhouse():
        try:
            client = get_clickhouse_client()
            start = time.monotonic()
            client.query("SELECT 1")
            return True, round((time.monotonic() - start) * 1000, 3)
        except Exception:
            return False, 0.0


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
