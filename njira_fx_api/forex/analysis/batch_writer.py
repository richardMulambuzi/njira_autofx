"""
Long-running ClickHouse batch writer for tick data.

Consumes ticks from a :class:`PersistenceBuffer` in a dedicated daemon thread
and flushes them to ClickHouse (with an optional HTTP fallback sink). A flush
is triggered by ``batch_size`` OR ``max_flush_interval`` — whichever comes
first. ClickHouse failures are retried with exponential backoff (max 3
attempts). The producer (the tick stream) is never blocked: the writer only
touches the buffer via the non-blocking ``drain_batch``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import requests

from forex.analysis.persistence_buffer import PersistenceBuffer

logger = logging.getLogger("forex.analysis.batch_writer")

DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL = 1.0
MAX_RETRIES = 3
BASE_BACKOFF = 0.5


class BatchWriter:
    """Daemon-thread ClickHouse batch writer backed by a PersistenceBuffer."""

    def __init__(
        self,
        buffer: PersistenceBuffer,
        client,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        ingest_url: Optional[str] = None,
        local_sink: Optional[Callable[[list], None]] = None,
    ) -> None:
        self._buffer = buffer
        self._client = client
        self._batch_size = max(1, int(batch_size))
        self._flush_interval = max(0.05, float(flush_interval))
        self._ingest_url = ingest_url
        self._local_sink = local_sink
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._total_flushed = 0
        self._total_failures = 0
        self._flush_durations: list[float] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="ticks-batch-writer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the writer to stop and wait for it to drain + exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def _run(self) -> None:
        pending: list = []
        last_flush = time.monotonic()
        while not self._stop_event.is_set():
            # Top up the pending batch from the buffer (non-blocking).
            if len(pending) < self._batch_size:
                needed = self._batch_size - len(pending)
                pending.extend(self._buffer.drain_batch(needed))
            now = time.monotonic()
            if pending and (
                len(pending) >= self._batch_size
                or (now - last_flush) >= self._flush_interval
            ):
                self.flush_now(pending)
                pending = []
                last_flush = now
            else:
                # No flush yet — sleep briefly, waking promptly on stop.
                self._stop_event.wait(timeout=min(0.05, self._flush_interval))
        # Graceful final flush: drain whatever is left and write it.
        pending.extend(self._buffer.drain_batch(self._batch_size * 50))
        if pending:
            self.flush_now(pending)

    # ------------------------------------------------------------------
    # Flush (public so the engine can use it for the shutdown drain too)
    # ------------------------------------------------------------------
    def flush_now(self, rows: list) -> None:
        if not rows:
            return
        start = time.monotonic()
        attempted = self._client is not None
        ch_ok = self._flush_clickhouse(rows) if attempted else False
        if self._ingest_url:
            self._flush_ingest(rows)
        if self._local_sink:
            self._local_sink(rows)
        duration = time.monotonic() - start
        with self._lock:
            self._flush_durations.append(duration)
            if len(self._flush_durations) > 100:
                self._flush_durations.pop(0)
            if attempted and ch_ok:
                self._total_flushed += len(rows)
            if attempted and not ch_ok:
                self._total_failures += 1
        if attempted:
            self._buffer.record_flush(len(rows), ch_ok)

    def _flush_clickhouse(self, rows: list) -> bool:
        """Insert with exponential backoff. Returns True on success."""
        delay = BASE_BACKOFF
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                from forex.clickhouse.schema import insert_ticks_batch

                insert_ticks_batch(self._client, rows)
                return True
            except Exception as exc:  # noqa: BLE001 — best-effort, never crash
                logger.warning(
                    "ClickHouse tick insert failed (attempt %d/%d): %s",
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(delay)
                    delay *= 2
        logger.error(
            "ClickHouse tick insert gave up after %d attempts; %d tick(s) "
            "not persisted",
            MAX_RETRIES, len(rows),
        )
        return False

    def _flush_ingest(self, rows: list) -> None:
        """POST ticks to the optional HTTP fallback sink (best-effort)."""
        try:
            payload = [
                {
                    "symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "digits": digits,
                    "timestamp": timestamp_ms,
                }
                for (symbol, bid, ask, spread, digits, timestamp_ms) in rows
            ]
            response = requests.post(self._ingest_url, json=payload, timeout=2)
            if response.status_code >= 400:
                logger.warning(
                    "Ticks ingest endpoint returned HTTP %s",
                    response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 — HTTP sink is optional
            logger.warning("Ticks ingest POST failed: %s", exc)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    def get_metrics(self) -> dict:
        with self._lock:
            avg = (
                sum(self._flush_durations) / len(self._flush_durations)
                if self._flush_durations
                else 0.0
            )
            return {
                "total_flushed": self._total_flushed,
                "total_failures": self._total_failures,
                "flush_duration_avg": avg,
                "running": self._thread is not None and self._thread.is_alive(),
            }
