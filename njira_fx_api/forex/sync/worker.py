"""
BackfillWorker — T6 recovery orchestration + backfill driving (async-engineer).

Owns (spec §5.5 / §5.6 / §6.4 / §8):

  * the **token-bucket rate limiter** — max ``SYNC_BACKFILL_RATE`` requests
    per second (default 5) so the provider's rate limits are respected (spec §8:
    "5–10 req/s, rate-limit aware");
  * the **gap timer** — periodic check (``SYNC_GAP_CHECK_INTERVAL``, default
    5 s) comparing each LIVE symbol's last accepted ts against ``now``; a
    stall past the threshold drives ``LIVE → GAP`` + ``gap_detected``
    (spec §5.5 — the engine detects gaps on tick arrival; the worker owns
    the timer loop that catches stalls where NO tick arrives);
  * **backfill orchestration** — cold start (``SYNCING_HISTORY``), gap
    recovery (``GAP → BACKFILLING`` from ``last_accepted_ts → now``) and
    provider-switch re-backfill (``RESET → SYNCING_HISTORY``), driven via the
    provider's ``backfill_ticks`` generator when present (e.g. Pepperstone), else
    ``get_ticks_range`` paging, else the ``get_ticks(limit)`` fallback
    (spec §5.6 / contracts §2.4);
  * **progress reporting** — ``backfill_progress`` events per batch carrying
    the contract fields (``pages_done``, ``pages_total``, ``oldest_ts``,
    contracts §6.5) plus the T6 diagnostics ``batches_done``,
    ``ticks_ingested``, ``window_covered_seconds``, ``pct``;
  * **cooperative cancellation** — a stop flag checked between batches;
    graceful shutdown via ``stop()``.

Single-writer rule (spec §6.1, binding): the worker NEVER touches
``SegmentStore``. Every page funnels through ``engine.ingest_batch()``; the
engine writer thread is the sole committer. State transitions go through the
engine's recovery hooks (``begin_backfill`` / ``complete_backfill`` /
``fail_backfill`` / ``report_gap``) so the state machine stays single-owner.

Threading model: two daemon threads owned by the worker —

  * ``_control_thread`` — gap timer + recovery scheduling + provider-switch
    re-orchestration;
  * ``_backfill_thread`` — executes backfill jobs sequentially (one symbol at
    a time; the engine's bounded queue absorbs the ingest bursts).

The worker is attached to the engine via ``engine.attach_worker(worker)``;
``engine.start()`` then starts it and ``engine.stop()`` stops it.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from forex.broker.errors import BrokerError
from forex.broker.models import MarketTick

from forex.sync.config import SyncConfig
from forex.sync.errors import SyncError, SyncErrorCode
from forex.sync.events import backfill_progress
from forex.sync.normalizer import ms_to_datetime, to_epoch_ms

logger = logging.getLogger("forex.sync.worker")

#: Job kinds (drives the paging strategy + state transitions).
_COLD_START = "cold_start"
_GAP_RECOVERY = "gap_recovery"
_PROVIDER_SWITCH = "provider_switch"

#: Default exponential-backoff parameters (capped; spec §5.5 "retry w/
#: backoff"). Overridable via the constructor for tests.
_BACKOFF_BASE = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP = 60.0
_MAX_RETRIES = 5


class TokenBucket:
    """A small token-bucket rate limiter (spec §6.4 / §8).

    ``rate`` tokens are added per second up to ``capacity`` (default: one
    second's worth — a burst of ``rate`` requests). ``acquire()`` blocks
    until ``tokens`` are available; the wait is interruptible in small
    increments so a worker stop is never delayed by more than ~50 ms.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self._rate = max(0.1, float(rate))
        self._capacity = float(capacity) if capacity is not None else self._rate
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available (no timeout — the caller
        checks its stop flag between batches)."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self._rate
            time.sleep(min(wait, 0.05))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now


@dataclass(frozen=True)
class _BackfillJob:
    """One backfill unit of work for the worker's job queue."""

    symbol: str
    kind: str  # _COLD_START | _GAP_RECOVERY | _PROVIDER_SWITCH
    days: float | None = None
    resume_from_ms: int | None = None


class BackfillWorker:
    """Recovery + backfill orchestration for one :class:`SyncEngine`.

    The worker is provider-neutral: it consumes only the ``TradingProvider``
    surface (``backfill_ticks`` when present, ``supports_ticks_range`` +
    ``get_ticks_range``, ``get_ticks`` fallback) and the engine's recovery
    hooks. It never imports provider classes (spec §2.2).
    """

    def __init__(
        self,
        engine: object,
        config: SyncConfig | None = None,
        *,
        days: float | None = None,
        max_rps: float | None = None,
        backoff_base: float = _BACKOFF_BASE,
        backoff_factor: float = _BACKOFF_FACTOR,
        backoff_cap: float = _BACKOFF_CAP,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._engine = engine
        self._config = config or getattr(engine, "config", None) or SyncConfig.from_env()
        #: Cold-start window in days (management command ``--days``); None →
        #: ``SYNC_WINDOW_MAX_DAYS`` (60).
        self._days = days
        #: Token-bucket rate limiter (spec §6.4): SYNC_BACKFILL_RATE default 5
        #: req/s. ``max_rps`` overrides for tests.
        self._rate_limiter = TokenBucket(max_rps or self._config.backfill_rate)
        self._backoff_base = float(backoff_base)
        self._backoff_factor = float(backoff_factor)
        self._backoff_cap = float(backoff_cap)
        self._max_retries = int(max_retries)

        self._stop_event = threading.Event()
        self._jobs: queue.Queue[_BackfillJob] = queue.Queue()
        #: Symbols with an in-flight backfill job (prevents duplicate jobs).
        self._active_jobs: set[str] = set()
        self._active_lock = threading.Lock()
        #: Pending provider-switch notification (set by the engine's writer
        #: thread; consumed by the control thread — never blocks the writer).
        self._pending_switch: tuple[str, str] | None = None
        self._switch_lock = threading.Lock()
        #: Re-subscribe the live stream once the switch re-backfill drains.
        self._resume_after_switch = False
        #: Per-symbol progress bookkeeping (batches, ticks, window coverage).
        self._progress: dict[str, dict] = {}
        self._control_thread: threading.Thread | None = None
        self._backfill_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Spawn the control + backfill threads and enqueue the cold-start
        jobs for every tracked symbol (spec §1.3a: cold start from
        historical backfill). Idempotent."""
        if self._control_thread is not None and self._control_thread.is_alive():
            return
        self._stop_event.clear()
        self._control_thread = threading.Thread(
            target=self._control_loop,
            name="sync-worker-control",
            daemon=True,
        )
        self._backfill_thread = threading.Thread(
            target=self._backfill_loop,
            name="sync-worker-backfill",
            daemon=True,
        )
        self._control_thread.start()
        self._backfill_thread.start()
        for symbol in self._engine.state:
            self._enqueue(_BackfillJob(symbol, _COLD_START, days=self._days))
        logger.info(
            "backfill worker started: symbols=%s rate=%s req/s",
            list(self._engine.state),
            self._config.backfill_rate,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Cooperative shutdown: set the stop flag, join both threads."""
        self._stop_event.set()
        for thread in (self._control_thread, self._backfill_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)

    def on_provider_switched(self, old_provider: str, new_provider: str) -> None:
        """Called by the engine (writer thread) on a factory switch (spec
        §6.5). Non-blocking: records the switch; the control thread
        re-orchestrates (RESET → SYNCING_HISTORY re-backfill + live resume)."""
        with self._switch_lock:
            self._pending_switch = (old_provider, new_provider)
        logger.info(
            "backfill worker: provider switch %s → %s queued",
            old_provider,
            new_provider,
        )

    # ------------------------------------------------------------------
    # Introspection (tests / management command)
    # ------------------------------------------------------------------
    @property
    def is_busy(self) -> bool:
        """True while a backfill job is in flight or queued."""
        with self._active_lock:
            active = bool(self._active_jobs)
        return active or not self._jobs.empty()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no backfill job is queued or in flight."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_busy:
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    # Control loop — gap timer + recovery scheduling (spec §5.5)
    # ------------------------------------------------------------------
    def _control_loop(self) -> None:
        interval = max(0.1, float(self._config.gap_check_interval))
        while not self._stop_event.is_set():
            self._handle_pending_switch()
            self._check_gaps()
            self._schedule_recoveries()
            self._stop_event.wait(interval)

    def _handle_pending_switch(self) -> None:
        with self._switch_lock:
            pending = self._pending_switch
            self._pending_switch = None
        if pending is None:
            return
        old_provider, new_provider = pending
        # Per-symbol RESET → SYNCING_HISTORY re-backfill (the engine already
        # transitioned the states and retained/cleared the window per
        # SYNC_KEEP_WINDOW_ON_SWITCH — spec §5.5).
        for symbol in self._engine.state:
            self._enqueue(
                _BackfillJob(
                    symbol,
                    _PROVIDER_SWITCH,
                    days=self._days or self._config.window_max_days,
                )
            )
        self._resume_after_switch = True
        logger.info(
            "backfill worker: re-backfill queued for %s on %s",
            list(self._engine.state),
            new_provider,
        )

    def _check_gaps(self) -> None:
        """Gap timer: LIVE symbols whose last accepted ts is older than the
        threshold enter GAP (spec §5.5). The engine's tick-arrival detection
        covers stalls followed by a tick; this loop covers total stalls."""
        now_ms = int(time.time() * 1000)
        for symbol, state in self._engine.state.items():
            if state.value != "live":
                continue
            last = self._engine.last_accepted_ms(symbol)
            if last is None:
                continue
            threshold = float(self._engine.gap_threshold(symbol))
            if now_ms - last > threshold * 1000.0:
                self._engine.report_gap(
                    symbol,
                    ms_to_datetime(last),
                    ms_to_datetime(now_ms),
                    threshold,
                )

    def _schedule_recoveries(self) -> None:
        """GAP symbols → recovery backfill from last_accepted_ts → now
        (spec §5.5: LIVE → GAP → BACKFILLING(from last_accepted_ts) → LIVE)."""
        for symbol, state in self._engine.state.items():
            if state.value != "gap":
                continue
            with self._active_lock:
                if symbol in self._active_jobs:
                    continue
            self._enqueue(
                _BackfillJob(
                    symbol,
                    _GAP_RECOVERY,
                    resume_from_ms=self._engine.last_accepted_ms(symbol),
                )
            )

    def _enqueue(self, job: _BackfillJob) -> None:
        with self._active_lock:
            if job.symbol in self._active_jobs:
                return
            self._active_jobs.add(job.symbol)
        self._jobs.put(job)

    # ------------------------------------------------------------------
    # Backfill thread — one job at a time (single-writer preserved)
    # ------------------------------------------------------------------
    def _backfill_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                if self._resume_after_switch and not self.is_busy:
                    self._resume_after_switch = False
                    self._engine.resume_live_subscription(force=True)
                continue
            try:
                self._run_backfill(job)
            except Exception:  # noqa: BLE001 — never kill the worker thread
                logger.exception(
                    "backfill worker: job failed for %s (%s)",
                    job.symbol,
                    job.kind,
                )
            finally:
                with self._active_lock:
                    self._active_jobs.discard(job.symbol)
                self._jobs.task_done()

    def _run_backfill(self, job: _BackfillJob) -> None:
        """Drive one backfill job: state entry → paged fetch (rate-limited,
        retried with exponential backoff) → ingest → state completion."""
        symbol = job.symbol
        engine = self._engine
        provider = engine.active_provider()
        provider_name = provider.name
        engine.begin_backfill(symbol, reason=f"{job.kind} backfill")

        if job.kind == _GAP_RECOVERY:
            start_ms = job.resume_from_ms or engine.last_accepted_ms(symbol)
            end = datetime.now(timezone.utc)
            start = ms_to_datetime(start_ms) if start_ms is not None else (
                end - timedelta(days=job.days or 1)
            )
            window_seconds = max(0.0, (end - start).total_seconds())
            pages_total = max(1, -(-int(window_seconds) // self._config.backfill_batch_size))
        else:
            days = job.days or self._config.window_max_days
            window_seconds = days * 86_400
            pages_total = max(1, -(-int(window_seconds) // self._config.backfill_batch_size))
            start = end = None  # computed by the iterator

        self._progress[symbol] = {
            "pages_done": 0,
            "pages_total": pages_total,
            "ticks_ingested": 0,
            "oldest_ts_ms": None,
            "newest_ts_ms": None,
            "window_seconds": window_seconds,
            "provider": provider_name,
        }

        attempt = 0
        while True:
            try:
                if job.kind == _GAP_RECOVERY:
                    pages = self._iter_window(provider, symbol, start, end)
                else:
                    pages = self._iter_cold_start(
                        provider, symbol, job.days or self._config.window_max_days
                    )
                for batch in pages:
                    if self._stop_event.is_set():
                        logger.info(
                            "backfill worker: cancelled %s (%s) between batches",
                            symbol,
                            job.kind,
                        )
                        return
                    accepted = engine.ingest_batch(batch).accepted
                    self._report_progress(symbol, batch, accepted)
                engine.complete_backfill(
                    symbol, reason=f"{job.kind} backfill complete"
                )
                return
            except BrokerError as exc:
                attempt += 1
                if attempt > self._max_retries or self._stop_event.is_set():
                    engine.fail_backfill(
                        symbol,
                        reason=f"{job.kind} backfill failed after {attempt} "
                        f"attempts: {exc}",
                    )
                    logger.error(
                        "backfill worker: %s (%s) failed permanently: %s",
                        symbol,
                        job.kind,
                        exc,
                    )
                    return
                delay = min(
                    self._backoff_cap,
                    self._backoff_base * (self._backoff_factor ** (attempt - 1)),
                )
                logger.warning(
                    "backfill worker: %s (%s) fetch failed (%s) — retry %d/%d "
                    "in %.1fs",
                    symbol,
                    job.kind,
                    exc,
                    attempt,
                    self._max_retries,
                    delay,
                )
                self._stop_event.wait(delay)
                if self._stop_event.is_set():
                    return
                # Re-plan: resume from the oldest committed watermark so the
                # retry does not re-fetch the whole window (dedup absorbs the
                # overlap — spec §5.5).
                if job.kind == _GAP_RECOVERY:
                    resume = self._progress[symbol].get("oldest_ts_ms") or start_ms
                    start = ms_to_datetime(resume) if resume is not None else start
                else:
                    resume = self._progress[symbol].get("oldest_ts_ms")
                    if resume is not None:
                        job = _BackfillJob(
                            symbol,
                            job.kind,
                            days=max(
                                0.0001,
                                (datetime.now(timezone.utc) - ms_to_datetime(resume))
                                .total_seconds()
                                / 86_400,
                            ),
                        )
            except SyncError:
                raise
            except Exception as exc:  # noqa: BLE001 — normalize unexpected
                logger.exception("backfill worker: %s (%s) unexpected error", symbol, job.kind)
                engine.fail_backfill(symbol, reason=f"{job.kind} backfill failed: {exc}")
                return

    # ------------------------------------------------------------------
    # Page iterators (spec §5.6 / contracts §2.4)
    # ------------------------------------------------------------------
    def _iter_cold_start(
        self, provider: object, symbol: str, days: float
    ) -> Iterator[list[MarketTick]]:
        """Yield batches for a cold-start window ``[now - days, now]``.

        Prefers the provider's ``backfill_ticks`` generator (e.g. Pepperstone — walks
        backward from now in paged windows, reconnecting fresh OTP every N
        requests); falls back to ``get_ticks_range`` forward paging, then the
        ``get_ticks(limit)`` best-effort fallback (spec §5.6). Each page
        fetch is gated by the token bucket.
        """
        backfill_ticks = getattr(provider, "backfill_ticks", None)
        if callable(backfill_ticks):
            generator = backfill_ticks(
                symbol,
                days=days,
                batch_size=self._config.backfill_batch_size,
            )
            while True:
                self._rate_limiter.acquire()
                try:
                    batch = next(generator)
                except StopIteration:
                    return
                yield batch
            return
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        yield from self._iter_window(provider, symbol, start, end)

    def _iter_window(
        self, provider: object, symbol: str, start: datetime, end: datetime
    ) -> Iterator[list[MarketTick]]:
        """Yield chronological pages of ``[start, end]`` (contracts §2.1).

        Uses ``get_ticks_range`` when ``supports_ticks_range`` (caller-driven
        paging: advance ``start`` past the last committed tick); the
        ``NotImplementedError`` backstop falls back to ``get_ticks``
        (contracts §2.4). Each page fetch is gated by the token bucket.
        """
        get_range = getattr(provider, "get_ticks_range", None)
        supports_range = bool(getattr(provider, "supports_ticks_range", False))
        if supports_range and get_range is not None:
            cursor = start
            batch = self._config.backfill_batch_size
            while cursor <= end:
                self._rate_limiter.acquire()
                try:
                    page = get_range(symbol, cursor, end, limit=batch)
                except NotImplementedError:
                    # Defensive backstop (contracts §2.4): the capability flag
                    # lied — fall back to get_ticks below.
                    logger.warning(
                        "backfill worker: %s raised NotImplementedError for "
                        "get_ticks_range — falling back to get_ticks",
                        getattr(provider, "name", "provider"),
                    )
                    break
                if not page:
                    break  # window exhausted (contracts §2.1)
                yield page
                if len(page) < batch:
                    break  # short page → window exhausted
                last_ts = to_epoch_ms(page[-1].timestamp)
                cursor = ms_to_datetime(last_ts) + timedelta(microseconds=1)
            return
        # Best-effort fallback (spec §5.6): get_ticks(limit), newest-first →
        # reversed to chronological. Partial windows are legal — the engine
        # tolerates unfilled gaps.
        get_ticks = getattr(provider, "get_ticks", None)
        if get_ticks is None:
            raise SyncError(
                SyncErrorCode.INVALID_REQUEST,
                f"provider {getattr(provider, 'name', 'provider')} exposes no "
                "tick history primitive",
            )
        self._rate_limiter.acquire()
        page = list(get_ticks(symbol, limit=self._config.backfill_batch_size) or [])
        page.reverse()
        if page:
            yield page

    # ------------------------------------------------------------------
    # Progress reporting (contracts §6.5 + T6 diagnostics)
    # ------------------------------------------------------------------
    def _report_progress(self, symbol: str, batch: list[MarketTick], accepted: int) -> None:
        prog = self._progress.setdefault(
            symbol,
            {
                "pages_done": 0,
                "pages_total": 1,
                "ticks_ingested": 0,
                "oldest_ts_ms": None,
                "newest_ts_ms": None,
                "window_seconds": 0.0,
                "provider": "",
            },
        )
        prog["pages_done"] += 1
        prog["ticks_ingested"] += accepted
        if batch:
            first_ms = to_epoch_ms(batch[0].timestamp)
            last_ms = to_epoch_ms(batch[-1].timestamp)
            if first_ms is not None:
                prog["oldest_ts_ms"] = (
                    first_ms
                    if prog["oldest_ts_ms"] is None
                    else min(prog["oldest_ts_ms"], first_ms)
                )
            if last_ms is not None:
                prog["newest_ts_ms"] = (
                    last_ms
                    if prog["newest_ts_ms"] is None
                    else max(prog["newest_ts_ms"], last_ms)
                )

        oldest = prog["oldest_ts_ms"]
        newest = prog["newest_ts_ms"]
        window_seconds = float(prog.get("window_seconds") or 0.0)
        if oldest is not None and newest is not None:
            covered = max(0.0, (newest - oldest) / 1000.0)
        else:
            covered = 0.0
        pct = min(100.0, covered / window_seconds * 100.0) if window_seconds > 0 else 0.0

        event = backfill_progress(
            symbol,
            prog["pages_done"],
            prog["pages_total"],
            ms_to_datetime(oldest) if oldest is not None else None,
        )
        # T6 diagnostics (additive — the §6.5 contract fields are untouched).
        event.update(
            {
                "batches_done": prog["pages_done"],
                "ticks_ingested": prog["ticks_ingested"],
                "window_covered_seconds": round(covered, 3),
                "pct": round(pct, 2),
            }
        )
        self._engine.publish_event(event)


__all__ = ["BackfillWorker", "TokenBucket"]