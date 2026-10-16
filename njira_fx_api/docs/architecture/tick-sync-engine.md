# Tick Synchronization Engine — Architecture Specification

| | |
|---|---|
| **Status** | APPROVED FOR IMPLEMENTATION (design only — no production code in this document) |
| **Owner** | CTO / Principal Software Architect |
| **Decision record** | ADR `dec-20260819-fc8319` — "Broker-independent, timestamp-driven rolling tick synchronization engine" (user-approved) |
| **Date** | 2026-08-19 |
| **Applies to** | `njira_fx_api` (Django 5.2 + DRF, Python 3.12, venv `njira_fx_api/myvenv`) |
| **Consumers** | `backend-dev`, `database-engineer`, `async-engineer`, `qa-engineer`, `api-architect`, `tech-lead` |

> **ADR note**: the decision record `dec-20260819-fc8319` was approved in the
> builder/CEO session but is not yet persisted to the Memory Fabric on disk
> (`.tmp/memory/fabric/` does not exist in this repo checkout). The builder
> should persist it via `decision record` when the fabric router is available.
> This spec treats the ADR as the binding requirement source.

---

## 0. Source-of-truth files (verified in repo — do not re-derive)

| File | Role in this design |
|---|---|
| `forex/broker/base.py` | `TradingProvider` ABC — the provider contract the engine consumes (`get_ticks`, `get_market_data`, `subscribe_to_market_data`, `health`, `get_connection_status`, `disconnect`) |
| `forex/broker/models.py` | Normalized value objects — `MarketTick(provider, symbol, bid, ask, last, timestamp, volume=0.0)`, `MarketCandle`, `ProviderName`, `TradingMode` |
| `forex/broker/factory.py` | `TradingProviderFactory` — `create/get_active/reset/switch`; the engine's only entry point to providers |
| `forex/broker/config.py` | `TradingConfig.from_env()` — `TRADING_PROVIDER` (default `mock`), `TRADING_MODE` (demo\|paper\|live) |
| `forex/broker/errors.py` | `BrokerError` + `BrokerErrorCode` taxonomy — reused by the sync layer |
| `forex/broker/health.py` | `ProviderHealthMonitor` / `ProviderHealth` — engine health telemetry hooks |
| `forex/broker/providers/{mock,pepperstone}.py` | The two provider adapters (see §7 for per-provider integration) |
| `forex/broker/views.py` + `urls.py` | Existing `/api/provider/*` endpoints (envelope `{status, message, data, errors}`) |
| `forex/ticks_views.py` | Existing `/api/ticks/`, `/api/chart-data/`, `/api/mtf-status/`, `/api/trade/` endpoints |
| `forex/clickhouse/connection.py` + `schema.py` | ClickHouse client + `forex_ticks` table (`ReplacingMergeTree ORDER BY (symbol, timestamp)`); **verified `ACCESS_DENIED` (error 497) on the `admin` user — schema creation blocked** |
| `forex/management/commands/ticks_engine.py` | Existing provider-independent streaming command (the engine's sibling/ancestor) |
| `forex/pepperstone/ticks_engine.py` | Legacy Pepperstone streaming engine (reactor in main thread, batched ClickHouse sink, exponential-backoff reconnect) |
| `forex/pepperstone/query_worker.py` | One-shot subprocess query worker (Pepperstone query plane) |
| `forex/settings.py` | Django settings — SQLite default DB, DRF JWT + `IsAuthenticated`, user throttle 100/min |
| `requirements.txt` | `numpy==2.2.1` present; `clickhouse-connect==0.8.11`; `websocket-client==1.8.0`; **no redis, no channels/daphne** |
| `njira_fx_ui/src/lib/api.ts` | Frontend consumes `/api/chart-data/` (polling), `/api/ticks/`, `/api/provider/candles/`, `/api/provider/market-data/` |

---

## 1. Context and requirements (from ADR `dec-20260819-fc8319`)

The system must maintain a **continuously correct, chronologically ordered
rolling window of ~3.4M ticks (60-day window) per symbol**, independent of
which broker provider is active, despite four classes of data pathology:

1. **Late / out-of-order arrivals** — `T1→T2→T4→T3` is possible; the
   **timestamp** determines logical position, never arrival order.
2. **Duplicates** — occur on reconnects and at historical/live sync boundaries.
3. **Gaps** — connection loss means `T3–T5` may be missing; the engine must
   detect and recover.
4. **Broker availability** — Pepperstone token may be unavailable for days;
    Mock always works. The app must run on any provider via the
    `TRADING_PROVIDER` switch **without rewriting the engine**.

### 1.1 Pipeline (binding)

```
Provider Adapter → Tick Normalizer → Synchronization Engine
  (Validate → Dedup → Gap Detect → Timestamp Ordering → Reconcile → Commit → Publish)
  → 3.4M Rolling Window → consumers (Strategy, Charts, Frontend)
```

### 1.2 Critical invariant (binding)

```
timestamp(T0) <= timestamp(T1) <= ... <= timestamp(T3,399,999)
```

The window is **logically ordered by timestamp at all times**. Physical
storage location is an implementation detail — a HEAD pointer alone is
insufficient because late ticks can land anywhere in the window.

### 1.3 Window semantics (binding)

- Bounded by **both** `MAX_TICKS = 3,400,000` **and** `MAX_AGE = 60 days`;
  evict whichever boundary is hit first.
- Must support:
  - (a) cold start from **historical backfill** — paged 1000-tick windows
    from `now` backward;
  - (b) **seamless transition** from backfill to live subscription;
  - (c) **reconnect recovery** — remember last accepted timestamp → backfill
    missing → dedup → reconcile → resume live.

### 1.4 Efficiency constraint (binding)

- **No O(n) shifts of 3.4M records per tick.**
- Locate segment: **O(log n)**.
- Insert within segment: **O(K)** where K = segment capacity (e.g. 100k).
- Dedup via **binary search**.
- Eviction from the **oldest segment** (O(1) per segment).

---

## 2. Module boundaries — new package `forex/sync/`

The engine is a new bounded context that **consumes** the existing
`forex/broker/` abstraction and **emits** normalized `MarketTick`-derived
records. It never imports provider classes directly — only
`TradingProviderFactory` and the `TradingProvider` ABC.

```
forex/sync/
├── __init__.py            # public API: create_engine(), SyncEngine, TickNormalizer
├── config.py              # SyncConfig — env-driven tuning (window, cadence, rates)
├── errors.py              # SyncError taxonomy (reuses BrokerErrorCode where applicable)
├── normalizer.py          # TickNormalizer — provider → canonical tick record
├── storage.py             # SegmentStore — segmented numpy arrays + segment index
├── engine.py              # SyncEngine — validate→dedup→gap→order→reconcile→commit→publish
├── recovery.py            # RecoveryController — backfill orchestration + state machine
├── backfill.py            # BackfillPlanner — paged window planning per provider capability
├── publisher.py           # EventPublisher — in-process pub/sub (SSE/WS bridge later)
├── management/commands/sync_engine.py   # `python manage.py sync_engine`
└── tests/                 # unit + property-based tests (qa-engineer owns)
```

### 2.1 Ownership map (per AGENTS.md)

| Module | Owner | Scope |
|---|---|---|
| `normalizer.py`, `engine.py`, `storage.py`, `recovery.py`, `backfill.py`, `publisher.py`, `config.py`, `errors.py` | `backend-dev` (Senior) | Implementation of the interfaces below |
| Interface contracts (§5) | `cto` (this spec) + `tech-lead` | Module interfaces, task breakdown |
| Any new HTTP/WS API contracts (`/api/sync/*`) | `api-architect` | OpenAPI/DTOs — **not defined here** |
| Persistence/snapshot schema (if adopted, §4.4) | `database-engineer` | Migrations, snapshot layout |
| Backfill scheduling if moved to queues/cron | `async-engineer` | Queue jobs, retries |
| Tests (unit, property, integration) | `qa-engineer` | §9 |
| Load/throughput benchmarks | `perf-tester` | §8 targets |
| Docs (this spec + ADR persistence) | `documentation-writer` | ADR `dec-20260819-fc8319` persistence |

### 2.2 Dependency direction (strict)

```
forex.sync ──uses──▶ forex.broker (TradingProvider, TradingProviderFactory,
                    MarketTick, BrokerError, BrokerErrorCode, ProviderHealth)
forex.sync ──uses──▶ numpy (storage only)
forex.sync ──optional──▶ forex.clickhouse (archival sink, best-effort)
forex.sync ──NEVER──▶ forex.pepperstone.* / provider internals
```

Provider-specific integration details (e.g. REST/OTP flows, 1000-tick
paging caps, symbol-list endpoints) are **confined to the individual provider
adapter** (e.g. `forex/broker/providers/pepperstone.py`). The engine is
untouched by them — this is the primary payoff of the broker abstraction.

---

## 3. Storage structure decision

### 3.1 Options evaluated

| Option | Insert (live) | Late-tick insert | Eviction | Memory @3.4M/symbol | Persistence | Ops complexity | Verdict |
|---|---|---|---|---|---|---|---|
| (a) In-memory segmented sorted arrays (numpy) + segment index | O(1) amortized (pre-allocated fill pointer) | O(K) memmove in one segment | O(1) drop oldest segment | **~82–136 MB** (24–40 B/tick) | Snapshot optional (numpy `.npy` per segment) | None (in-process) | ✅ **CHOSEN** |
| (b) Circular buffer of fixed-size segments | O(1) | O(K) | O(1) overwrite | same as (a) | none | Ring wrap complicates dual-boundary eviction + late-tick insertion into a segment that may be overwritten | ❌ Subsumed by (a) with a `deque` of segments |
| (c) SQLite + timestamp index | O(log n) B-tree insert; ~10–50k rows/s with WAL batching | O(log n) (normal insert) | `DELETE WHERE ts < X` — O(n) worst case per batch | n/a (disk ~300–500 MB + index) | Free | Low; but shares the Django SQLite file or needs a second file | ⚠️ Fallback/persistence companion only |
| (d) Redis sorted sets | O(log n) ZADD | O(log n) | O(log n + m) ZREMRANGEBYSCORE | ~340 MB+ (zset overhead ~100 B/member) | Depends on AOF/RDB | **New infra — redis not in `requirements.txt`** | ❌ Rejected |
| (e) ClickHouse | Batch-only (per-tick inserts inefficient) | Fine (MergeTree) | TTL / DELETE | n/a (server-side) | Free | High; **verified `ACCESS_DENIED` (error 497) on `admin` — schema creation blocked today** | ❌ Rejected as primary; keep as optional archival sink |

### 3.2 Decision: (a) in-memory segmented sorted arrays (numpy) with segment index

Rationale:

1. **The window is a hot in-memory dataset.** Strategies and charts read it at
   tick frequency; a server round-trip (ClickHouse/Redis) adds latency and a
   hard operational dependency that is currently broken (ClickHouse auth).
2. **numpy 2.2.1 is already a pinned dependency** (`requirements.txt` line 43)
   — zero new infrastructure.
3. **The complexity budget is exactly met**: segment locate O(log n) via a
   sorted boundary index; insert O(K) via one memmove inside a single segment;
   dedup O(log K) via `searchsorted`; eviction O(1) by dropping the oldest
   segment from a `deque`; **no full-window shift ever**.
4. **Persistence is a nice-to-have, not a requirement**: the recovery state
   machine (§5.6) already cold-starts from historical backfill, so a process
   restart is a normal `SYNCING_HISTORY` event. Optional snapshot (§4.4)
   shortens cold start but is not on the critical path.
5. ClickHouse remains the **archival sink** (existing `insert_ticks_batch`
   path, best-effort) — the engine publishes committed ticks to it, but the
   engine never depends on it.

### 3.3 Record layout (canonical)

```python
TICK_DTYPE = np.dtype([
    ("ts",   "int64"),    # epoch milliseconds UTC — THE ordering key
    ("bid",  "float64"),
    ("ask",  "float64"),
    ("last", "float64"),
    ("seq",  "int64"),    # provider tick id/sequence; -1 when unavailable
])
```

- **FULL layout** (default): 40 bytes/tick → **136 MB @ 3.4M ticks/symbol**.
- **COMPACT layout** (`SYNC_RECORD_LAYOUT=compact`): `ts + last + seq` =
  24 bytes/tick → **82 MB @ 3.4M ticks/symbol** (bid/ask dropped; charts
  degrade to last-price only).

> **Assumption (explicit)**: the ADR's 55–80 MB budget assumed 16–24 B/tick
> (epoch + price + optional id). Consumers (charts, MTF strategy) need
> `bid`/`ask`, so the default FULL layout costs ~136 MB/symbol. This is
> acceptable for the target deployment (a small number of tracked symbols);
> COMPACT is the escape hatch. `SYNC_MAX_SYMBOLS` (default 4) caps total
> memory at ~544 MB FULL.

### 3.4 Segment structure and index

- **Segment**: a pre-allocated numpy structured array of capacity
  `K = SYNC_SEGMENT_CAPACITY` (default **100,000** ticks) with a fill counter.
  Appends write at `arr[fill]` and increment — **O(1) amortized, no copies**.
- **Segment index**: a `collections.deque` of segment descriptors
  `(first_ts, last_ts, count, capacity, array_ref)`, kept sorted by time and
  non-overlapping. The boundary arrays (`first_ts` per segment) are cached in
  a numpy array for `bisect` — **O(log S)** locate, S ≈ 34 segments.
- **3.4M ticks = 34 segments × 100k** — the count boundary aligns exactly
  with segment granularity.

### 3.5 Complexity table (binding)

| Operation | Complexity | Mechanism |
|---|---|---|
| Locate segment for `ts` | O(log S) | `bisect` over segment `first_ts` boundaries |
| Append (live tick, newest) | O(1) amortized | fill-pointer write into active segment |
| Late-tick insert | O(K) | `searchsorted` + one memmove within one segment; split when full |
| Dedup | O(log K) | `searchsorted` on `ts`, then scan equal-`ts` neighbors for `seq`/price match |
| Query range | O(m + S) | slice filled portions of ≤ S segments, concatenate |
| Evict oldest | O(1) per segment | `popleft()` on the segment deque |
| Snapshot | O(n) copy (rare) | immutable view publication (§6) |

---

## 4. Storage design details

### 4.1 Insert paths

**Append path (live, `ts >= active.last_ts`)** — the 99.9% case:

```
if active_segment.fill == K: seal(active_segment); active_segment = new_segment()
# tail dedup: duplicates at the live boundary are the only realistic case
if tail_contains(active_segment, dedup_key(tick)): return DUPLICATE
active_segment.arr[fill] = record; fill += 1
```

**Late-tick path (`ts < active.last_ts`)**:

```
seg = locate_segment(ts)                      # O(log S)
if seg is None: return OUT_OF_WINDOW          # older than oldest segment → drop
if contains(seg, dedup_key(tick)): return DUPLICATE   # O(log K)
idx = np.searchsorted(seg.arr[:seg.fill], ts, side="left")
if seg.fill == K:
    seg, new_seg = split_segment(seg)         # O(K): move upper half to a new segment
    insert into the half that contains ts
else:
    seg.arr[idx+1:fill+1] = seg.arr[idx:fill] # O(K) memmove (~4 MB @ 40 B × 100k)
    seg.arr[idx] = record; fill += 1
```

**Split invariant**: after any insert, every segment has `count <= K` and
segments remain sorted, non-overlapping, and contiguous in time.

### 4.2 Dedup key

- When the provider supplies a tick id/sequence (`seq != -1`): key =
  `(ts_ms, seq)` — exact.
- Otherwise: key = `(ts_ms, float_bits(last))` where `float_bits` is the
  IEEE-754 bit pattern (`struct.pack(">d", last)` → int) — exact float
  comparison, no epsilon games.
- Duplicates are **counted and logged** (`SYNC_DUP_LOG_RATE`) but never
  stored twice.

### 4.3 Eviction policy (dual boundary)

```
after each commit batch:
    while total_count > MAX_TICKS:            # 3,400,000
        drop oldest segment                    # O(1)
    while (now - oldest_segment.last_ts) > MAX_AGE:   # 60 days
        drop oldest segment                    # O(1)
    publish WindowAdvanced(evicted_count, oldest_ts, newest_ts)
```

- **Whichever boundary is hit first wins** (ADR §1.3).
- Eviction is **segment-granular**: a segment is dropped when its *newest*
  tick violates a boundary. The count boundary is exact at 34 × 100k; the age
  boundary is exact to within one segment's span (≤ a few minutes at 1 tick/s).
  State this in `stats()`.

### 4.4 Optional persistence (Phase 3+, not on critical path)

- Cold-start warm-up snapshot: per-symbol directory of sealed segments as
  numpy `.npy` files + a JSON index (`first_ts`, `last_ts`, `count`).
- `database-engineer` owns the layout; `SyncConfig.SYNC_SNAPSHOT_DIR` enables.
- The engine treats a missing/stale snapshot as a normal `SYNCING_HISTORY`
  event — persistence is an optimization, never a correctness dependency.

---

## 5. Contracts (provider-neutral)

### 5.1 `TickNormalizer`

```python
class TickNormalizer(Protocol):
    """Provider → canonical tick record. Pure, stateless, thread-safe."""

    def normalize(self, tick: MarketTick) -> NormalizedTick | None:
        """Validate and canonicalize a MarketTick.

        Rejects (returns None, logs reason):
          - non-finite or non-positive prices; bid > ask (epsilon 1e-9)
          - timestamp outside [SYNC_MIN_TS, now + SYNC_MAX_FUTURE_SKEW]
            (default skew 5 min — mirrors ticks_views.MAX_TIMESTAMP_SKEW)
          - symbol failing ^[A-Z0-9_]{1,20}$ (mirrors SYMBOL_PATTERN)
        Converts timestamp → epoch ms UTC int64; derives seq from the
        provider tick id when present, else -1.
        """

    def dedup_key(self, tick: NormalizedTick) -> tuple[int, int]:
        """(ts_ms, seq) when seq != -1 else (ts_ms, float_bits(last))."""
```

### 5.2 `SegmentStore`

```python
class SegmentStore(Protocol):
    """Owns the segmented arrays. Single-writer; readers via shared lock."""

    def insert(self, symbol: str, ticks: Sequence[NormalizedTick]) -> InsertResult:
        """Dedup + order + commit. Returns InsertResult(inserted, duplicates,
        out_of_window, oldest_ts, newest_ts, total_count)."""

    def query_range(self, symbol: str, start: datetime, end: datetime,
                    limit: int | None = None) -> list[NormalizedTick]:
        """Chronological slice. limit applies to the NEWEST end."""

    def snapshot(self, symbol: str) -> WindowSnapshot:
        """Immutable view: (tuple of sealed segment arrays, index, metadata)."""

    def stats(self, symbol: str) -> WindowStats:
        """count, oldest_ts, newest_ts, segment_count, evictions, duplicates."""

    def evict(self, symbol: str, max_ticks: int, max_age: timedelta) -> EvictionResult:
        """Enforce dual boundary (§4.3)."""
```

### 5.3 `SyncEngine`

```python
class SyncEngine(Protocol):
    """The pipeline: validate → dedup → gap detect → order → reconcile →
    commit → publish. One engine instance per process; one writer thread."""

    def ingest_tick(self, tick: MarketTick) -> IngestResult:
        """Entry point for live ticks (also used by tests)."""

    def backfill(self, symbol: str, start: datetime | None = None,
                 end: datetime | None = None) -> BackfillResult:
        """Paged historical fill (default: last_accepted_ts → now, or
        now - MAX_AGE → now on cold start)."""

    def subscribe(self, symbol: str, callback: Callable[[SyncEvent], None]) -> str:
        """Publish-subscribe for committed ticks and lifecycle events."""

    def snapshot(self, symbol: str) -> WindowSnapshot: ...
    def query_range(self, symbol: str, start: datetime, end: datetime,
                    limit: int | None = None) -> list[NormalizedTick]: ...
    def stats(self) -> dict[str, WindowStats]: ...
    def state(self, symbol: str) -> SyncState: ...
    def start(self) -> None: ...   # bootstrap: resolve provider, subscribe, backfill
    def stop(self) -> None: ...    # drain, disconnect provider, flush sink
```

### 5.4 Publish events (`SyncEvent`)

| Event | Payload | Emitted when |
|---|---|---|
| `TickCommitted` | `(symbol, NormalizedTick)` | a tick is committed to the window |
| `WindowAdvanced` | `(symbol, evicted_count, oldest_ts, newest_ts, total_count)` | after eviction check |
| `StateChanged` | `(symbol, old_state, new_state, reason)` | any state transition |
| `GapDetected` | `(symbol, from_ts, to_ts, expected_cadence)` | live stream stalls |
| `BackfillProgress` | `(symbol, pages_done, pages_total, oldest_ts)` | during backfill |
| `ProviderSwitched` | `(old_provider, new_provider)` | factory switch detected |

### 5.5 Recovery state machine

```
INIT ──▶ SYNCING_HISTORY ──▶ LIVE ◀──▶ GAP ──▶ BACKFILLING ──▶ LIVE
              ▲                  │                    │
              └──────────────────┴── (backfill failed → retry w/ backoff) ──┘
LIVE ──(provider switch)──▶ RESET ──▶ SYNCING_HISTORY
LIVE ──(reconnect)──▶ GAP ──▶ BACKFILLING(from last_accepted_ts) ──▶ LIVE
```

- **`SYNCING_HISTORY`**: backfill pages commit; live ticks arriving meanwhile
  are buffered (bounded, `SYNC_HISTORY_BUFFER` default 10k) and reconciled
  after the backfill watermark passes them.
- **`LIVE`**: normal operation; gap timer armed per symbol.
- **`GAP`**: `now - last_accepted_ts > gap_threshold` (default
  `max(3 × cadence, SYNC_GAP_MIN_SECONDS=5)`).
- **`BACKFILLING`**: paged fill from `last_accepted_ts` → `now`; dedup handles
  the overlap with already-committed ticks; on completion verify continuity
  (walk segment boundaries; any residual hole > threshold → re-plan).
- **`RESET`**: provider switch — per-symbol state cleared, window retained or
  dropped per `SYNC_KEEP_WINDOW_ON_SWITCH` (default `true`: the window is
  provider-agnostic and survives; only the live stream restarts).

### 5.6 Backfill planner

```
BackfillPlanner(provider, symbol, start, end):
    # Provider capability: paged windows up to the per-request cap (e.g. 1000
    # ticks/request). Walk NEWEST-first: page = (start=end - window, end),
    # window sized so each page returns ≤ cap (default 1000 ticks ≈ 1000 s at
    # 1 tick/s). Historical data is retrievable via paged start/end windows
    # when the provider advertises supports_ticks_range.
    # Pepperstone: falls back to provider.get_ticks(limit) (newest-first,
    # subprocess per call — slow, rate-limited).
    # Mock: bounded by the provider's 1000-tick buffer — best-effort partial
    # window; the engine tolerates it (gaps simply remain unfilled).
    yield pages until start watermark reached or provider returns empty
```

**Proposed additive provider contract** (owned by `api-architect` +
`backend-dev`; additive, does not break existing providers):

```python
# On TradingProvider (base.py) — OPTIONAL extension:
def get_ticks_range(self, symbol: str, start: datetime, end: datetime,
                    limit: int = 1000) -> list[MarketTick]:
    """Paged tick history in [start, end], newest-first. Default
     implementation falls back to get_ticks(limit) so existing providers
     work unchanged. Providers that advertise `supports_ticks_range` implement
     it natively with paged windows."""
```

---

## 6. Concurrency model

### 6.1 Single-writer principle (binding)

- **One writer thread** (the engine thread) owns `SegmentStore` mutations.
- All ingestion funnels through a **bounded queue**
  (`SYNC_INGEST_QUEUE_SIZE`, default 100,000):
  - Provider subscription listener thread → `queue.put(normalized_tick)`
  - Mock tick-loop thread → `queue.put(...)`
  - Legacy Pepperstone `TicksEngine._on_message` → `queue.put(...)`
  - Backfill worker thread(s) → `queue.put(...)` (pages normalized first)
- Provider callback threads **never touch storage** — they only `put()`.
  This mirrors the existing `_StreamingTickSink` pattern in
  `forex/management/commands/ticks_engine.py`.

### 6.2 Commit batching

- The engine thread drains the queue in batches
  (`SYNC_COMMIT_BATCH_SIZE` default 1000, or `SYNC_COMMIT_FLUSH_MS` default
  10 ms, whichever first) and commits under a **shared reader lock**
  (`threading.RLock`): writer takes exclusive, readers take shared.
- Writer hold time per batch: < 1 ms (numpy memmove + index update).

### 6.3 Readers

- `query_range` / `snapshot` / `stats` acquire the shared lock — safe against
  concurrent commits.
- **Hot-path optimization (Phase 3)**: the engine publishes immutable
  `WindowSnapshot` views (sealed segments are immutable; the active segment is
  copied on snapshot). Strategy readers hold the view lock-free. Late-tick
  inserts into a segment referenced by a live snapshot trigger
  copy-on-write of that segment (rare — late ticks are the exception).

### 6.4 Backfill workers

- 1–2 worker threads fetch pages (rate-limited by a token bucket,
  `SYNC_BACKFILL_RATE` default 5 req/s, provider rate-limit aware) and enqueue
  normalized ticks. The engine thread remains the sole committer — the
  single-writer invariant holds.

### 6.5 Provider switching

- `TradingProviderFactory.switch()` disconnects the old provider and caches
  the new one. The engine polls `factory.get_active()` identity on its
  heartbeat (or receives an explicit `ProviderSwitched` signal from the
  switch endpoint) → per-symbol `RESET` → `SYNCING_HISTORY` with the new
  provider. No engine code changes per provider.

---

## 7. Integration points

### 7.1 Engine → providers (consumption)

| Provider | Live ticks | Historical backfill | Notes |
|---|---|---|---|
| **Pepperstone** | Legacy `forex.pepperstone.ticks_engine.TicksEngine` session callbacks wired into the engine's queue (the provider's `subscribe_to_market_data` is in-process-only — no reactor) | `get_ticks_range` default → `get_ticks(limit)` via one-shot `query_worker` subprocess (~1–2 s/call) — slow; planner rate-limits; recovery tolerates slow backfill | Token currently unavailable for days — engine must run on other providers meanwhile (it does: provider-neutral) |
| **Mock** | `subscribe_to_market_data` daemon thread (1 tick/symbol/0.5 s) → queue | `get_ticks` bounded by the 1000-tick internal buffer — best-effort partial window | Primary integration-test vehicle; fail modes (`set_fail_mode`) exercise GAP/BACKFILLING paths |

### 7.2 Consumers → engine window

| Consumer | Today | With engine (phased) |
|---|---|---|
| **Strategy** (`forex/mtf_strategy.py`, `mtf_aggregator` command) | ClickHouse `get_ohlc_bars` | Phase 3: `BarAggregator` over `engine.query_range` produces the same bar shape; flag `SYNC_SERVE_STRATEGY` |
| **Charts / frontend** (`/api/chart-data/`) | ClickHouse `get_recent_ticks` | Phase 2: re-point to `engine.query_range` behind `SYNC_SERVE_CHART_DATA`; response shape `{bid, ask, spread, time}` unchanged; fallback to ClickHouse |
| **Frontend live updates** | polling | Phase 4: `api-architect` defines `/api/sync/*` (status, window stats, query, SSE subscribe) — **proposed, not yet specified** |
| **Archival** | ClickHouse `insert_ticks_batch` | Engine publishes committed ticks to the same best-effort sink (unchanged behavior) |

### 7.3 API mapping (existing, unchanged)

- `GET /api/provider/market-data/` — unchanged (one-shot provider call).
- `GET /api/provider/candles/` — unchanged.
- `POST /api/ticks/` — unchanged (legacy ingest path remains for the
  Pepperstone HTTP fallback).
- `GET /api/chart-data/` — Phase 2 re-point (see above), same contract.
- New `/api/sync/*` endpoints are **proposals for `api-architect`** — this
  spec does not invent their contracts.

---

## 8. Performance targets (binding for `perf-tester`)

| Metric | Target | Mechanism |
|---|---|---|
| Live ingest throughput | **≥ 50k ticks/s sustained** (target 200k) | O(1) amortized fill-pointer append + tail dedup |
| Ingest → publish latency | **p95 < 5 ms** | commit batching ≤ 10 ms |
| Late-tick insert | **p95 < 1 ms** @ K=100k | O(K) memmove (~4 MB) |
| Memory per symbol | **≤ 136 MB FULL** (82 MB COMPACT) @ 3.4M | numpy structured arrays; `SYNC_MAX_SYMBOLS` cap |
| Backfill rate | **5–10 req/s** (provider rate-limit aware) | token bucket in `BackfillPlanner` |
| Full 60-day backfill (3.4M ticks, 3400 pages) | **≤ 15 min** | 5 req/s × 1000 ticks |
| Reconnect gap backfill (minutes of downtime) | **≤ 30 s** | a handful of pages |
| Eviction | **amortized O(1)/tick** | segment `popleft()` |
| `query_range` (1M ticks) | **< 10 ms** | slice ≤ 10 segments, concatenate |

---

## 9. Testing strategy (qa-engineer owns)

### 9.1 Property-based (hypothesis)

1. **Ordering invariant**: for any sequence of ticks with arbitrary arrival
   order (shuffled, reversed, interleaved), the stored window is always
   sorted by `ts` — `ts[i] <= ts[i+1]` for all i.
2. **Dedup**: injecting the same `(ts, seq)` (or `(ts, last_bits)`) any number
   of times never stores it twice; duplicates are counted.
3. **Gap detection**: a stall longer than the threshold always transitions
   `LIVE → GAP → BACKFILLING → LIVE`; a stall shorter than the threshold never
   does.
4. **Eviction boundaries**: (i) count boundary hit while age is fine → evicts
   oldest segments; (ii) age boundary hit while count is fine → evicts by age;
   (iii) both hit → whichever first; window size never exceeds either bound.
5. **Recovery transitions**: state machine reachability — every transition in
   §5.5 is reachable and every illegal transition is rejected.
6. **Segment invariants**: every segment `count <= K`; segments sorted,
   non-overlapping, time-contiguous; split preserves invariants.

### 9.2 Unit

- `normalizer`: timestamp coercion (epoch ms, ISO-8601, None), future-skew
  rejection, non-finite prices, symbol allowlist.
- `storage`: append/late-insert/duplicate/out-of-window/evict/query_range.
- `engine`: state machine, commit batching, publish event ordering.
- `publisher`: subscriber registration, event fan-out, error isolation.

### 9.3 Integration (MockProvider as the deterministic vehicle)

- `set_fail_mode("network")` mid-stream → `GAP` → recovery → `LIVE`.
- Duplicate injection at the historical/live sync boundary (backfill overlap).
- Late-tick injection after live ticks (T1→T2→T4→T3).
- Provider switch mid-stream (`factory.switch("mock"→"pepperstone"→"mock")`).
- Cold start: full backfill → seamless transition to live.

### 9.4 Verification commands

- `python manage.py sync_engine` (mock provider) — e2e smoke.
- `pytest forex/sync/tests/` (unit + property).
- `perf-tester`: ingest throughput benchmark + late-tick p95 benchmark.
- Existing gates: `security-engineer` → `qa-engineer` → `code-reviewer`.

---

## 10. Migration / rollout (incremental, non-breaking)

| Phase | Change | Risk | Rollback |
|---|---|---|---|
| **0** | Land `forex/sync/` package + tests; `SYNC_ENGINE_ENABLED=false` default | none (dead code) | delete package |
| **1** | `python manage.py sync_engine` runs alongside existing `ticks_engine`; both write to ClickHouse; engine maintains the in-memory window | low (parallel path) | stop the command |
| **2** | Re-point `/api/chart-data/` to `engine.query_range` behind `SYNC_SERVE_CHART_DATA=true`; fallback to ClickHouse on engine absence | low (same response shape) | flip flag off |
| **3** | `mtf_aggregator` reads engine window behind `SYNC_SERVE_STRATEGY=true` | medium (strategy inputs) | flip flag off |
| **4** | `api-architect` defines `/api/sync/*`; frontend live updates (SSE/WS — `async-engineer`/`observability-engineer`) | medium | feature-flag the new endpoints |

**Guarantees**: the existing dashboard, strategy, and `/api/provider/*`
endpoints keep working at every phase. The engine is additive; nothing is
rewritten in place. Provider-specific migrations ship independently inside
their provider adapter and are invisible to the engine.

---

## 11. Assumptions (explicit)

1. **Memory budget**: FULL layout at ~136 MB/symbol exceeds the ADR's 55–80 MB
   estimate because consumers need `bid`/`ask`; COMPACT layout (82 MB) is the
   escape hatch; `SYNC_MAX_SYMBOLS` caps total.
2. **Segment-granular eviction**: the age boundary is enforced per segment
   (a segment is dropped when its newest tick exceeds 60 days); the count
   boundary is exact at 34 × 100k.
3. **`get_ticks_range` is a proposed additive contract** on `TradingProvider`;
   until it lands, the planner falls back to `get_ticks(limit)`.
4. **Pepperstone live streaming** remains the legacy `TicksEngine`'s job; the
   sync engine consumes its session callbacks. The provider's
   `subscribe_to_market_data` is in-process-only by design (one-shot reactor).
5. **ClickHouse stays an optional archival sink** — never a correctness
   dependency (verified `ACCESS_DENIED` today).
6. **Persistence is optional** (snapshot warm-up); cold start is a normal
   `SYNCING_HISTORY` event.
7. **Single process, single engine instance** per deployment; multi-process
   (gunicorn workers) deployments run the engine in exactly one worker or a
   dedicated process (management command) — the window is not shared across
   processes in this phase.

## 12. Open items (escalate to builder)

- Persist ADR `dec-20260819-fc8319` to the Memory Fabric (router unavailable
  in this session).
- `api-architect` to confirm the `get_ticks_range` contract shape and the
  Phase-4 `/api/sync/*` surface.
- `database-engineer` to confirm the snapshot layout if Phase-3 persistence is
  adopted.