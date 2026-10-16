"""Context manager for recording elapsed time to a latency metric."""

from contextlib import contextmanager

import time

from .metrics import EngineMetrics


@contextmanager
def measure_latency(metrics: EngineMetrics, name: str):
    """Context manager that records elapsed time to a latency metric."""
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        metrics.record_latency(name, elapsed_ms)
