"""Centralized, thread-safe metrics collector for the njira_auto_fx forex system.

Zero-dependency: stdlib only. Aggregates counters and latency histograms from
all running engines and exposes a snapshot dict for the health endpoint.
"""

import threading
from collections import deque

_LATENCY_WINDOW = 1000

_LATENCY_METRICS = {
    "processing_latency",
    "persistence_latency",
    "candle_latency",
    "signal_latency",
    "clickhouse_insert_duration",
}

_COUNTER_METRICS = {
    "ticks_received",
    "ticks_processed",
    "ticks_dropped",
    "ticks_duplicate",
    "ticks_out_of_order",
    "ticks_invalid",
    "buffer_size",
    "buffer_utilization",
    "clickhouse_insert_failures",
    "reconnect_count",
    "stream_disconnects",
    "signals_generated",
    "signals_confirmed",
    "signals_invalidated",
    "signals_expired",
    "signals_waiting",
}


class EngineMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {name: 0 for name in _COUNTER_METRICS}
        self._latency = {name: deque(maxlen=_LATENCY_WINDOW) for name in _LATENCY_METRICS}

    def increment(self, name, amount=1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def record_latency(self, name, duration_ms):
        with self._lock:
            self._latency.setdefault(name, deque(maxlen=_LATENCY_WINDOW)).append(duration_ms)

    @staticmethod
    def _stats(samples):
        if not samples:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
        s = sorted(samples)
        n = len(s)

        def pct(p):
            return s[min(n - 1, int(p * n / 100))]

        return {
            "count": n,
            "p50": pct(50),
            "p95": pct(95),
            "p99": pct(99),
            "max": s[-1],
            "avg": sum(s) / n,
        }

    def get_latency_stats(self, name):
        with self._lock:
            return self._stats(list(self._latency.get(name, ())))

    def snapshot(self):
        with self._lock:
            return {
                "counters": dict(self._counters),
                "latency": {name: self._stats(samples) for name, samples in self._latency.items()},
            }


METRICS = EngineMetrics()
