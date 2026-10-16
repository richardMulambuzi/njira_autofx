"""
Bounded, thread-safe persistence buffer for tick data.

Ticks arrive faster than ClickHouse can always absorb them. This buffer
decouples the producer (the tick stream) from the consumer (the BatchWriter
daemon thread). It is backed by a ``collections.deque(maxlen=N)``: when full,
the oldest ticks are evicted automatically and a counter is incremented so we
observe backpressure instead of letting memory grow without bound.

All operations are guarded by a lock so the producer thread and the writer
thread can safely share the buffer.
"""
from __future__ import annotations

import threading
import time
from collections import deque

DEFAULT_MAXLEN = 50_000


class PersistenceBuffer:
    """Bounded, thread-safe tick buffer with overflow-drop semantics."""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._maxlen = max(0, int(maxlen))
        self._buffer: deque = deque(maxlen=self._maxlen)
        self._lock = threading.Lock()
        self._dropped_ticks = 0
        self._flush_count = 0
        self._last_flush_time = 0.0
        self._persistence_failures = 0

    def put(
        self,
        symbol: str,
        bid: float,
        ask: float,
        spread: float,
        digits: int,
        timestamp_ms: int,
    ) -> None:
        """Append a tick. Non-blocking; drops the oldest when at capacity."""
        with self._lock:
            # deque(maxlen) evicts exactly one oldest item on a full append.
            if len(self._buffer) >= self._maxlen:
                self._dropped_ticks += 1
            self._buffer.append(
                (symbol, bid, ask, spread, digits, timestamp_ms)
            )

    def drain_batch(self, max_size: int) -> list:
        """Remove and return up to ``max_size`` ticks (oldest first)."""
        batch: list = []
        with self._lock:
            while self._buffer and len(batch) < max_size:
                batch.append(self._buffer.popleft())
        return batch

    def flush_on_shutdown(self) -> list:
        """Return (and clear) every remaining tick for a final best-effort flush."""
        with self._lock:
            remaining = list(self._buffer)
            self._buffer.clear()
        return remaining

    def record_flush(self, count: int, success: bool) -> None:
        """Update flush metrics after a writer flush attempt."""
        with self._lock:
            self._flush_count += 1
            self._last_flush_time = time.time()
            if not success:
                self._persistence_failures += 1

    def get_metrics(self) -> dict:
        with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "maxlen": self._maxlen,
                "dropped_ticks": self._dropped_ticks,
                "flush_count": self._flush_count,
                "last_flush_time": self._last_flush_time,
                "persistence_failures": self._persistence_failures,
            }
