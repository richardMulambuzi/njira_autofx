"""
SyncEngine — validate → dedup → gap detect → order → reconcile → commit →
publish (spec §1.1 / contracts §5).

One engine instance per process, one writer thread (spec §6.1). All
ingestion funnels through a bounded queue; provider callback threads only
``put()`` (spec §6.1). The writer thread is the sole committer — the
single-writer invariant holds (spec §6.2).

Provider-neutral by construction (spec §2.2): providers are resolved only
through ``TradingProviderFactory`` (or an injected provider for tests).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Sequence

from forex.broker.errors import BrokerError
from forex.broker.factory import TradingProviderFactory
from forex.broker.models import MarketTick

from forex.sync.config import SyncConfig
from forex.sync.errors import SyncError, SyncErrorCode
from forex.sync.events import (
    EventPublisher,
    backfill_progress,
    gap_detected,
    provider_switched,
    recovery_state_changed,
    tick_committed,
    window_advanced,
)
from forex.sync.normalizer import TickNormalizer, ms_to_datetime, to_epoch_ms
from forex.sync.storage import SegmentStore

logger = logging.getLogger("forex.sync.engine")

#: Sentinel that stops the writer thread (drains remaining items first).
_STOP = object()

#: Heartbeat interval for provider-switch detection (spec §6.5).
_HEARTBEAT_SECONDS = 5.0

#: Worst-state ordering for the engine-level state (contracts §5).
_STATE_RANK = {
    "init": 0,
    "syncing_history": 1,
    "live": 2,
    "gap": 3,
    "backfilling": 4,
    "reset": 5,
}


class SyncState(str, Enum):
    """Recovery state machine states (contracts §5.2)."""

    INIT = "init"
    SYNCING_HISTORY = "syncing_history"
    LIVE = "live"
    GAP = "gap"
    BACKFILLING = "backfilling"
    RESET = "reset"


#: Legal transitions (contracts §5.2 table — only these; any other
#: transition is rejected with SyncError INVALID_STATE).
_TRANSITIONS: dict[SyncState, set[SyncState]] = {
    SyncState.INIT: {SyncState.SYNCING_HISTORY, SyncState.RESET},
    SyncState.SYNCING_HISTORY: {SyncState.LIVE, SyncState.RESET},
    SyncState.LIVE: {SyncState.GAP, SyncState.RESET},
    SyncState.GAP: {SyncState.BACKFILLING, SyncState.RESET},
    SyncState.BACKFILLING: {SyncState.LIVE, SyncState.GAP, SyncState.RESET},
    SyncState.RESET: {SyncState.SYNCING_HISTORY, SyncState.RESET},
}


# ---------------------------------------------------------------------------
# DTOs (contracts §5.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IngestResult:
    """Result of an ingest call (contracts §5.1)."""

    accepted: int
    duplicates: int
    out_of_window: int
    invalid: int
    state_after: SyncState
    last_accepted_ts: datetime | None


@dataclass(frozen=True)
class BackfillResult:
    """Result of a backfill run (contracts §5.1)."""

    symbol: str
    pages_done: int
    pages_total: int
    ticks_committed: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    state_after: SyncState
    provider: str


# ---------------------------------------------------------------------------
# SyncEngine
# ---------------------------------------------------------------------------
class SyncEngine:
    """The pipeline engine (spec §5.3 / contracts §5).

    Threading model (spec §6):
      * One writer thread owns all ``SegmentStore`` mutations.
      * Provider callback threads / backfill workers only ``put()`` into the
        bounded queue (``SYNC_INGEST_QUEUE_SIZE``).
      * Readers (``query_range`` / ``snapshot`` / ``stats``) are safe under
        the stores' shared ``RLock``.
    """

    def __init__(
        self,
        symbols: Sequence[str],
        config: SyncConfig | None = None,
        provider: object | None = None,
        normalizer: TickNormalizer | None = None,
    ) -> None:
        self._config = config or SyncConfig.from_env()
        if len(symbols) > self._config.max_symbols:
            raise SyncError(
                SyncErrorCode.INVALID_REQUEST,
                f"{len(symbols)} symbols exceed SYNC_MAX_SYMBOLS "
                f"({self._config.max_symbols})",
            )
        self._symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not self._symbols:
            raise SyncError(SyncErrorCode.INVALID_REQUEST, "no symbols tracked")
        #: Injected provider (tests) or None → TradingProviderFactory.
        self._provider = provider
        self._normalizer = normalizer or TickNormalizer(self._config)
        self._publisher = EventPublisher()

        self._stores: dict[str, SegmentStore] = {
            symbol: SegmentStore(
                symbol=symbol,
                capacity=self._config.segment_capacity,
                record_layout=self._config.record_layout,
                dedup_key=self._config.dedup_key,
                dup_log_rate=self._config.dup_log_rate,
            )
            for symbol in self._symbols
        }
        self._states: dict[str, SyncState] = {
            symbol: SyncState.INIT for symbol in self._symbols
        }
        #: Last accepted tick ts (epoch ms) per symbol — the gap-detection
        #: watermark (spec §5.5).
        self._last_accepted: dict[str, int | None] = {
            symbol: None for symbol in self._symbols
        }
        #: Per-symbol cadence (seconds) used for the gap threshold
        #: ``max(3 × cadence, gap_threshold)`` (spec §5.5).
        self._cadence: dict[str, float] = {symbol: 1.0 for symbol in self._symbols}
        #: Per-symbol invalid-tick counters (dropped by the normalizer).
        self._invalid: dict[str, int] = {symbol: 0 for symbol in self._symbols}
        #: Per-symbol untracked-symbol drops (live ticks for unknown symbols).
        self._untracked: dict[str, int] = {symbol: 0 for symbol in self._symbols}
        #: SYNCING_HISTORY live-tick buffers (spec §5.5: live ticks arriving
        #: during cold start are buffered — bounded by SYNC_HISTORY_BUFFER —
        #: and reconciled after the backfill watermark passes; committing them
        #: immediately would push the backfill's older ticks out-of-window).
        self._history_buffers: dict[str, list[tuple[object, str, str, int]]] = {}
        self._buffer_drops: dict[str, int] = {symbol: 0 for symbol in self._symbols}

        self._queue: queue.Queue = queue.Queue(maxsize=self._config.ingest_queue_size)
        self._dropped = 0
        self._running = False
        self._stop_event = threading.Event()
        self._writer_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._subscription_id: str | None = None
        self._active_provider_name: str | None = None
        self._provider_lock = threading.Lock()
        self._touched: set[str] = set()
        #: Optional BackfillWorker (T6) attached via ``attach_worker``. The
        #: engine treats it as an opaque duck-typed object (``start``,
        #: ``stop``, ``on_provider_switched``) — no import, no cycle.
        self._worker: object | None = None

    # ------------------------------------------------------------------
    # Public API (contracts §5.3)
    # ------------------------------------------------------------------
    def ingest_tick(self, tick: MarketTick) -> IngestResult:
        """Entry point for one live tick (spec §5.3; also used by tests).

        Funnels through the bounded queue → normalized → validated →
        committed by the writer thread. Returns immediately (queue enqueue);
        the commit happens on the next batch drain (spec §6.2).
        """
        self._require_running()
        try:
            self._queue.put_nowait(("tick", tick, tick.provider, tick.symbol))
            accepted = 1
        except queue.Full:
            self._dropped += 1
            accepted = 0
        return self._ingest_result(tick.symbol, accepted)

    def ingest_batch(self, ticks: Sequence[MarketTick]) -> IngestResult:
        """Batched variant; the writer drains SYNC_COMMIT_BATCH_SIZE or
        SYNC_COMMIT_FLUSH_MS batches (spec §6.2)."""
        self._require_running()
        accepted = 0
        for tick in ticks:
            try:
                self._queue.put_nowait(("tick", tick, tick.provider, tick.symbol))
                accepted += 1
            except queue.Full:
                self._dropped += 1
                break
        symbol = ticks[0].symbol if ticks else self._symbols[0]
        return self._ingest_result(symbol, accepted)

    def ingest_raw(self, raw: object, provider: str, symbol: str) -> IngestResult:
        """Ingest a raw provider payload (normalized by the writer thread)."""
        self._require_running()
        try:
            self._queue.put_nowait(("tick", raw, provider, symbol))
            accepted = 1
        except queue.Full:
            self._dropped += 1
            accepted = 0
        return self._ingest_result(symbol, accepted)

    def backfill(self, symbol: str, days: int | None = None) -> BackfillResult:
        """Paged historical fill for ``symbol`` over the last ``days``
        (default: SYNC_WINDOW_MAX_DAYS = 60, spec §1.3 window age).

        Pages via ``TradingProvider.get_ticks_range`` when
        ``supports_ticks_range`` (contracts §2.4), else the
        ``get_ticks(limit)`` best-effort fallback. Backfill worker threads
        enqueue normalized pages; the writer thread remains the sole
        committer (spec §6.4). Emits ``backfill_progress`` events per page
        (spec §5.4). Blocking; returns BackfillResult.
        """
        symbol = symbol.upper()
        if symbol not in self._stores:
            raise SyncError(
                SyncErrorCode.INVALID_REQUEST, f"symbol {symbol!r} is not tracked"
            )
        self._require_running()
        state = self._states[symbol]
        if state in (SyncState.SYNCING_HISTORY, SyncState.BACKFILLING):
            raise SyncError(
                SyncErrorCode.BACKFILL_IN_PROGRESS,
                f"backfill already active for {symbol} (state={state.value})",
            )
        if state == SyncState.INIT:
            self._transition(symbol, SyncState.SYNCING_HISTORY, "cold start backfill")
        elif state == SyncState.GAP:
            self._transition(
                symbol,
                SyncState.BACKFILLING,
                "recovery backfill from last accepted ts",
            )
        elif state == SyncState.RESET:
            self._transition(
                symbol, SyncState.SYNCING_HISTORY, "re-sync after provider switch"
            )
        else:
            raise SyncError(
                SyncErrorCode.INVALID_STATE,
                f"backfill not allowed from state {state.value} for {symbol} "
                "(contracts §5.2: LIVE → SYNCING_HISTORY is illegal)",
            )

        try:
            result = self._run_backfill(symbol, days or self._config.window_max_days)
        except BrokerError:
            # Failure path (spec §5.5): BACKFILLING → GAP (retry w/ backoff);
            # SYNCING_HISTORY stays put (retry in place).
            if self._states[symbol] == SyncState.BACKFILLING:
                self._transition(symbol, SyncState.GAP, "backfill failed — retry with backoff")
            raise
        except SyncError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalize unexpected failures
            logger.exception("sync engine backfill failed for %s", symbol)
            if self._states[symbol] == SyncState.BACKFILLING:
                self._transition(symbol, SyncState.GAP, "backfill failed — retry with backoff")
            raise SyncError(
                SyncErrorCode.INVALID_STATE,
                f"backfill failed for {symbol}: {exc}",
            ) from exc

        if self._states[symbol] in (SyncState.BACKFILLING, SyncState.SYNCING_HISTORY):
            # Flush the SYNCING_HISTORY live-tick buffer on the writer thread
            # BEFORE going LIVE (spec §5.5) — same path as the worker's
            # complete_backfill.
            self.complete_backfill(symbol, "backfill complete, watermark passed")
        return result

    def subscribe(self, callback: Callable[[dict], None]) -> str:
        """Engine-wide event subscription (contracts §5 refined): the
        callback receives SyncEvent dicts (§6) which carry ``symbol`` where
        relevant. Returns a subscription id. Callback exceptions are
        isolated (publisher fan-out, spec §9.2)."""
        return self._publisher.subscribe(callback)

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription (no-op for unknown ids)."""
        self._publisher.unsubscribe(subscription_id)

    # ------------------------------------------------------------------
    # Recovery-controller hooks (T6 BackfillWorker — spec §5.5/§5.6)
    # ------------------------------------------------------------------
    @property
    def config(self) -> SyncConfig:
        """The engine's :class:`SyncConfig` (worker reads tuning from it)."""
        return self._config

    def attach_worker(self, worker: object) -> None:
        """Attach the T6 :class:`BackfillWorker` to this engine.

        When attached, the worker owns cold-start backfill, gap recovery and
        provider-switch re-backfill (spec §5.5); ``start()`` skips the
        synchronous backfill loop and ``_handle_provider_switch`` defers
        re-backfill to the worker. The worker is duck-typed (``start``,
        ``stop``, ``on_provider_switched``) so this module never imports it.
        """
        self._worker = worker

    def active_provider(self) -> object:
        """Resolve the active provider (injected or factory)."""
        return self._resolve_provider()

    def last_accepted_ms(self, symbol: str) -> int | None:
        """Last accepted tick timestamp (epoch ms) for ``symbol`` — the
        recovery watermark (spec §5.5). ``None`` when nothing committed yet."""
        return self._last_accepted.get(symbol.upper())

    def last_accepted_ts(self, symbol: str) -> datetime | None:
        """Last accepted tick timestamp (tz-aware UTC) or ``None``."""
        ms = self.last_accepted_ms(symbol)
        return ms_to_datetime(ms) if ms is not None else None

    def gap_threshold(self, symbol: str) -> float:
        """Effective gap threshold (seconds) for ``symbol`` (spec §5.5):
        ``max(3 × cadence, per-symbol override, SYNC_GAP_MIN_SECONDS)``."""
        symbol = symbol.upper()
        return max(
            3.0 * self._cadence.get(symbol, 1.0),
            self._config.gap_threshold_for(symbol),
            self._config.gap_min_seconds,
        )

    def publish_event(self, event: dict) -> None:
        """Publish a SyncEvent dict to subscribers (worker progress events)."""
        if self._config.publish_events:
            self._publisher.publish(event)

    def begin_backfill(self, symbol: str, reason: str = "backfill") -> None:
        """Enter the backfill state for ``symbol`` (worker-driven).

        Idempotent for in-flight states: ``INIT → SYNCING_HISTORY``,
        ``GAP → BACKFILLING``, ``RESET → SYNCING_HISTORY`` (contracts §5.2).
        A symbol already in ``SYNCING_HISTORY``/``BACKFILLING`` is left
        untouched (the worker may re-plan after a provider switch).
        """
        symbol = symbol.upper()
        state = self._states[symbol]
        if state in (SyncState.SYNCING_HISTORY, SyncState.BACKFILLING):
            return
        if state == SyncState.INIT:
            self._transition(symbol, SyncState.SYNCING_HISTORY, reason)
        elif state == SyncState.GAP:
            self._transition(symbol, SyncState.BACKFILLING, reason)
        elif state == SyncState.RESET:
            self._transition(symbol, SyncState.SYNCING_HISTORY, reason)
        else:
            raise SyncError(
                SyncErrorCode.INVALID_STATE,
                f"backfill not allowed from state {state.value} for {symbol} "
                "(contracts §5.2)",
            )

    def complete_backfill(self, symbol: str, reason: str = "backfill complete") -> None:
        """``SYNCING_HISTORY``/``BACKFILLING → LIVE`` (spec §5.5: watermark
        passed / continuity verified). No-op in any other state.

        The SYNCING_HISTORY live-tick buffer (spec §5.5) is flushed on the
        writer thread BEFORE the LIVE transition — the buffered live ticks
        are newer than the backfill pages, so committing them after LIVE
        would push the backfill's older ticks out-of-window. Blocking until
        the writer has flushed and transitioned (single-writer, spec §6.1).
        """
        symbol = symbol.upper()
        if self._states[symbol] not in (SyncState.SYNCING_HISTORY, SyncState.BACKFILLING):
            return
        done = threading.Event()
        try:
            self._queue.put(("flush_history", symbol, done, reason))
        except queue.Full:
            raise SyncError(
                SyncErrorCode.ENGINE_NOT_RUNNING,
                f"queue full during complete_backfill for {symbol}",
            ) from None
        if not done.wait(300.0):
            raise SyncError(
                SyncErrorCode.ENGINE_NOT_RUNNING,
                f"history flush timed out for {symbol}",
            )

    def fail_backfill(self, symbol: str, reason: str = "backfill failed") -> None:
        """Failure path (spec §5.5): ``BACKFILLING → GAP`` (retry w/ backoff);
        ``SYNCING_HISTORY`` stays put (retry in place)."""
        symbol = symbol.upper()
        if self._states[symbol] == SyncState.BACKFILLING:
            self._transition(symbol, SyncState.GAP, reason)

    def report_gap(self, symbol: str, from_ts: datetime, to_ts: datetime,
                   expected_cadence_seconds: float) -> None:
        """Timer-driven gap detection (T6 worker): ``LIVE → GAP`` + publish
        ``gap_detected`` (contracts §6.4). No-op unless the symbol is LIVE
        (contracts §5.2: only LIVE may enter GAP)."""
        symbol = symbol.upper()
        if self._states[symbol] != SyncState.LIVE:
            return
        self._transition(
            symbol,
            SyncState.GAP,
            f"no tick for {(to_ts - from_ts).total_seconds():.1f}s "
            f"(threshold {expected_cadence_seconds:.1f}s)",
        )
        if self._config.publish_events:
            self._publisher.publish(
                gap_detected(symbol, from_ts, to_ts, expected_cadence_seconds)
            )

    def resume_live_subscription(self, force: bool = False) -> None:
        """Re-establish the live subscription after a gap/reconnect (T6).

        Without ``force`` the provider's connection status is consulted: a
        provider reporting ``connected`` is presumed alive and left alone.
        ``force=True`` (provider switch) always re-subscribes. Best-effort —
        providers without a streaming path simply log (spec §11.4).
        """
        if not self._running:
            return
        if not force:
            provider = self._resolve_provider()
            status = getattr(provider, "get_connection_status", None)
            if status is not None:
                try:
                    conn = status()
                except Exception:  # noqa: BLE001 — status is advisory
                    conn = None
                if conn is not None and str(getattr(conn, "value", conn)) == "connected":
                    return  # stream presumed alive
        self._start_live_subscription()

    def snapshot(self) -> dict[str, list[MarketTick]]:
        """Per-symbol materialized window views: ``{symbol: [MarketTick,
        ...]}`` chronological (spec §6.3; contracts §5 refined)."""
        return {symbol: store.snapshot() for symbol, store in self._stores.items()}

    def query_range(
        self,
        start: datetime,
        end: datetime,
        limit: int | None = None,
        symbol: str | None = None,
    ) -> list[MarketTick]:
        """Chronological slice across the engine windows (contracts §5
        refined). ``symbol=None`` → merged chronological view across all
        tracked symbols; ``limit`` applies to the NEWEST end (spec §5.2)."""
        if symbol is not None:
            symbol = symbol.upper()
            if symbol not in self._stores:
                raise SyncError(
                    SyncErrorCode.INVALID_REQUEST,
                    f"symbol {symbol!r} is not tracked",
                )
            return self._stores[symbol].query_range(start, end, limit)
        merged: list[MarketTick] = []
        for store in self._stores.values():
            merged.extend(store.query_range(start, end, None))
        merged.sort(key=lambda tick: tick.timestamp)
        if limit is not None and limit > 0 and len(merged) > limit:
            merged = merged[-limit:]
        return merged

    def stats(self) -> dict[str, object]:
        """Per-symbol WindowStats map (contracts §5)."""
        return {symbol: store.stats() for symbol, store in self._stores.items()}

    @property
    def state(self) -> dict[str, SyncState]:
        """Per-symbol recovery state (contracts §5)."""
        return dict(self._states)

    @property
    def engine_state(self) -> SyncState:
        """Engine-level state = worst state across symbols (contracts §5)."""
        return max(self._states.values(), key=lambda s: _STATE_RANK[s.value])

    @property
    def enabled(self) -> bool:
        """True when SYNC_ENGINE_ENABLED=true (spec §10 Phase 0)."""
        return self._config.engine_enabled

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Bootstrap (spec §5.3): resolve active provider via factory,
        cold-start backfill (SYNCING_HISTORY), then live subscription.
        Idempotent; raises BrokerError on provider failure."""
        if self._running:
            return
        if not self._config.engine_enabled:
            raise SyncError(
                SyncErrorCode.ENGINE_DISABLED,
                "SYNC_ENGINE_ENABLED=false — sync engine is inactive "
                "(spec §10 Phase 0)",
            )
        provider = self._resolve_provider()
        self._active_provider_name = provider.name
        for symbol in self._symbols:
            self._stores[symbol].provider = provider.name

        self._stop_event.clear()
        self._running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="sync-engine-writer",
            daemon=True,
        )
        self._writer_thread.start()

        try:
            if self._worker is None:
                # Cold start: INIT → SYNCING_HISTORY → LIVE per symbol (spec
                # §1.3a/b). Live ticks arriving meanwhile are buffered by the
                # bounded queue and reconciled after the backfill watermark
                # passes them (spec §5.5; full buffering refinement is T6).
                for symbol in self._symbols:
                    self.backfill(symbol)
            # With a worker attached the cold-start backfill is driven by the
            # worker (rate-limited, progress events, cooperative cancel) —
            # the live subscription starts first so ticks buffer in the
            # bounded queue during SYNCING_HISTORY (spec §5.5).
            self._start_live_subscription()
        except BaseException:
            self._running = False
            self._stop_event.set()
            raise

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="sync-engine-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        if self._worker is not None:
            self._worker.start()
        logger.info(
            "sync engine started: symbols=%s provider=%s worker=%s",
            self._symbols,
            self._active_provider_name,
            self._worker is not None,
        )

    def stop(self) -> None:
        """Teardown (spec §5.3): stop the worker, drain queue, stop writer +
        heartbeat. Idempotent. The provider itself is owned by the factory
        and is NOT disconnected here (spec §6.5)."""
        if not self._running:
            return
        self._stop_event.set()
        if self._worker is not None:
            self._worker.stop(timeout=5)
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:  # pragma: no cover — writer will exit on stop_event
            pass
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=10)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
        self._running = False
        logger.info("sync engine stopped")

    # ------------------------------------------------------------------
    # Provider resolution & switching (spec §6.5)
    # ------------------------------------------------------------------
    def _resolve_provider(self) -> object:
        if self._provider is not None:
            return self._provider
        return TradingProviderFactory.get_active()

    def _heartbeat_loop(self) -> None:
        """Poll the factory's active provider identity; on change, enqueue a
        provider-switch event for the writer (spec §6.5)."""
        while not self._stop_event.wait(_HEARTBEAT_SECONDS):
            try:
                provider = self._resolve_provider()
                name = provider.name
            except Exception:  # noqa: BLE001 — provider resolution may fail
                logger.warning("sync engine heartbeat: provider resolution failed", exc_info=True)
                continue
            with self._provider_lock:
                if (
                    self._active_provider_name is not None
                    and name != self._active_provider_name
                ):
                    old = self._active_provider_name
                    self._active_provider_name = name  # set first — no re-trigger
                    try:
                        self._queue.put_nowait(("provider_switch", old, name))
                    except queue.Full:
                        self._dropped += 1
                        logger.warning(
                            "sync engine: provider switch event dropped (queue full)"
                        )

    def _handle_provider_switch(self, old_provider: str, new_provider: str) -> None:
        """Per-symbol RESET → SYNCING_HISTORY on provider switch (spec §5.5).

        The window is retained per ``SYNC_KEEP_WINDOW_ON_SWITCH`` (default
        true — the window is provider-agnostic and survives; only the live
        stream restarts). Re-backfill runs on a background thread so the
        writer is never blocked.
        """
        logger.info(
            "sync engine: provider switched %s → %s", old_provider, new_provider
        )
        if self._config.publish_events:
            self._publisher.publish(provider_switched(old_provider, new_provider))
        for symbol in self._symbols:
            old_state = self._states[symbol]
            self._states[symbol] = SyncState.RESET
            if not self._config.keep_window_on_switch:
                self._stores[symbol].clear()
            else:
                self._stores[symbol].provider = new_provider
            self._last_accepted[symbol] = None
            # Buffered live ticks from the old provider are stale — drop them
            # (the re-backfill will re-cover the window).
            self._history_buffers.pop(symbol, None)
            self._transition(
                symbol,
                SyncState.SYNCING_HISTORY,
                f"provider switch {old_provider} → {new_provider}",
            )
        # Re-backfill on the new provider. With a worker attached the worker
        # owns the re-orchestration (rate-limited, progress events, resume);
        # otherwise fall back to the engine's background thread (T5).
        if self._worker is not None:
            self._worker.on_provider_switched(old_provider, new_provider)
        else:
            threading.Thread(
                target=self._rebackfill_all,
                name="sync-engine-rebackfill",
                daemon=True,
            ).start()

    def _rebackfill_all(self) -> None:
        for symbol in self._symbols:
            try:
                self.backfill(symbol)
            except Exception:  # noqa: BLE001 — recovery is best-effort here
                logger.exception("sync engine re-backfill failed for %s", symbol)

    def _start_live_subscription(self) -> None:
        """Subscribe the active provider's live stream into the queue.

        Best-effort: providers without a working streaming path (legacy
        Pepperstone, spec §11.4) simply log; the engine stays LIVE and T6's
        recovery controller owns reconnect handling.
        """
        try:
            provider = self._resolve_provider()
            subscribe = getattr(provider, "subscribe_to_market_data", None)
            if subscribe is None:
                logger.warning(
                    "sync engine: provider %s has no subscribe_to_market_data",
                    provider.name,
                )
                return
            self._subscription_id = subscribe(self._symbols, self._on_live_tick)
            logger.info(
                "sync engine: live subscription %s active on %s",
                self._subscription_id,
                provider.name,
            )
        except (BrokerError, NotImplementedError) as exc:
            logger.warning(
                "sync engine: live subscription failed (engine stays LIVE; "
                "recovery is T6): %s",
                exc,
            )

    def _on_live_tick(self, tick: MarketTick) -> None:
        """Provider callback thread → bounded queue (spec §6.1: callbacks
        never touch storage — they only put())."""
        if not self._running:
            return
        try:
            self._queue.put_nowait(("tick", tick, tick.provider, tick.symbol))
        except queue.Full:
            self._dropped += 1

    # ------------------------------------------------------------------
    # Writer thread (single-writer, spec §6.1-6.2)
    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        flush_s = self._config.commit_flush_ms / 1000.0
        batch_size = self._config.commit_batch_size
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is _STOP:
                self._drain()
                return
            batch = [item]
            deadline = time.monotonic() + flush_s
            while len(batch) < batch_size and not self._stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=remaining))
                except queue.Empty:
                    break
            try:
                self._process_batch(batch)
            except Exception:  # noqa: BLE001 — never kill the writer
                logger.exception("sync engine writer: batch processing failed")

    def _drain(self) -> None:
        """Process everything left in the queue (stop path, spec §5.3)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _STOP:
                continue
            if isinstance(item, tuple) and item and item[0] == "flush":
                item[1].set()  # unblock backfill waiters
                continue
            if isinstance(item, tuple) and item and item[0] == "flush_history":
                item[2].set()  # unblock complete_backfill waiters
                continue
            try:
                self._process_batch([item])
            except Exception:  # noqa: BLE001
                logger.exception("sync engine writer: drain item failed")

    def _process_batch(self, items: list[object]) -> None:
        for item in items:
            if not isinstance(item, tuple) or not item:
                continue
            kind = item[0]
            if kind == "tick":
                _raw, provider, symbol = item[1], item[2], item[3]
                self._ingest_one(_raw, provider, symbol)
            elif kind == "commit":
                _tick, provider, symbol, seq = item[1], item[2], item[3], item[4]
                self._commit_one(_tick, provider, symbol, seq)
            elif kind == "flush":
                item[1].set()
            elif kind == "flush_history":
                _symbol, done, reason = item[1], item[2], item[3]
                try:
                    self._flush_history(_symbol)
                    self._transition(
                        _symbol, SyncState.LIVE, reason
                    )
                finally:
                    done.set()
            elif kind == "provider_switch":
                self._handle_provider_switch(item[1], item[2])
        # Dual-boundary eviction after each commit batch (spec §4.3).
        if self._touched:
            now_ms = int(time.time() * 1000)
            max_age_ms = self._config.window_max_days * 86_400_000
            for symbol in self._touched:
                store = self._stores[symbol]
                ev1 = store.evict_older_than(now_ms - max_age_ms)
                ev2 = store.evict_to_max_count(self._config.window_max_ticks)
                evicted = ev1.evicted_ticks + ev2.evicted_ticks
                if evicted and self._config.publish_events:
                    self._publisher.publish(
                        window_advanced(
                            symbol,
                            evicted,
                            store.oldest_ts(),
                            store.newest_ts(),
                            len(store),
                        )
                    )
            self._touched.clear()

    def _ingest_one(self, raw: object, provider: str, symbol: str) -> None:
        """Live path: normalize (validate) → buffer during cold start →
        dedup → gap detect → commit."""
        if isinstance(raw, MarketTick):
            # Pre-normalized payload (provider backfill pages, tests) — the
            # generic pass-through path still validates prices/timestamp/
            # symbol (contracts §3: MarketTick is canonical, validation
            # remains the enforcement point).
            tick = self._normalizer.normalize(raw, "generic", symbol)
        else:
            tick = self._normalizer.normalize(raw, provider, symbol)
        if tick is None:
            self._invalid[symbol] = self._invalid.get(symbol, 0) + 1
            return
        seq = self._normalizer.extract_seq(raw, tick)
        canonical = tick.symbol.upper()
        state = self._states.get(canonical)
        if state in (SyncState.INIT, SyncState.SYNCING_HISTORY):
            # Spec §5.5: live ticks arriving during cold start are buffered
            # (bounded, SYNC_HISTORY_BUFFER) and reconciled after the backfill
            # watermark passes them. Committing them immediately would make
            # the backfill's older ticks out-of-window (I6).
            self._buffer_history(canonical, tick, provider, seq)
            return
        self._commit_one(tick, provider, symbol, seq)

    def _buffer_history(
        self, symbol: str, tick: MarketTick, provider: str, seq: int
    ) -> None:
        """Buffer one live tick during INIT/SYNCING_HISTORY (spec §5.5).

        Bounded by ``SYNC_HISTORY_BUFFER`` (default 10,000): when full, the
        OLDEST buffered tick is dropped and counted (the backfill watermark
        will not pass it anyway).
        """
        buf = self._history_buffers.setdefault(symbol, [])
        if len(buf) >= self._config.history_buffer:
            buf.pop(0)
            self._buffer_drops[symbol] = self._buffer_drops.get(symbol, 0) + 1
        buf.append((tick, provider, symbol, seq))

    def _flush_history(self, symbol: str) -> None:
        """Writer-thread flush of the SYNCING_HISTORY buffer (spec §5.5).

        Commits the buffered live ticks in timestamp order (the backfill
        watermark has passed them), then the caller transitions to LIVE.
        """
        buf = self._history_buffers.pop(symbol, [])
        if not buf:
            return
        buf.sort(key=lambda item: to_epoch_ms(item[0].timestamp) or 0)
        for tick, provider, sym, seq in buf:
            self._commit_record(tick, provider, sym, seq)
        logger.info(
            "sync engine %s: flushed %d buffered live ticks after backfill "
            "watermark passed (drops=%d)",
            symbol,
            len(buf),
            self._buffer_drops.get(symbol, 0),
        )

    def _commit_one(
        self, tick: MarketTick, provider: str, symbol: str, seq: int
    ) -> None:
        """Dedup → gap detect → commit → publish for one normalized tick."""
        canonical = tick.symbol.upper()
        store = self._stores.get(canonical)
        if store is None:
            self._untracked[canonical] = self._untracked.get(canonical, 0) + 1
            return
        ts = to_epoch_ms(tick.timestamp)
        if ts is None:
            self._invalid[canonical] = self._invalid.get(canonical, 0) + 1
            return

        # Gap detect (spec §5.5): only in LIVE, against the last accepted ts.
        state = self._states[canonical]
        last = self._last_accepted.get(canonical)
        if state == SyncState.LIVE and last is not None:
            threshold = self.gap_threshold(canonical)
            if ts - last > threshold * 1000.0:
                self._transition(
                    canonical,
                    SyncState.GAP,
                    f"no tick for {(ts - last) / 1000.0:.1f}s "
                    f"(threshold {threshold:.1f}s)",
                )
                if self._config.publish_events:
                    self._publisher.publish(
                        gap_detected(
                            canonical,
                            ms_to_datetime(last),
                            ms_to_datetime(ts),
                            threshold,
                        )
                    )

        # Commit (append routes late ticks to the late-insert path).
        self._commit_record(tick, provider, canonical, seq)

    def _commit_record(
        self, tick: MarketTick, provider: str, symbol: str, seq: int
    ) -> None:
        """Single-writer commit of one normalized tick (store append, dedup,
        last-accepted watermark, ``tick_committed`` event). Shared by the
        live path (``_commit_one``) and the SYNCING_HISTORY buffer flush."""
        ts = to_epoch_ms(tick.timestamp)
        if ts is None:
            self._invalid[symbol] = self._invalid.get(symbol, 0) + 1
            return
        record = {
            "ts": ts,
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "seq": seq,
        }
        result = self._stores[symbol].append([record])
        if result.inserted == 1:
            self._last_accepted[symbol] = ts
            self._touched.add(symbol)
            if self._config.publish_events:
                self._publisher.publish(
                    tick_committed(
                        symbol,
                        tick.provider,
                        tick.bid,
                        tick.ask,
                        tick.last,
                        tick.timestamp,
                        tick.volume,
                        seq,
                    )
                )

    # ------------------------------------------------------------------
    # Backfill orchestration (spec §5.6 / contracts §2)
    # ------------------------------------------------------------------
    def _run_backfill(self, symbol: str, days: int) -> BackfillResult:
        provider = self._resolve_provider()
        provider_name = provider.name
        self._stores[symbol].provider = provider_name

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        end = now
        batch = self._config.backfill_batch_size
        window_seconds = days * 86_400
        # Estimate: 1 tick/s cadence → ceil(window_seconds / batch) pages.
        pages_total = max(1, -(-window_seconds // batch))
        pages_done = 0
        ticks_committed = 0
        oldest_ts: datetime | None = None
        newest_ts: datetime | None = None

        supports_range = bool(getattr(provider, "supports_ticks_range", False))
        get_range = getattr(provider, "get_ticks_range", None)

        if supports_range and get_range is not None:
            # Windowed paging (contracts §2.1): chronological pages, caller
            # advances start past the last committed tick.
            cursor = start
            while cursor <= end:
                try:
                    page = get_range(symbol, cursor, end, limit=batch)
                except NotImplementedError:
                    # Defensive backstop (contracts §2.4): capability flag
                    # lied — fall back to get_ticks below.
                    logger.warning(
                        "sync engine: %s raised NotImplementedError for "
                        "get_ticks_range — falling back to get_ticks",
                        provider_name,
                    )
                    break
                if not page:
                    break
                enqueued = self._enqueue_page(symbol, provider_name, page)
                ticks_committed += enqueued
                pages_done += 1
                if oldest_ts is None:
                    oldest_ts = page[0].timestamp
                newest_ts = page[-1].timestamp
                if self._config.publish_events:
                    self._publisher.publish(
                        backfill_progress(symbol, pages_done, pages_total, oldest_ts)
                    )
                if len(page) < batch:
                    break  # window exhausted (contracts §2.1)
                last_ts = to_epoch_ms(page[-1].timestamp)
                cursor = ms_to_datetime(last_ts) + timedelta(microseconds=1)
        else:
            # Best-effort fallback (spec §5.6): provider.get_ticks(limit),
            # newest-first → reversed to chronological. Partial windows are
            # legal — the engine tolerates unfilled gaps.
            get_ticks = getattr(provider, "get_ticks", None)
            if get_ticks is None:
                raise SyncError(
                    SyncErrorCode.INVALID_REQUEST,
                    f"provider {provider_name} exposes no tick history primitive",
                )
            page = list(get_ticks(symbol, limit=batch) or [])
            page.reverse()
            enqueued = self._enqueue_page(symbol, provider_name, page)
            ticks_committed += enqueued
            pages_done = 1
            if page:
                oldest_ts = page[0].timestamp
                newest_ts = page[-1].timestamp
            if self._config.publish_events:
                self._publisher.publish(
                    backfill_progress(symbol, pages_done, pages_total, oldest_ts)
                )

        # Wait for the writer to drain everything enqueued (single-writer).
        self._flush_and_wait()

        store = self._stores[symbol]
        stats = store.stats()
        return BackfillResult(
            symbol=symbol,
            pages_done=pages_done,
            pages_total=pages_total,
            ticks_committed=ticks_committed,
            oldest_ts=stats.oldest_ts,
            newest_ts=stats.newest_ts,
            state_after=self._states[symbol],
            provider=provider_name,
        )

    def _enqueue_page(
        self, symbol: str, provider_name: str, page: Sequence[MarketTick]
    ) -> int:
        """Normalize a provider page and enqueue pre-normalized commits.

        Returns the number of ticks enqueued (invalid ticks are dropped).
        """
        enqueued = 0
        for raw in page:
            tick = self._normalizer.normalize(raw, provider_name, symbol)
            if tick is None:
                continue
            seq = self._normalizer.extract_seq(raw, tick)
            try:
                self._queue.put_nowait(("commit", tick, provider_name, symbol, seq))
            except queue.Full:
                self._dropped += 1
                continue
            enqueued += 1
        return enqueued

    def _flush_and_wait(self, timeout: float = 300.0) -> None:
        """Enqueue a flush marker and block until the writer processes it.

        Guarantees every previously enqueued item is committed before the
        caller proceeds (backfill completion barrier).
        """
        done = threading.Event()
        self._queue.put(("flush", done))
        if not done.wait(timeout):
            raise SyncError(
                SyncErrorCode.ENGINE_NOT_RUNNING,
                "writer drain timed out during backfill",
            )

    # ------------------------------------------------------------------
    # State machine (contracts §5.2)
    # ------------------------------------------------------------------
    def _transition(self, symbol: str, new_state: SyncState, reason: str) -> SyncState:
        old = self._states[symbol]
        if old == new_state:
            return old
        allowed = _TRANSITIONS.get(old, set())
        if new_state not in allowed:
            raise SyncError(
                SyncErrorCode.INVALID_STATE,
                f"illegal transition {old.value} → {new_state.value} for {symbol}",
            )
        self._states[symbol] = new_state
        if self._config.publish_events:
            self._publisher.publish(
                recovery_state_changed(symbol, old.value, new_state.value, reason)
            )
        logger.info(
            "sync engine %s: %s → %s (%s)", symbol, old.value, new_state.value, reason
        )
        return new_state

    def set_symbol_cadence(self, symbol: str, cadence_seconds: float) -> None:
        """Set the per-symbol tick cadence used for gap thresholds."""
        symbol = symbol.upper()
        if symbol not in self._cadence:
            raise SyncError(
                SyncErrorCode.INVALID_REQUEST, f"symbol {symbol!r} is not tracked"
            )
        self._cadence[symbol] = float(cadence_seconds)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _require_running(self) -> None:
        if not self._running:
            raise SyncError(
                SyncErrorCode.ENGINE_NOT_RUNNING,
                "sync engine is not running — call start() first",
            )

    def _ingest_result(self, symbol: str, accepted: int) -> IngestResult:
        canonical = symbol.upper()
        last = self._last_accepted.get(canonical)
        return IngestResult(
            accepted=accepted,
            duplicates=0,
            out_of_window=0,
            invalid=0,
            state_after=self._states.get(canonical, self.engine_state),
            last_accepted_ts=ms_to_datetime(last) if last is not None else None,
        )


def create_engine(
    symbols: Sequence[str],
    config: SyncConfig | None = None,
    provider: object | None = None,
) -> SyncEngine:
    """Public factory (spec §2: ``create_engine()`` from ``forex.sync``).

    Registers the instance as the process-wide engine singleton
    (:func:`set_engine`) so the ``/api/sync/*`` layer can reach the running
    engine (contracts §7 — one engine instance per process, spec §2).
    """
    engine = SyncEngine(symbols=symbols, config=config, provider=provider)
    set_engine(engine)
    return engine


# ---------------------------------------------------------------------------
# Process-wide engine singleton (contracts §7 — API layer access)
# ---------------------------------------------------------------------------
#: The registered process-wide engine instance (``None`` when absent).
#: ``create_engine`` registers automatically; the API layer reaches the
#: running engine through :func:`get_engine` (spec §2: one engine per
#: process). The management command's engine is therefore reachable by
#: ``/api/sync/*`` without extra wiring.
_ENGINE: SyncEngine | None = None
_ENGINE_LOCK = threading.Lock()


def set_engine(engine: SyncEngine | None) -> None:
    """Register (or clear) the process-wide engine instance.

    ``create_engine`` calls this automatically; tests may clear it with
    ``set_engine(None)``. Thread-safe (the API layer may read it from any
    request thread while the engine thread runs).
    """
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = engine


def get_engine() -> SyncEngine | None:
    """Return the registered engine instance, or ``None`` when absent.

    The caller must check ``engine.running`` — a registered engine may be
    stopped (``stop()`` keeps the singleton; ``engine_running`` reflects the
    live state in ``/api/sync/status/``).
    """
    with _ENGINE_LOCK:
        return _ENGINE


__all__ = [
    "BackfillResult",
    "IngestResult",
    "SyncEngine",
    "SyncState",
    "create_engine",
    "get_engine",
    "set_engine",
]