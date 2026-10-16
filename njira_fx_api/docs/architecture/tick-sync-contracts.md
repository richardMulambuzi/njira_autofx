# Tick Synchronization Engine — Interface & Data Contracts

| | |
|---|---|
| **Status** | DRAFT FOR IMPLEMENTATION (spec/interface definitions only — no production code) |
| **Owner** | `api-architect` (API & Data Contract Architect) |
| **Governing design** | `tick-sync-engine.md` (architecture spec, ADR `dec-20260819-fc8319`) |
| **Date** | 2026-08-19 |
| **Applies to** | `njira_fx_api` (Django 5.2 + DRF, Python 3.12) |
| **Consumers** | `backend-dev`, `tech-lead`, `database-engineer`, `async-engineer`, `qa-engineer`, `frontend-architect`, `state-engineer`, `documentation-writer` |

> This document is the **api-architect deliverable** for the tick-sync ADR
> (spec §12 open item: "api-architect to confirm the `get_ticks_range` contract
> shape and the Phase-4 `/api/sync/*` surface"). It defines the module
> interfaces, DTOs, event payloads and REST contracts that implementers must
> code against. Where this document refines the architecture spec's §5 sketch,
> the refinement is **flagged explicitly** (§9). Nothing here invents
> endpoints, fields or storage backends beyond the spec and the verified
> broker files.

---

## 0. Source of truth (verified in repo — cited throughout)

| File | What it grounds here |
|---|---|
| `docs/architecture/tick-sync-engine.md` | ADR `dec-20260819-fc8319`: §1.1 pipeline, §1.2 ordering invariant, §1.3 window semantics, §3.3 record layout, §3.4 segment K=100k, §3.5 complexity, §4.1 insert paths, §4.2 dedup key, §4.3 eviction, §5.1–5.6 contracts, §6 concurrency, §7.1 provider integration, §7.2 consumers, §9 testing |
| `forex/broker/base.py` | `TradingProvider` ABC — contract rules (lines 6–12): failures → `BrokerError`; `ValueError` reserved for programming errors |
| `forex/broker/models.py` | Canonical value objects: `MarketTick(provider, symbol, bid, ask, last, timestamp, volume=0.0)` (lines 202–216) |
| `forex/broker/errors.py` | `BrokerErrorCode` taxonomy — reused verbatim by the sync layer (§0 spec) |
| `forex/broker/health.py` | `ProviderHealth.to_dict()` — ISO-8601 UTC wire convention (lines 47–59) |
| `forex/broker/factory.py` | `TradingProviderFactory.switch()` — provider-switch detection source (§6.5 spec) |
| `forex/broker/providers/mock.py` | 1000-tick buffer (lines 218–220), fail modes (281–300), tick loop (443–462) |
| `forex/broker/providers/pepperstone.py` | `_run_query` subprocess worker (215–306), ClickHouse `get_ticks` (564–596) |
| `forex/broker/views.py` | Response envelope `{status, message, data, errors}` (lines 22–23), `_broker_error_response` HTTP mapping (94–116), staff check pattern (465–476), symbol allowlist (49) |
| `forex/ticks_views.py` | `SYMBOL_PATTERN` (46), `MAX_TIMESTAMP_SKEW` (125), chart-data limits (59–60) |
| `forex/broker/config.py` | `TRADING_PROVIDER` (default `mock`), `TRADING_MODE` (demo\|paper\|live) |

---

## 1. Contract conventions (binding)

1. **Type style**: every interface below is a Python `Protocol` (or ABC method
   signature) defined in `forex/sync/` — `normalizer.py`, `storage.py`,
   `engine.py`, `publisher.py`, `backfill.py`, `errors.py`, `config.py`
   (spec §2 module map). Implementations live in the same modules
   (`backend-dev` owns them per spec §2.1).
2. **Datetimes**: every `datetime` in these contracts is **timezone-aware
   UTC**. Naive datetimes are rejected with `ValueError` (programming error).
3. **Wire format**: JSON. Datetimes serialize as ISO-8601 UTC strings
   (`isoformat()`), mirroring `ProviderHealth.to_dict()` (health.py lines
   47–59) and `_json_safe` (broker/views.py lines 61–76).
4. **Error taxonomy**: provider failures are `BrokerError` with
   `BrokerErrorCode` (errors.py). The sync layer adds a **minimal** additive
   taxonomy (`SyncErrorCode`, spec §2: "reuses BrokerErrorCode where
   applicable") — see §7.4. `ValueError` = programming error;
   `NotImplementedError` = declared capability absent (see §2.4).
5. **Provider neutrality**: no interface below names a provider.
   `get_ticks_range` capability is advertised per provider (§2.4); the engine
   never imports provider classes (spec §2.2 dependency direction).
6. **Threading**: one writer thread owns all `SegmentStore` mutations; all
   ingestion funnels through the bounded queue (spec §6.1–6.2). Reader
   methods (`query_range`, `snapshot`, `stats`, `__len__`) are safe under the
   shared reader lock.

---

## 2. `TradingProvider.get_ticks_range` — ABC addition (spec §5.6)

Additive extension of `forex/broker/base.py` (spec §5.6: "additive, does not
break existing providers"). Placed in the **Market data** section of
`TradingProvider`, directly after `get_ticks` (base.py lines 122–126).

### 2.1 Exact signature and docstring contract

```python
# forex/broker/base.py — class TradingProvider(ABC)  (additive)

#: Capability advertisement. False (default) → the BackfillPlanner MUST NOT
#: call get_ticks_range and uses the get_ticks(limit) fallback (spec §5.6).
#: True → the provider honors the full [start, end] contract below.
supports_ticks_range: bool = False

def get_ticks_range(
    self,
    symbol: str,
    start: datetime,
    end: datetime,
    limit: int = 1000,
) -> list[MarketTick]:
    """Return the OLDEST ``limit`` ticks in the inclusive window
    ``[start, end]``, in chronological (ascending-timestamp) order.

    Window semantics
    ----------------
    * Candidates are ticks ``t`` with ``start <= t.timestamp <= end``
      (inclusive on both bounds).
    * Returns at most ``limit`` ticks: the OLDEST ticks in the window.
      When the window contains fewer than ``limit`` ticks, all of them
      are returned (a short page means "no more data in window").
    * ``limit`` is clamped to the provider's per-request capability
       (Mock: 1000; Pepperstone: query-worker defined; generic cap 1000 when
       ``supports_ticks_range``). Providers do NOT page internally — one call,
       one page.
    * Ordering is chronological (oldest first). ``ts[i] <= ts[i+1]``.

    Paging contract (caller-driven)
    -------------------------------
    When ``len(result) == limit`` the page is full and more ticks may exist
    in ``[start, end]``. The caller advances:

        next_start = result[-1].timestamp + timedelta(microseconds=1)

    and repeats the call. When ``len(result) < limit`` the window is
    exhausted — stop. (Reverse/newest-first walk — spec §5.6 — is achieved
    by shrinking ``end`` to ``result[0].timestamp - 1us``; both directions
    are supported by the parameterization.) The engine's dedup (spec §4.2)
    absorbs any boundary overlap between pages.

    Arguments
    ---------
    symbol: Instrument key (``^[A-Z0-9_]{1,20}$``, ticks_views.py:46).
    start:  Window lower bound, inclusive, tz-aware UTC.
    end:    Window upper bound, inclusive, tz-aware UTC.
    limit:  Maximum ticks per call (1..provider_cap, default 1000).

    Raises
    ------
    ValueError:  ``start > end``, naive datetimes, or ``limit < 1``
                 (programming errors — base.py contract rule, lines 6-12).
    BrokerError: provider failures, normalized to BrokerErrorCode
                 (e.g. SYMBOL_NOT_FOUND, RATE_LIMITED, NETWORK_ERROR,
                 BROKER_UNAVAILABLE, TOKEN_EXPIRED).
    NotImplementedError: provider does not support windowed history AND
                 ``supports_ticks_range`` is False (see §2.4). This is a
                 capability signal, never a provider failure.

    Health: on success record via ProviderHealthMonitor.record_market_data();
    on failure record_error(...) — existing pattern (pepperstone.py:303,
    mock.py:296-300).
    """
    raise NotImplementedError(
        f"{type(self).__name__} does not support windowed tick history "
        "(get_ticks_range); set supports_ticks_range = False (default) and "
        "the BackfillPlanner will use the get_ticks(limit) fallback "
        "(tick-sync-engine.md §5.6)."
    )
```

### 2.2 Return contract

- Returns **chronological** `list[MarketTick]` (models.py lines 202–216):
  `provider` = canonical provider key (`ProviderName` value), `symbol` =
  the requested symbol (upper-cased), `bid`/`ask`/`last` finite positive
  floats, `timestamp` tz-aware UTC, `volume` default 0.0.
- The provider's `MarketTick` records are **already normalized** (base.py
  contract rule line 10: "All methods return the normalized value objects
  from `forex.broker.models`"). The `TickNormalizer` (§3) still runs on
  these records inside the engine — it is the enforcement point for
  price/timestamp/symbol validation (spec §5.1) and the sole derivation
  point of the dedup `seq`.

### 2.3 Per-provider implementation contract

| Provider | `supports_ticks_range` | Implementation |
|---|---|---|
| **Pepperstone** (`providers/pepperstone.py`) | `True` **only after** the query-worker history extension lands; `False` today | Query-plane extension: add a `ticks_range` query type to `forex.pepperstone.query_worker` (params `{symbol, start_epoch_ms, end_epoch_ms, limit}`) and implement `get_ticks_range` via `self._run_query("ticks_range", params)` (pepperstone.py lines 215–306). Map rows → `MarketTick` with `last = bid` (existing convention, pepperstone.py:592). One-shot subprocess ≈ 1–2 s/call → the planner rate-limits and recovery tolerates slow backfill (spec §7.1). Until the worker ships, keep `supports_ticks_range = False`. |
| **Mock** (`providers/mock.py`) | `True` | Deterministic synthetic history generator: seeded random walk (seed derived from `(symbol, start, end, limit)` for reproducible tests — spec §9.3 uses Mock as the deterministic vehicle), fixed cadence (1 tick/s, mock-internal), covering `[start, end]`, oldest-first, at most `min(limit, 1000)` ticks (1000 = buffer cap, spec §5.6/§7.1). Honors `set_fail_mode` via `_check_fail_mode` (mock.py:281–300) so GAP/BACKFILLING paths are exercisable (spec §9.3). May return partial windows; the engine tolerates unfilled gaps (spec §5.6). |

### 2.4 NotImplementedError / graceful contract (binding)

1. `supports_ticks_range: bool = False` is the **default** class attribute on
   `TradingProvider`. A provider that does not (yet) implement windowed
   history does nothing — it inherits the default and the raising base method.
2. The **BackfillPlanner MUST check `supports_ticks_range` before calling**
   (spec §5.6 fallback: `provider.get_ticks(limit)` newest-first, best-effort,
   rate-limited). This is the graceful degradation path — never exception
   flow for the common case.
3. `NotImplementedError` is a **defensive backstop** only. If it escapes, the
   planner catches it and falls back exactly as in (2). It is never a
   `BrokerError`: it signals a missing *contract capability* (the same class
   of programming/contract error as `ValueError` in base.py's taxonomy,
   lines 6–12), not a provider failure.
4. A provider that sets `supports_ticks_range = True` and still raises
   `NotImplementedError` **violates this contract** — it must raise
   `BrokerError` for every data-plane failure instead.

---

## 3. `TickNormalizer` Protocol (spec §5.1, refined)

Lives in `forex/sync/normalizer.py`. One normalizer instance per provider
(pure, stateless, thread-safe — spec §5.1).

```python
# forex/sync/normalizer.py
from typing import Any, Protocol

class TickNormalizer(Protocol):
    """Provider-native payload → canonical MarketTick (or drop).

    Refinement of spec §5.1: the input is the RAW provider payload
     (Pepperstone query-worker row, Mock internal tick, or generic provider
     WS tick msg), not a pre-normalized MarketTick — the engine accepts
     payloads directly from provider callback threads / backfill rows (spec
     §1.1 pipeline: Provider Adapter → Tick Normalizer → Engine).
     """

    def normalize(self, raw: Any, provider: str, symbol: str) -> MarketTick | None:
        """Validate and canonicalize one provider-native tick payload.

        Returns a canonical :class:`MarketTick` (models.py:202-216) or
        ``None`` when the payload must be DROPPED (invalid/unsupported).
        Drop reasons (log at ``SYNC_DUP_LOG_RATE``-style rate, spec §4.2):
          - non-finite or non-positive prices; ``bid > ask`` (epsilon 1e-9)
            — spec §5.1
          - timestamp outside [SYNC_MIN_TS, now + SYNC_MAX_FUTURE_SKEW]
            (default skew 5 min — mirrors ticks_views.MAX_TIMESTAMP_SKEW,
            ticks_views.py:125) — spec §5.1
          - ``symbol`` failing ``^[A-Z0-9_]{1,20}$`` (SYMBOL_PATTERN,
            ticks_views.py:46) — spec §5.1
          - unparseable provider payload (missing quote/epoch, wrong shape)

        ``raw`` is provider-native and opaque to the engine:
          - Pepperstone: query-worker history row
            ``{"timestamp", "bid", "ask", "last"}``
          - Mock: internal ``MarketTick`` (pass-through)
          - Generic: provider WS ``tick`` message dict
            ``{"tick": {symbol, quote, epoch}}``
        """

    def dedup_key(self, raw: Any, tick: MarketTick) -> tuple[int, int]:
        """Return the dedup key for a normalized tick (spec §4.2/§5.1).

        ``(ts_ms, seq)`` when the provider payload carries a tick id /
        sequence (``seq != -1``); else ``(ts_ms, float_bits(last))`` where
        ``float_bits`` is the IEEE-754 bit pattern
        (``struct.pack(">d", last)`` → int) — exact comparison, no epsilon.

        Refinement of spec §5.1: takes ``raw`` as well as ``tick`` because
        the canonical :class:`MarketTick` (models.py:202-216) has NO ``seq``
        field — the provider id exists only in the raw payload. Derivation
        is pure (no state): Pepperstone → deal/tick id when present else -1;
        Mock → -1; generic providers → -1 when no tick id is carried.
        """
```

**Composition rule**: the engine writer thread converts
`(normalize(raw, ...), dedup_key(raw, tick))` into the storage record —
`ts` (epoch ms int64, spec §3.3), `bid`, `ask`, `last`, `seq` — so
`SegmentStore` dedup (§4.3) is fully internal once the record carries `seq`.

---

## 4. `SegmentStore` Protocol (spec §5.2, refined)

Lives in `forex/sync/storage.py`. **One instance per symbol** (the engine
holds `dict[symbol → SegmentStore]`; `SYNC_MAX_SYMBOLS` default 4 caps
memory at ~544 MB FULL, spec §3.3).

```python
# forex/sync/storage.py
from typing import Protocol, Sequence

class SegmentStore(Protocol):
    """Owns one symbol's segmented sorted window (spec §3-4).

    Single-writer (engine thread only, spec §6.1); readers via shared lock.
    Backed by pre-allocated numpy structured arrays, capacity
    K = SYNC_SEGMENT_CAPACITY (default 100_000, spec §3.4), held in a
    time-sorted, non-overlapping segment deque (spec §3.4).
    """

    def append(self, ticks: Sequence[MarketTick]) -> InsertResult:
        """Commit a batch of live ticks (spec §4.1 append path).

        Contract: caller guarantees ticks are normalized and roughly
        chronological; ticks with ``ts >= active.last_ts`` take the O(1)
        amortized fill-pointer path with tail dedup (spec §4.1). Any tick
        older than the active segment's last_ts is routed internally to the
        late-insert path. Deduped/out-of-window ticks are counted, never
        stored (spec §4.2). Seals the active segment at fill == K and opens
        a fresh one (spec §4.1).
        """

    def insert_late(self, tick: MarketTick) -> LateInsertOutcome:
        """Insert one tick with ``ts < active.last_ts`` (spec §4.1 late path).

        Locate segment O(log S) via the boundary index (spec §3.4); dedup
        check O(log K) on ``searchsorted``; O(K) memmove insert; split the
        segment when full (spec §4.1 split invariant). Returns
        ``LateInsertOutcome.INSERTED`` | ``DUPLICATE`` | ``OUT_OF_WINDOW``.
        """

    def query_range(
        self,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[MarketTick]:
        """Chronological slice of [start, end], oldest first (spec §5.2).

        O(m + S): slice filled portions of ≤ S segments, concatenate (spec
        §3.5). ``limit`` applies to the NEWEST end (spec §5.2) — when set,
        the newest ``limit`` ticks of the window are returned (this matches
        the existing chart-data consumption shape).
        """

    def snapshot(self) -> list[MarketTick]:
        """Materialized chronological copy of the whole window (spec §6.3).

        O(n), rare. The consumer-visible immutable view; the engine may
        publish sealed segments lock-free in Phase 3 (spec §6.3) but the
        contract here is the plain list.
        """

    def stats(self) -> WindowStats:
        """WindowStats: count, oldest_ts, newest_ts, segment_count,
        evictions, duplicates (spec §5.2)."""

    def evict_older_than(self, ts: datetime) -> EvictionResult:
        """Age boundary (spec §4.3): drop oldest segments while
        ``oldest_segment.last_ts < ts``. Segment-granular; the age boundary
        is exact to within one segment's span (state in stats(), spec
        §4.3)."""

    def evict_to_max_count(self, n: int) -> EvictionResult:
        """Count boundary (spec §4.3): drop oldest segments while
        ``total_count > n``. O(1) per segment ``popleft()`` (spec §3.5)."""

    def __len__(self) -> int:
        """Total stored tick count (== stats().count)."""
```

### 4.1 DTOs (api-architect-owned shapes)

```python
@dataclass(frozen=True)
class InsertResult:            # spec §5.2 field list
    inserted: int
    duplicates: int
    out_of_window: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    total_count: int

class LateInsertOutcome(str, Enum):   # spec §4.1 return values
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    OUT_OF_WINDOW = "out_of_window"

@dataclass(frozen=True)
class EvictionResult:
    evicted_segments: int
    evicted_ticks: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    total_count: int

@dataclass(frozen=True)
class WindowStats:             # spec §5.2 field list
    count: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    segment_count: int
    evictions: int
    duplicates: int
```

### 4.2 Invariants (binding — property-tested per spec §9.1)

- **I1 Ordering**: the window is always sorted by `ts` — `ts[i] <= ts[i+1]`
  for all i (spec §1.2). Logical order is by timestamp, never arrival order.
- **I2 Dedup**: key `(ts_ms, seq)` when `seq != -1`, else `(ts_ms,
  float_bits(last))` (spec §4.2). A duplicate is counted and logged, never
  stored twice.
- **I3 Segments**: every segment `count <= K` (K = `SYNC_SEGMENT_CAPACITY`
  default 100_000, spec §3.4); segments sorted, non-overlapping,
  time-contiguous; split preserves invariants (spec §4.1).
- **I4 Dual-boundary eviction**: `MAX_TICKS = 3_400_000` and
  `MAX_AGE = 60 days` (spec §1.3) — whichever boundary is hit first wins
  (spec §4.3). Enforced via `evict_older_than` + `evict_to_max_count` after
  each commit batch (spec §4.3).
- **I5 Single-writer**: mutations only from the engine writer thread
  (spec §6.1); readers via the shared lock.
- **I6 Out-of-window**: a tick older than the oldest segment is dropped and
  counted — never stored (spec §4.1).
- **I7 Efficiency**: locate O(log S), append O(1) amortized, late insert
  O(K), dedup O(log K), query O(m + S), evict O(1)/segment (spec §3.5 —
  binding for `perf-tester`, spec §8).

---

## 5. `SyncEngine` Protocol (spec §5.3, refined)

Lives in `forex/sync/engine.py`. One engine instance per process, one writer
thread (spec §2/§6.1). Manages the tracked symbol set (constructed via
`create_engine(symbols=[...])` from `forex/sync/__init__.py`, spec §2).

```python
# forex/sync/engine.py
from typing import Callable, Protocol, Sequence

class SyncEngine(Protocol):
    """The pipeline: validate → dedup → gap detect → order → reconcile →
    commit → publish (spec §1.1). Provider-neutral by construction (spec
    §2.2): resolves providers only through TradingProviderFactory."""

    def ingest_tick(self, tick: MarketTick) -> IngestResult:
        """Entry point for one live tick (spec §5.3; also used by tests).

        Funnels through the bounded queue (SYNC_INGEST_QUEUE_SIZE default
        100_000, spec §6.1) → normalized → validated → committed by the
        writer thread. Returns immediately (queue enqueue); the commit
        happens on the next batch drain (spec §6.2).
        """

    def ingest_batch(self, ticks: Sequence[MarketTick]) -> IngestResult:
        """Batched variant; the writer drains SYNC_COMMIT_BATCH_SIZE (1000)
        or SYNC_COMMIT_FLUSH_MS (10 ms) batches (spec §6.2)."""

    def backfill(self, symbol: str, days: int | None = None) -> BackfillResult:
        """Paged historical fill for ``symbol`` over the last ``days``
        (default: SYNC_WINDOW_MAX_AGE_DAYS = 60, spec §1.3 window age).

        Pages via TradingProvider.get_ticks_range when
        ``supports_ticks_range`` (spec §5.6 / §2.4), else the
        ``get_ticks(limit)`` best-effort fallback. Backfill worker threads
        enqueue normalized pages; the writer thread remains the sole
        committer (spec §6.4). Emits backfill_progress events per page
        (spec §5.4). Blocking; returns BackfillResult.
        """

    def subscribe(self, callback: Callable[[dict], None]) -> str:
        """Engine-wide event subscription (spec §5.3 refined): the callback
        receives SyncEvent dicts (§6) which carry ``symbol`` where
        relevant, so per-symbol filtering is a caller-side predicate.
        Returns a subscription id. Callback exceptions are isolated
        (publisher fan-out, spec §9.2)."""

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription (no-op for unknown ids)."""

    def snapshot(self) -> dict[str, list[MarketTick]]:
        """Per-symbol materialized window views: ``{symbol: [MarketTick,
        ...]}`` chronological (spec §6.3; §5.3 refined to a map because
        the CEO contract shape omits the symbol parameter — see §9)."""

    def query_range(
        self,
        start: datetime,
        end: datetime,
        limit: int | None = None,
        symbol: str | None = None,
    ) -> list[MarketTick]:
        """Chronological slice across the engine windows (spec §5.3
        refined). ``symbol=None`` → merged chronological view across all
        tracked symbols; ``symbol`` given → that symbol's window.
        ``limit`` applies to the NEWEST end (spec §5.2)."""

    def stats(self) -> dict[str, WindowStats]:
        """Per-symbol WindowStats map (spec §5.3)."""

    @property
    def state(self) -> dict[str, SyncState]:
        """Per-symbol recovery state (spec §5.5). The engine-level state is
        the worst-state across symbols (for /api/sync/status/)."""

    def start(self) -> None:
        """Bootstrap (spec §5.3): resolve active provider via factory,
        subscribe live ticks, begin cold-start backfill (SYNCING_HISTORY,
        spec §1.3a/b). Idempotent; raises BrokerError on provider failure."""

    def stop(self) -> None:
        """Teardown (spec §5.3): drain queue, disconnect provider, flush
        the archival sink (best-effort, spec §3.2.5). Idempotent."""
```

### 5.1 DTOs

```python
@dataclass(frozen=True)
class IngestResult:
    accepted: int
    duplicates: int
    out_of_window: int
    invalid: int
    state_after: SyncState
    last_accepted_ts: datetime | None

@dataclass(frozen=True)
class BackfillResult:
    symbol: str
    pages_done: int
    pages_total: int
    ticks_committed: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    state_after: SyncState
    provider: str
```

### 5.2 Recovery state machine (spec §5.5 — binding)

```python
class SyncState(str, Enum):
    INIT = "init"
    SYNCING_HISTORY = "syncing_history"
    LIVE = "live"
    GAP = "gap"
    BACKFILLING = "backfilling"
    RESET = "reset"
```

Legal transitions (only these; any other transition is rejected — spec
§9.1.5):

| From | To | Trigger (spec §5.5) |
|---|---|---|
| `INIT` | `SYNCING_HISTORY` | `start()` bootstrap / cold start (§1.3a) |
| `SYNCING_HISTORY` | `LIVE` | backfill watermark passes live frontier — seamless transition (§1.3b) |
| `LIVE` | `GAP` | `now - last_accepted_ts > gap_threshold`, threshold = `max(3 × cadence, SYNC_GAP_MIN_SECONDS = 5)` (§5.5) |
| `GAP` | `BACKFILLING` | recovery planned: paged fill from `last_accepted_ts` → now (§1.3c, §5.5) |
| `BACKFILLING` | `LIVE` | fill completes and continuity verified (walk segment boundaries; residual hole > threshold → re-plan, §5.5) |
| `BACKFILLING` | `GAP` | backfill failed → retry with backoff (§5.5 dotted loop) |
| any | `RESET` | provider switch detected (factory identity change, §6.5) |
| `RESET` | `SYNCING_HISTORY` | window retained per `SYNC_KEEP_WINDOW_ON_SWITCH` (default true, §5.5); live stream restarts |

Illegal (rejected) examples: `INIT → LIVE`, `LIVE → BACKFILLING`,
`SYNCING_HISTORY → GAP`, `RESET → LIVE`, `GAP → SYNCING_HISTORY`.

---

## 6. SyncEvent payloads — publish events (spec §5.4, binding)

`forex/sync/publisher.py` — in-process pub/sub (SSE/WS bridge later, spec
§2). Events are plain **JSON dicts** (the `SyncEvent` wire form), each with
an `"event"` discriminator. Datetimes render as ISO-8601 UTC strings
(§1.3). Field names are snake_case.

| # | Event (wire `event` value) | Spec §5.4 name | Emitted when |
|---|---|---|---|
| 1 | `tick_committed` | `TickCommitted` | a tick is committed to the window |
| 2 | `window_advanced` | `WindowAdvanced` | after the eviction check (spec §4.3) |
| 3 | `recovery_state_changed` | `StateChanged` | any state transition |
| 4 | `gap_detected` | `GapDetected` | live stream stalls past the threshold |
| 5 | `backfill_progress` | `BackfillProgress` | during backfill, per page |
| 6 | `provider_switched` | `ProviderSwitched` | factory switch detected (engine-wide, no symbol) |

### 6.1 `tick_committed`

```json
{
  "event": "tick_committed",
  "symbol": "EURUSD",
  "provider": "pepperstone",
  "bid": 1.08501,
  "ask": 1.08512,
  "last": 1.08501,
  "timestamp": "2026-08-19T10:00:00.123456+00:00",
  "volume": 0.0,
  "seq": -1
}
```

`timestamp` mirrors `MarketTick.timestamp` (models.py); `seq` mirrors the
storage record's `seq` (spec §3.3; `-1` when unavailable).

### 6.2 `window_advanced`

```json
{
  "event": "window_advanced",
  "symbol": "EURUSD",
  "evicted_count": 100000,
  "oldest_ts": "2026-06-20T10:00:00+00:00",
  "newest_ts": "2026-08-19T10:00:00+00:00",
  "total_count": 3400000
}
```

`evicted_count` = ticks dropped by the eviction pass (segment-granular,
spec §4.3).

### 6.3 `recovery_state_changed`

```json
{
  "event": "recovery_state_changed",
  "symbol": "EURUSD",
  "old_state": "live",
  "new_state": "gap",
  "reason": "no tick for 12.4s (threshold 5.0s)"
}
```

`old_state`/`new_state` are `SyncState` values (§5.2); `reason` is a
human-readable, log-safe string. Every legal transition emits this event
(§5.2 table).

### 6.4 `gap_detected`

```json
{
  "event": "gap_detected",
  "symbol": "EURUSD",
  "from_ts": "2026-08-19T10:00:00+00:00",
  "to_ts": "2026-08-19T10:00:12+00:00",
  "expected_cadence_seconds": 0.5
}
```

`expected_cadence_seconds` = the provider/symbol cadence used for the
threshold (`max(3 × cadence, SYNC_GAP_MIN_SECONDS)`, spec §5.5).

### 6.5 `backfill_progress`

```json
{
  "event": "backfill_progress",
  "symbol": "EURUSD",
  "pages_done": 34,
  "pages_total": 3400,
  "oldest_ts": "2026-08-19T09:59:06+00:00"
}
```

`oldest_ts` = the oldest tick committed so far (the advancing watermark).
Emitted per page (spec §5.4).

### 6.6 `provider_switched`

```json
{
  "event": "provider_switched",
  "old_provider": "mock",
  "new_provider": "pepperstone"
}
```

Engine-wide (no symbol — spec §5.4 `ProviderSwitched (old_provider,
new_provider)`). Detected via `TradingProviderFactory.get_active()` identity
polling on the engine heartbeat or an explicit signal from the switch
endpoint (spec §6.5). Consumers react with per-symbol `RESET` →
`SYNCING_HISTORY` (§5.2).

---

## 7. `/api/sync/*` REST contracts (spec §7.2 Phase 4 — api-architect)

All endpoints: **JWT-protected** (`IsAuthenticated` — spec §0 "DRF JWT +
`IsAuthenticated`"; mirror broker/views.py). Response envelope
`{status, message, data, errors}` (spec §0; broker/views.py lines 22–23;
ticks_views.py line 16). `status` ∈ `success` | `error`. Timestamps
ISO-8601 UTC.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/sync/status/` | IsAuthenticated | Engine + per-symbol window status (spec §7.2 "status, window stats") |
| GET | `/api/sync/ticks/` | IsAuthenticated | Query the engine window (spec §7.2 "query") |
| POST | `/api/sync/backfill/` | IsAuthenticated + staff | Trigger historical backfill |
| (SSE) | `/api/sync/stream/` | **Not defined here** | Spec §7.2 "SSE subscribe — proposed, not yet specified"; the §6 events are its substrate |

### 7.1 `GET /api/sync/status/`

Response `200`:

```json
{
  "status": "success",
  "message": "Sync engine status",
  "data": {
    "engine_running": true,
    "active_provider": "pepperstone",
    "mode": "demo",
    "window": {
      "max_ticks": 3400000,
      "max_age_days": 60,
      "segment_capacity": 100000,
      "record_layout": "full"
    },
    "symbols": {
      "EURUSD": {
        "state": "live",
        "count": 3400000,
        "oldest_ts": "2026-06-20T10:00:00+00:00",
        "newest_ts": "2026-08-19T10:00:00+00:00",
        "segment_count": 34,
        "evictions": 12,
        "duplicates": 3
      }
    }
  },
  "errors": null
}
```

Field provenance: `engine_running` = engine instance active; `active_provider`
/ `mode` from `TradingConfig`/factory (config.py); `window.*` from
`SyncConfig` (spec §3.3/§3.4/§1.3); `symbols.*` = `engine.state` +
`engine.stats()` per symbol (spec §5.2/§5.5). Never crashes when the engine
is off or the provider is unresolvable — mirror `provider_status`
(broker/views.py lines 171–224): return the envelope with
`engine_running: false` and/or the provider error in the error channel.

### 7.2 `GET /api/sync/ticks/`

Query params:

| Param | Type | Required | Default | Rules |
|---|---|---|---|---|
| `symbol` | str | **yes** | — | `^[A-Z0-9_]{1,20}$` (SYMBOL_PATTERN, ticks_views.py:46) |
| `start` | epoch ms \| ISO-8601 | no | oldest in window | must parse per `_parse_timestamp` rules (ticks_views.py:85–118) |
| `end` | epoch ms \| ISO-8601 | no | newest in window | must parse; `end >= start` |
| `limit` | int | no | 500 | clamped 1..2000 (mirror `CHART_DATA_LIMIT_DEFAULT`/`MAX`, ticks_views.py:59–60) |

Success `200`:

```json
{
  "status": "success",
  "message": "Sync window ticks",
  "data": {
    "symbol": "EURUSD",
    "start": "2026-08-19T09:00:00+00:00",
    "end": "2026-08-19T10:00:00+00:00",
    "limit": 500,
    "returned": 500,
    "ticks": [
      {
        "provider": "pepperstone",
        "symbol": "EURUSD",
        "bid": 1.08501,
        "ask": 1.08512,
        "last": 1.08501,
        "timestamp": "2026-08-19T09:00:00.123000+00:00",
        "volume": 0.0
      }
    ]
  },
  "errors": null
}
```

`ticks[]` fields = `MarketTick` field names verbatim (models.py:202–216);
chronological order (§1.2).

### 7.3 `POST /api/sync/backfill/` (staff-only)

Request body:

```json
{
  "symbol": "EURUSD",
  "days": 60
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| `symbol` | str | **yes** | `^[A-Z0-9_]{1,20}$` |
| `days` | int | no | default 60 (= `SYNC_WINDOW_MAX_AGE_DAYS`, spec §1.3); range 1..60 (backfilling beyond the window is pointless — evicted immediately, §4.2 I4) |

Semantics: invokes `engine.backfill(symbol, days)` (spec §5.3) for the
active provider; progress observable via `backfill_progress` events (§6.5).

Success `202 Accepted`:

```json
{
  "status": "success",
  "message": "Backfill accepted for EURUSD (60 days)",
  "data": {
    "symbol": "EURUSD",
    "days": 60,
    "state": "backfilling",
    "pages_done": 0,
    "pages_total": 3400,
    "ticks_committed": 0,
    "oldest_ts": null,
    "newest_ts": null
  },
  "errors": null
}
```

`data` mirrors `BackfillResult` (§5.1). Staff gate mirrors `provider_switch`
(broker/views.py lines 465–476).

### 7.4 Error contract (reuses `BrokerErrorCode` taxonomy, spec §0)

`BrokerErrorCode` (errors.py) is reused **verbatim** for every provider
failure, mapped to HTTP exactly like `_broker_error_response`
(broker/views.py lines 94–116): `BROKER_UNAVAILABLE` / `NETWORK_ERROR` →
HTTP 503; other broker codes → HTTP 200 with the error envelope carrying
`error_code` + `provider`.

The sync layer adds **three** codes (spec §2: "SyncError taxonomy (reuses
BrokerErrorCode where applicable)" — additive only where the broker taxonomy
cannot express the failure):

```python
class SyncErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"          # HTTP 400 — param/body validation
    ENGINE_NOT_RUNNING = "engine_not_running"    # HTTP 503 — engine absent/stopped
    BACKFILL_IN_PROGRESS = "backfill_in_progress"  # HTTP 409 — concurrent backfill
```

| Condition | HTTP | Envelope `error_code` |
|---|---|---|
| Bad `symbol` / `start` / `end` / `limit` / `days` | 400 | `invalid_request` (mirror provider_candles 400s, views.py:363–374 — these omit `error_code` today; the sync layer includes it) |
| Engine not running / stopped | 503 | `engine_not_running` |
| Backfill already active for the symbol (`BACKFILLING`/`SYNCING_HISTORY`) | 409 | `backfill_in_progress` |
| Unknown symbol at the provider | 200 | `symbol_not_found` (broker mapping) |
| Provider rate limit | 200 | `rate_limited` (broker mapping) |
| Provider unreachable / network | 503 | `broker_unavailable` / `network_error` (broker mapping) |
| Token expired / auth | 200 | `token_expired` / `authentication_error` (broker mapping) |
| Not staff (`POST backfill`) | 403 | `forbidden` (mirror provider_switch, views.py:465–476 — no `error_code` in the existing 403; sync keeps the same shape) |
| Unexpected | 500 | `internal_error` (mirror `_internal_error_response`, views.py:119–135) |

---

## 8. Provider adapter contract additions (spec §7.1 — per provider)

Summary of what each provider must expose for the engine (all additive,
none breaking):

### 8.1 Pepperstone (`providers/pepperstone.py`)

- **Query-worker history extension**: add a `ticks_range` query type to
  `forex.pepperstone.query_worker` (one-shot subprocess, params
  `{symbol, start_epoch_ms, end_epoch_ms, limit}`); implement
  `get_ticks_range` via `_run_query` (pepperstone.py:215–306). Until the
  worker ships, `supports_ticks_range = False` → planner fallback to
  `get_ticks(limit)` (spec §5.6, §7.1 "query worker history extension").
- The subprocess cost (~1–2 s/call) is the planner's rate-limiting input
  (spec §7.1); recovery tolerates slow backfill.
- Live streaming stays the legacy `TicksEngine`'s job —
  `subscribe_to_market_data` remains in-process-only (spec §11.4).

### 8.2 Mock (`providers/mock.py`)

- `supports_ticks_range = True`; deterministic synthetic history generator
  (seeded per `(symbol, start, end, limit)`, 1 tick/s cadence, oldest-first,
  ≤ `min(limit, 1000)` ticks) — the primary integration-test vehicle (spec
  §9.3). Bounded by the 1000-tick buffer cap (spec §5.6/§7.1); partial
  windows are legal — the engine tolerates unfilled gaps (spec §5.6).
- Honors `set_fail_mode` via `_check_fail_mode` (mock.py:281–300) so
  GAP/BACKFILLING paths are exercisable (spec §9.3).
- `seq` always `-1` → dedup via `(ts_ms, float_bits(last))` (§4.2).

---

## 9. Explicit assumptions & deviations from spec §5 (flag for CTO)

1. **`get_ticks_range` ordering**: spec §5.6 proposed "newest-first"; this
   contract fixes **chronological (oldest-first)** with the caller-driven
   `start = last + 1µs` advance (CEO-approved contract shape). The spec's
   newest-first *walk* remains expressible by shrinking `end` (§2.1).
2. **`get_ticks_range` fallback**: spec §5.6 proposed a default
   implementation falling back to `get_ticks(limit)`. This contract rejects
   a silent in-method fallback (it cannot honor `[start, end]` and would
   corrupt backfill watermarks) — the graceful path is the capability flag
   `supports_ticks_range` + a `NotImplementedError` backstop + planner-level
   fallback (§2.4).
3. **`TickNormalizer` input**: spec §5.1's `normalize(tick: MarketTick)`
   is refined to `normalize(raw: Any, provider: str, symbol: str) ->
   MarketTick | None` so provider-native payloads are normalized directly
   (§3); spec's `NormalizedTick` is folded into canonical `MarketTick`
   (models.py). `dedup_key` gains the `raw` parameter because `MarketTick`
   carries no `seq` field.
4. **`SegmentStore` is per-symbol**: spec §5.2's `symbol` parameters are
   dropped; the engine owns `dict[symbol → SegmentStore]` (§4).
5. **`SyncEngine` read surface**: per CEO contract shape, `snapshot()`,
   `query_range(start, end)`, `stats()` and `state` omit the symbol
   parameter; `query_range` accepts an optional trailing `symbol`, and
   `snapshot`/`stats`/`state` return per-symbol maps (§5).
6. **`subscribe(callback)` is engine-wide** (spec §5.3 was per-symbol);
   events carry `symbol` so per-symbol filtering is caller-side (§5).
7. **`/api/sync/ticks/` limit default/max (500/2000)** mirrors the existing
   chart-data contract (ticks_views.py:59–60) — not invented.
8. **SSE/WS subscribe endpoint is deliberately NOT specified** (spec §7.2
   defers it; the §6 events are its substrate).
9. **Memory-budget / eviction** numbers (3.4M, 60 days, K=100k, 1000-cap)
   are spec constants (spec §1.3/§3.4/§5.6), not api-architect inventions.

---

## 10. Contract verification hooks (for qa-engineer, spec §9)

- **Property-based**: §4.2 invariants I1–I7 (spec §9.1.1–9.1.6), §5.2
  transition legality (§9.1.5), paging termination (advancing `start` always
  terminates; partial pages stop the loop).
- **Unit**: normalizer drop rules (§3), storage append/late/dup/evict
  (§4), engine batching + event ordering (spec §9.2), publisher error
  isolation (§9.2).
- **Integration**: Mock `set_fail_mode("network")` mid-stream →
  GAP → BACKFILLING → LIVE; boundary-duplicate injection; late-tick
  injection; `factory.switch("mock"→"pepperstone"→"mock")` → RESET (spec §9.3).
- **API**: `/api/sync/*` contract tests against the §7 schemas; error-code
  mapping table §7.4.

---

## CHANGED FILES

- `njira_fx_api/docs/architecture/tick-sync-contracts.md` — created (this contract document)
