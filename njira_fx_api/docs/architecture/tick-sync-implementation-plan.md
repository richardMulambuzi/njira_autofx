> STATUS (2026-08-24): Superseded — the Deriv provider was removed from the codebase; only pepperstone and mock providers exist.

# Tick Synchronization Engine — Implementation Plan (Tech-Lead Task Breakdown)

| | |
|---|---|
| **Status** | APPROVED FOR IMPLEMENTATION (task breakdown — no production code in this document) |
| **Owner** | `tech-lead` (Development Manager) |
| **Governing spec** | `docs/architecture/tick-sync-engine.md` (628 lines, ADR `dec-20260819-fc8319`) — all section citations below refer to it |
| **Date** | 2026-08-19 |
| **Squad** | `backend-dev` (Senior), `database-engineer` (Senior), `performance-engineer` (Senior); `async-engineer` + `qa-engineer` implement T6/T9 from the specs in this plan |
| **External dependency** | `api-architect` — `njira_fx_api/docs/architecture/tick-sync-contracts.md` does **NOT exist yet**. T7 is BLOCKED on it. T2 proceeds on the spec §5.6 baseline shape and reconciles when the contract lands (additive, low risk). |

---

## 0. Scope guardrails (binding)

1. **Priority is the sync engine** (§1–§6). `/api/sync/*` (T7) is additive Phase-4 work and MUST NOT alter existing `/api/provider/*`, `/api/ticks/`, `/api/chart-data/` behavior (§7.3, §10 guarantees).
2. **Deriv NEW-API migration is confined to `forex/broker/providers/deriv.py`** (§2.2) — the engine never imports provider internals; it consumes only `TradingProviderFactory` + `TradingProvider` ABC (§2).
3. **No new infrastructure**: numpy 2.2.1 is pinned (`requirements.txt:43`); no redis, no ClickHouse dependency for correctness (§3.2, §11.5).
4. **No invented contracts**: every interface below is the spec §5 baseline. Where api-architect's contract differs, the delta is reconciled in T2/T7, never silently re-specified here.
5. **Rollout is incremental** (§10): `SYNC_ENGINE_ENABLED=false` default (Phase 0) — the package lands as dead code until the flag flips.

---

## 1. Task graph (waves)

```
WAVE 0 (parallel, no deps)          WAVE 1                WAVE 2          WAVE 3              WAVE 4
────────────────────────────        ──────────────        ──────────      ──────────────      ──────────────
T1 Deriv migration (backend-dev) ──▶ T2 get_ticks_range ─┐                ┌─▶ T6 Recovery+BF (async-engineer)
T3 sync scaffold + normalizer ─────▶ T4 SegmentStore ────┼─▶ T5 Engine ──┤
T8 Mock range + PS query worker     T10 snapshot (db,opt)┘   (backend-dev)└─▶ T11 perf pass (performance-engineer)
                                                                              T7 /api/sync/* (BLOCKED: api-architect)
                                                                              T9 tests (qa-engineer)
```

**Critical path (Deriv end-to-end):** `T1 → T2 → T5 → T6`.
**Parallel spine:** `T3 → T4 → T5` merges into the same engine.
**Blocked on api-architect contracts:** T7 (and T2's final shape — proceeds on §5.6 baseline).
**Blocked on Deriv migration:** T2 (needs T1's OTP/WS plumbing).

---

## 2. Task list

### T1 — Deriv provider migration to NEW API (connection / OTP / auth / accounts / symbols) — CRITICAL PATH

| | |
|---|---|
| **Owner** | `backend-dev` |
| **Dependencies** | none (Wave 0) |
| **Complexity** | 5 |
| **Spec refs** | §2.2 (migration confined to deriv.py), §7.1 (Deriv NEW API row), §5.6 (get_ticks_range context) |

**Files to touch**
- `forex/broker/providers/deriv.py` — rewrite transport/auth/account/symbols to the NEW API
- `forex/broker/config.py` — add `DERIV_REST_URL_DEFAULT = "https://api.derivws.com"` + `DERIV_REST_URL` env override (mirrors existing `DERIV_API_URL_DEFAULT` pattern); `DERIV_REQUIRED_ENV` unchanged (`DERIV_APP_ID`, `DERIV_API_TOKEN`); `DERIV_ACCOUNT_ID` stays optional

**Acceptance criteria (DoD)**
1. Connection uses the OTP flow: `POST /trading/v1/options/accounts/{DERIV_ACCOUNT_ID}/otp` → `{data: {url: "wss://...?otp=..."}}` → WS connect to that URL. **No `authorize` message is ever sent** (OTP-scoped connection is pre-authorized).
2. When `DERIV_ACCOUNT_ID` is empty: `GET /trading/v1/options/accounts` → pick `account_type == "demo"`.
3. Auth headers on REST calls: `Deriv-App-ID: <DERIV_APP_ID>` + `Authorization: Bearer <DERIV_API_TOKEN>`.
4. `get_account()` uses REST `GET /trading/v1/options/accounts` (returns `account_id`, `balance`, `currency`, `account_type`) — **`get_settings` is never called** (unsupported on NEW API).
5. `get_symbols()` uses `active_symbols` **without** `product_type` (that param causes `InputValidationFailed`).
6. Symbol normalization helper added: app symbols are plain (`EURUSD`), Deriv needs `frx`-prefixed (`frxEURUSD`); both directions covered; used by every market-data call.
7. All failures normalized to `BrokerError` + `BrokerErrorCode`; health monitor (`ProviderHealthMonitor`) updated on connect/auth/error — same pattern as today (§0 source-of-truth files).
8. Existing `mock`/`pepperstone` tests unaffected; `python manage.py test forex.broker` passes except legacy Deriv-specific cases (updated in T9).

---

### T2 — `get_ticks_range` + paged backfill on DerivProvider (1000-cap paging, reconnect cadence)

| | |
|---|---|
| **Owner** | `backend-dev` |
| **Dependencies** | T1 (Wave 1) |
| **Complexity** | 4 |
| **Spec refs** | §5.6 (proposed additive contract + Deriv paging), §7.1 (Deriv backfill row), §8 (backfill rate 5–10 req/s, 60-day backfill ≤ 15 min) |

**Files to touch**
- `forex/broker/base.py` — add the **optional** `get_ticks_range(symbol, start, end, limit=1000)` default implementation that falls back to `get_ticks(limit)` (spec §5.6 — additive; existing providers work unchanged)
- `forex/broker/providers/deriv.py` — native `get_ticks_range` implementation

**Acceptance criteria (DoD)**
1. `get_ticks_range(symbol, start, end, limit=1000)` on Deriv returns ticks in `[start, end]`, **newest-first**, using `ticks_history` with `start` + `end` + `adjust_start_time: 1` (end alone is ignored by the API).
2. Paging: max **1000 ticks/request** (count=5000 still returns 1000); `end` is required when `count > 1000`; the planner walks newest-first windows sized so each page returns ≤ 1000 ticks.
3. Reconnect cadence: the WS socket dies after ~2–3 requests → provider re-runs the OTP flow for a **fresh OTP-scoped connection** every N requests (N configurable, default 2, `SYNC_DERIV_RECONNECT_EVERY` in `SyncConfig`).
4. 2-month-old tick data is retrievable via paged windows (verified by an integration test or documented manual verification).
5. `base.py` default `get_ticks_range` → `get_ticks(limit)` keeps `mock`/`pepperstone` working with zero changes (their overrides land in T8).
6. **Contract reconciliation**: when api-architect's `tick-sync-contracts.md` lands, the signature/ordering semantics are reconciled — this task ships on the §5.6 baseline and is additive, so the delta is a rename/adjust, not a rewrite.

---

### T3 — `forex/sync/` package scaffold + `TickNormalizer`

| | |
|---|---|
| **Owner** | `backend-dev` |
| **Dependencies** | none (Wave 0) |
| **Complexity** | 2 |
| **Spec refs** | §2 (module tree), §5.1 (`TickNormalizer`), §3.3 (record layout), §10 Phase 0 (dead code, `SYNC_ENGINE_ENABLED=false`) |

**Files to touch**
- `forex/sync/__init__.py` — public API: `create_engine()`, `SyncEngine`, `TickNormalizer`
- `forex/sync/config.py` — `SyncConfig` (env-driven): `SYNC_MAX_TICKS` (3,400,000), `SYNC_MAX_AGE` (60 days), `SYNC_SEGMENT_CAPACITY` (100,000), `SYNC_RECORD_LAYOUT` (full|compact), `SYNC_MAX_SYMBOLS` (4), `SYNC_INGEST_QUEUE_SIZE` (100,000), `SYNC_COMMIT_BATCH_SIZE` (1000), `SYNC_COMMIT_FLUSH_MS` (10), `SYNC_BACKFILL_RATE` (5 req/s), `SYNC_GAP_MIN_SECONDS` (5), `SYNC_HISTORY_BUFFER` (10,000), `SYNC_MIN_TS`, `SYNC_MAX_FUTURE_SKEW` (5 min), `SYNC_DUP_LOG_RATE`, `SYNC_ENGINE_ENABLED` (false), `SYNC_SNAPSHOT_DIR`, `SYNC_KEEP_WINDOW_ON_SWITCH` (true), `SYNC_SERVE_CHART_DATA`, `SYNC_SERVE_STRATEGY`, `SYNC_DERIV_RECONNECT_EVERY` (2)
- `forex/sync/errors.py` — `SyncError` taxonomy reusing `BrokerErrorCode` where applicable (§2 tree)
- `forex/sync/normalizer.py` — `TickNormalizer` + `NormalizedTick` value object (spec §5.1; `NormalizedTick` lives here — the spec tree has no `models.py`)

**Acceptance criteria (DoD)**
1. Package imports cleanly; `SYNC_ENGINE_ENABLED=false` default → nothing runs (Phase 0 dead code, §10).
2. `TickNormalizer.normalize(MarketTick) -> NormalizedTick | None` rejects (returns None + logs reason): non-finite or non-positive prices; `bid > ask` (epsilon 1e-9); timestamp outside `[SYNC_MIN_TS, now + SYNC_MAX_FUTURE_SKEW]`; symbol failing `^[A-Z0-9_]{1,20}$` (§5.1).
3. Timestamp coerced to **epoch ms UTC int64**; `seq` derived from provider tick id when present, else `-1`.
4. `dedup_key(tick) -> (ts_ms, seq)` when `seq != -1`, else `(ts_ms, float_bits(last))` where `float_bits` is the IEEE-754 bit pattern (`struct.pack(">d", last)` → int) (§4.2).
5. `SyncConfig` reads every `SYNC_*` env var with the spec defaults above; invalid values fall back with a warning (mirrors `TradingConfig.from_env()` behavior).

---

### T4 — `SegmentStore` (numpy segmented sorted arrays, K=100k, dedup, eviction)

| | |
|---|---|
| **Owner** | `backend-dev` (spec §2.1 ownership map: `storage.py` → backend-dev) |
| **Dependencies** | T3 (Wave 1) |
| **Complexity** | 4 |
| **Spec refs** | §3.2–3.5 (decision, layout, segment structure, complexity table), §4.1–4.3 (insert paths, dedup key, eviction), §5.2 (`SegmentStore` protocol) |

**Files to touch**
- `forex/sync/storage.py` — `SegmentStore` + `TICK_DTYPE` (§3.3) + segment index

**Acceptance criteria (DoD)**
1. `TICK_DTYPE` per §3.3: `ts(int64 ms)`, `bid/ask/last(float64)`, `seq(int64)`; FULL layout 40 B/tick; COMPACT layout (`SYNC_RECORD_LAYOUT=compact`: `ts + last + seq`, 24 B/tick) supported.
2. Segments: pre-allocated numpy structured arrays, capacity `K = SYNC_SEGMENT_CAPACITY` (100,000), fill-pointer append — **O(1) amortized, no copies** (§3.4, §4.1).
3. Segment index: `collections.deque` of `(first_ts, last_ts, count, capacity, array_ref)` descriptors, sorted + non-overlapping; `first_ts` boundaries cached in a numpy array for `bisect` — **O(log S)** locate (§3.4).
4. Late-tick insert: `searchsorted` + one memmove within one segment; **split when full** (upper half → new segment); split invariant: every segment `count <= K`, sorted, non-overlapping, time-contiguous (§4.1).
5. Dedup: `searchsorted` on `ts`, then scan equal-`ts` neighbors for `seq`/price match; duplicates counted + logged (`SYNC_DUP_LOG_RATE`) but never stored twice (§4.2).
6. Eviction: dual boundary — `while total_count > MAX_TICKS: popleft()` and `while (now - oldest_segment.last_ts) > MAX_AGE: popleft()`; whichever boundary hits first wins; segment-granular (§4.3).
7. `insert` returns `InsertResult(inserted, duplicates, out_of_window, oldest_ts, newest_ts, total_count)`; `query_range` chronological with `limit` on the newest end; `snapshot` immutable view; `stats` returns count/oldest/newest/segment_count/evictions/duplicates (§5.2).
8. Complexity targets honored (§3.5): locate O(log S), append O(1) amortized, late-insert O(K), dedup O(log K), evict O(1)/segment, query O(m + S).

---

### T5 — `SyncEngine` core (ingest, reconcile, gap detect, commit, publish)

| | |
|---|---|
| **Owner** | `backend-dev` |
| **Dependencies** | T3, T4 (Wave 2); T2 is a soft dependency — engine works via the `get_ticks_range` fallback until T2 lands |
| **Complexity** | 5 |
| **Spec refs** | §1.1 (pipeline), §5.3 (`SyncEngine` protocol), §5.4 (events), §6.1–6.3 (concurrency), §2 tree (`engine.py`, `publisher.py`, management command) |

**Files to touch**
- `forex/sync/engine.py` — `SyncEngine` (§5.3)
- `forex/sync/publisher.py` — `EventPublisher` (§5.4)
- `forex/sync/management/__init__.py` — package init (scaffolding)
- `forex/sync/management/commands/__init__.py` — package init (scaffolding)
- `forex/sync/management/commands/sync_engine.py` — `python manage.py sync_engine` (§2 tree, §9.4)

**Acceptance criteria (DoD)**
1. Pipeline implemented: validate → dedup → gap detect → timestamp order → reconcile → commit → publish (§1.1).
2. **Single-writer principle**: one engine thread owns `SegmentStore` mutations; all ingestion funnels through a bounded queue (`SYNC_INGEST_QUEUE_SIZE` = 100,000); provider callback threads only `put()` (§6.1).
3. Commit batching: drain queue in batches (`SYNC_COMMIT_BATCH_SIZE` = 1000 or `SYNC_COMMIT_FLUSH_MS` = 10 ms, whichever first) under a shared reader lock (`threading.RLock`); writer exclusive, readers shared (§6.2).
4. `ingest_tick`, `backfill`, `subscribe`, `snapshot`, `query_range`, `stats`, `state`, `start`, `stop` implemented per §5.3. `start()` bootstraps: resolve provider via `TradingProviderFactory.get_active()`, subscribe, backfill (§5.3).
5. Publish events (§5.4): `TickCommitted`, `WindowAdvanced`, `StateChanged`, `GapDetected`, `BackfillProgress`, `ProviderSwitched` — fan-out with subscriber error isolation.
6. Gap detection: `now - last_accepted_ts > max(3 × cadence, SYNC_GAP_MIN_SECONDS=5)` → `GapDetected` (§5.5).
7. Provider switch: engine polls `factory.get_active()` identity on heartbeat → `ProviderSwitched` → per-symbol `RESET` (§6.5); window retained per `SYNC_KEEP_WINDOW_ON_SWITCH=true` (§5.5).
8. `python manage.py sync_engine` runs end-to-end with the **mock** provider (e2e smoke, §9.4) — cold start → backfill → live.

---

### T6 — Recovery state machine + backfill worker (SPEC — implemented by `async-engineer`)

| | |
|---|---|
| **Owner** | `async-engineer` (implementation); **interface spec by `tech-lead`** (this section is the task) |
| **Dependencies** | T5, T2 (Wave 3) |
| **Complexity** | 4 |
| **Spec refs** | §5.5 (state machine), §5.6 (backfill planner), §6.4 (backfill workers), §8 (backfill rate targets) |

**Files to touch (async-engineer)**
- `forex/sync/recovery.py` — `RecoveryController` (§5.5)
- `forex/sync/backfill.py` — `BackfillPlanner` (§5.6)

**Interface spec (binding for async-engineer)**

```python
# recovery.py — per-symbol state machine, single owner (the engine thread)
class RecoveryController:
    def transition(self, symbol: str, event: RecoveryEvent) -> SyncState: ...
    # States: INIT, SYNCING_HISTORY, LIVE, GAP, BACKFILLING, RESET (§5.5)
    # Events: START, BACKFILL_DONE, TICK_RECEIVED, GAP_TIMEOUT,
    #         BACKFILL_FAILED, PROVIDER_SWITCHED
    # Illegal transitions raise SyncError (reachability verified in T9 §9.1.5)

# backfill.py — paged window planning per provider capability
class BackfillPlanner:
    def plan(self, provider, symbol, start, end) -> Iterator[BackfillPage]: ...
    # Deriv NEW API: 1000 ticks/request cap, start+end+adjust_start_time:1,
    #   newest-first, page window sized so each page returns <= 1000 ticks
    # Pepperstone: falls back to provider.get_ticks(limit) (subprocess/call)
    # Mock: bounded by the provider's 1000-tick buffer — best-effort partial
    # Token bucket: SYNC_BACKFILL_RATE (default 5 req/s) (§6.4, §8)
```

**Acceptance criteria (DoD)**
1. State machine per §5.5: `INIT → SYNCING_HISTORY → LIVE ↔ GAP → BACKFILLING → LIVE`; `LIVE → RESET → SYNCING_HISTORY` on provider switch; `LIVE → GAP → BACKFILLING(from last_accepted_ts) → LIVE` on reconnect; backfill failure → retry with backoff. Every transition reachable, every illegal transition rejected (§9.1.5).
2. `SYNCING_HISTORY`: live ticks arriving meanwhile are buffered (bounded, `SYNC_HISTORY_BUFFER` = 10,000) and reconciled after the backfill watermark passes them (§5.5).
3. `BACKFILLING`: paged fill from `last_accepted_ts` → `now`; dedup handles overlap with committed ticks; on completion, walk segment boundaries — any residual hole > threshold → re-plan (§5.5).
4. `RESET`: per-symbol state cleared; window retained or dropped per `SYNC_KEEP_WINDOW_ON_SWITCH` (default true) (§5.5).
5. Backfill workers: 1–2 threads fetch pages (token-bucket rate-limited, `SYNC_BACKFILL_RATE` = 5 req/s) and enqueue normalized ticks; the engine thread remains the sole committer (§6.4).
6. Targets: reconnect gap backfill (minutes of downtime) ≤ 30 s; full 60-day backfill (3.4M ticks, ~3400 pages) ≤ 15 min at 5 req/s (§8).

---

### T7 — `/api/sync/*` endpoints — **BLOCKED on api-architect contracts**

| | |
|---|---|
| **Owner** | `backend-dev` |
| **Dependencies** | T5 + **api-architect `tick-sync-contracts.md`** (Wave 4; cannot start until the contract lands) |
| **Complexity** | 2 (thin endpoints once the contract exists) |
| **Spec refs** | §7.3 (proposals for api-architect — this spec does NOT invent their contracts), §10 Phase 4 (feature-flag the new endpoints), §12 (open item: api-architect to confirm the `/api/sync/*` surface) |

**Files to touch (provisional — final paths per api-architect contract)**
- `forex/sync/views.py` — new `/api/sync/*` views (status, window stats, query, SSE subscribe — per contract)
- `forex/sync/urls.py` — new urlpatterns
- `forex/urls.py` — additive `include()` of sync urls (existing broker include at line 103 untouched)

**Acceptance criteria (DoD)**
1. Endpoints match api-architect's contract exactly (paths, DTOs, validation) — no invented fields.
2. **Additive only**: existing `/api/provider/*` (broker/urls.py), `/api/ticks/`, `/api/chart-data/` behavior unchanged (§7.3, §10 guarantees).
3. JWT-protected (`IsAuthenticated`), same envelope `{status, message, data, errors}` as `/api/provider/*` (broker/views.py pattern).
4. Feature-flagged per §10 Phase 4 (e.g. `SYNC_SERVE_CHART_DATA` re-point of `/api/chart-data/` to `engine.query_range` with ClickHouse fallback — same response shape `{bid, ask, spread, time}`).

---

### T8 — Mock `get_ticks_range` + Pepperstone query-worker history extension

| | |
|---|---|
| **Owner** | `backend-dev` |
| **Dependencies** | none (Wave 0) |
| **Complexity** | 2 |
| **Spec refs** | §5.6 (per-provider backfill capability), §7.1 (Mock/Pepperstone rows) |

**Files to touch**
- `forex/broker/providers/mock.py` — `get_ticks_range` override: best-effort slice of the 1000-tick internal buffer within `[start, end]` (§7.1: "bounded by the provider's 1000-tick buffer — best-effort partial window; the engine tolerates it")
- `forex/pepperstone/query_worker.py` — add a `history` query type (range-based tick history; one-shot subprocess, ~1–2 s/call)
- `forex/broker/providers/pepperstone.py` — `get_ticks_range` override using the new query type (§5.6: "subprocess per call — slow, rate-limited; planner rate-limits; recovery tolerates slow backfill")

**Acceptance criteria (DoD)**
1. `MockProvider.get_ticks_range` returns ticks within `[start, end]` from the buffer, newest-first, best-effort (may be partial — engine tolerates gaps, §7.1).
2. `query_worker` gains `history` query type with the same `{"ok": true, "data": ...}` single-JSON-line contract as existing query types (pepperstone.py `_run_query` parsing unchanged).
3. `PepperstoneProvider.get_ticks_range` returns newest-first ticks in `[start, end]` via the worker; failures normalized to `BrokerError` (existing `_run_query` path).
4. Existing query types (`account`, `balance`, `positions`, `orders`, `symbols`, `market_data`, `trade_history`, `cancel_order`, `modify_order`) behavior unchanged.

---

### T9 — Tests (SPEC — implemented by `qa-engineer`)

| | |
|---|---|
| **Owner** | `qa-engineer` (implementation); **scenario spec by `tech-lead`** (this section is the task) |
| **Dependencies** | T1–T5 (Wave 4) |
| **Complexity** | 3 |
| **Spec refs** | §9.1 (property-based), §9.2 (unit), §9.3 (integration), §9.4 (verification commands) |

**Files to touch (qa-engineer)**
- `forex/sync/tests/` — unit + property + integration suites (§2 tree: `tests/` owned by qa-engineer)
- `forex/broker/tests.py` — update Deriv tests for the NEW API (OTP flow, REST accounts, `get_ticks_range` paging, symbol normalization)
- `forex/pepperstone/tests.py` — `history` query type tests (if the existing suite covers query types)

**Scenario spec (binding for qa-engineer)**
1. **Property-based (hypothesis)** — §9.1:
   - Ordering invariant: arbitrary arrival order (shuffled/reversed/interleaved) → stored window always sorted by `ts`.
   - Dedup: same `(ts, seq)` or `(ts, last_bits)` injected any number of times → never stored twice; duplicates counted.
   - Gap detection: stall > threshold always transitions `LIVE → GAP → BACKFILLING → LIVE`; stall < threshold never does.
   - Eviction boundaries: (i) count hit while age fine → evict by count; (ii) age hit while count fine → evict by age; (iii) both → whichever first; window never exceeds either bound.
   - Recovery transitions: every §5.5 transition reachable; every illegal transition rejected.
   - Segment invariants: `count <= K`; sorted, non-overlapping, time-contiguous; split preserves invariants.
2. **Unit** — §9.2: normalizer (epoch ms / ISO-8601 / None coercion, future-skew rejection, non-finite prices, symbol allowlist); storage (append / late-insert / duplicate / out-of-window / evict / query_range); engine (state machine, commit batching, publish event ordering); publisher (registration, fan-out, error isolation).
3. **Integration (MockProvider as the deterministic vehicle)** — §9.3: `set_fail_mode("network")` mid-stream → GAP → recovery → LIVE; duplicate injection at the historical/live sync boundary; late-tick injection (T1→T2→T4→T3); provider switch mid-stream (`factory.switch("mock"→"deriv"→"mock")`); cold start: full backfill → seamless transition to live.
4. **Deriv NEW-API specifics**: OTP flow connects with no `authorize` message; `get_account` via REST (no `get_settings`); `get_ticks_range` paging (1000-cap, `start`+`end`+`adjust_start_time:1`, reconnect cadence); `active_symbols` without `product_type`; `frx` symbol normalization.
5. **Verification**: `pytest forex/sync/tests/` exit 0; `python manage.py sync_engine` (mock) e2e smoke; existing broker/pepperstone suites green (§9.4).

---

### T10 — Snapshot persistence layout (OPTIONAL — Phase 3, not on critical path)

| | |
|---|---|
| **Owner** | `database-engineer` |
| **Dependencies** | T3, T4 (Wave 1; can slip to Wave 4 — non-blocking) |
| **Complexity** | 2 |
| **Spec refs** | §4.4 (optional persistence — "database-engineer owns the layout"), §11.6 (persistence is optional; cold start is a normal SYNCING_HISTORY event) |

**Files to touch**
- `forex/sync/snapshot.py` — per-symbol directory of sealed segments as numpy `.npy` files + a JSON index (`first_ts`, `last_ts`, `count`); enabled by `SYNC_SNAPSHOT_DIR`

**Acceptance criteria (DoD)**
1. Layout per §4.4: per-symbol directory of sealed-segment `.npy` files + JSON index.
2. Engine treats a missing/stale snapshot as a normal `SYNCING_HISTORY` event — persistence is an optimization, **never a correctness dependency** (§4.4, §11.6).
3. Snapshot write/read is off the hot path (sealed segments only; active segment excluded).

---

### T11 — Hot-path benchmark + optimization pass (per §8 binding targets)

| | |
|---|---|
| **Owner** | `performance-engineer` |
| **Dependencies** | T4, T5 (Wave 3; non-blocking for T6/T7) |
| **Complexity** | 3 |
| **Spec refs** | §8 (binding performance targets), §3.5 (complexity table) |

**Files to touch**
- `forex/sync/benchmarks/bench_ingest.py` — benchmark harness (new; benchmark artifact, not production code)
- `forex/sync/storage.py`, `forex/sync/engine.py` — optimization fixes only if a target is violated (no interface change)

**Acceptance criteria (DoD)**
1. §8 targets verified and met:
   - Live ingest throughput ≥ 50k ticks/s sustained (target 200k)
   - Ingest → publish latency p95 < 5 ms
   - Late-tick insert p95 < 1 ms @ K=100k
   - Memory ≤ 136 MB/symbol FULL (82 MB COMPACT) @ 3.4M; `SYNC_MAX_SYMBOLS` cap respected
   - `query_range` (1M ticks) < 10 ms
   - Eviction amortized O(1)/tick
2. Any violation fixed inside `storage.py`/`engine.py` without changing the §5.2/§5.3 interfaces.

---

## 3. Sequencing summary

| Wave | Tasks | Unblocked by | Notes |
|---|---|---|---|
| 0 | T1, T3, T8 | — | Fully parallel; T1 is the critical-path head |
| 1 | T2 (←T1), T4 (←T3), T10 (←T3/T4, optional) | T1, T3 | T2 cannot start before the OTP/WS plumbing exists |
| 2 | T5 (←T3, T4; soft ←T2) | T3, T4 | Engine builds against the `get_ticks_range` fallback; T2 upgrades it to native paging |
| 3 | T6 (←T5, T2), T11 (←T4, T5) | T5, T2 | Recovery needs the engine + native Deriv paging |
| 4 | T7 (←T5 + api-architect), T9 (←T1–T5) | T5, contracts | T7 is contract-blocked; T9 is the gate-input suite |

**Critical path:** T1 → T2 → T5 → T6 (Deriv end-to-end: OTP connect → paged backfill → engine → recovery).
**Parallel spine:** T3 → T4 → T5 merges at the engine; T8/T10/T11 run off-path; T7/T9 are Wave-4.

---

## 4. Consolidated file-level change manifest

| File | Task | Change |
|---|---|---|
| `forex/broker/providers/deriv.py` | T1, T2 | Rewrite to NEW API (OTP/REST/WS) + native `get_ticks_range` paging |
| `forex/broker/config.py` | T1 | Add `DERIV_REST_URL_DEFAULT` + `DERIV_REST_URL` env override |
| `forex/broker/base.py` | T2 | Add optional `get_ticks_range` default (fallback to `get_ticks`) |
| `forex/broker/providers/mock.py` | T8 | Add `get_ticks_range` override (buffer slice) |
| `forex/broker/providers/pepperstone.py` | T8 | Add `get_ticks_range` override (worker-backed) |
| `forex/pepperstone/query_worker.py` | T8 | Add `history` query type |
| `forex/sync/__init__.py` | T3 | Public API (`create_engine`, `SyncEngine`, `TickNormalizer`) |
| `forex/sync/config.py` | T3 | `SyncConfig` (all `SYNC_*` env vars) |
| `forex/sync/errors.py` | T3 | `SyncError` taxonomy |
| `forex/sync/normalizer.py` | T3 | `TickNormalizer` + `NormalizedTick` |
| `forex/sync/storage.py` | T4, T11 | `SegmentStore` + `TICK_DTYPE` (+ perf fixes if needed) |
| `forex/sync/engine.py` | T5, T11 | `SyncEngine` (+ perf fixes if needed) |
| `forex/sync/publisher.py` | T5 | `EventPublisher` |
| `forex/sync/recovery.py` | T6 | `RecoveryController` (async-engineer) |
| `forex/sync/backfill.py` | T6 | `BackfillPlanner` (async-engineer) |
| `forex/sync/management/__init__.py` | T5 | Package init |
| `forex/sync/management/commands/__init__.py` | T5 | Package init |
| `forex/sync/management/commands/sync_engine.py` | T5 | Management command |
| `forex/sync/views.py` | T7 | `/api/sync/*` views (provisional — per api-architect contract) |
| `forex/sync/urls.py` | T7 | Sync urlpatterns (provisional) |
| `forex/urls.py` | T7 | Additive `include()` of sync urls |
| `forex/sync/snapshot.py` | T10 | Snapshot persistence layout (optional, Phase 3) |
| `forex/sync/benchmarks/bench_ingest.py` | T11 | Benchmark harness (artifact) |
| `forex/sync/tests/` | T9 | Unit + property + integration suites (qa-engineer) |
| `forex/broker/tests.py` | T9 | Deriv NEW-API test updates (qa-engineer) |
| `forex/pepperstone/tests.py` | T9 | `history` query-type tests (qa-engineer) |

**Untouched (guaranteed):** `forex/broker/{factory,errors,health,views,urls}.py`, `forex/ticks_views.py`, `forex/clickhouse/*`, `forex/pepperstone/{ticks_engine,client,execution_engine}.py`, `forex/mtf_strategy.py`, all `/api/provider/*` routes.

---

## 5. Open items (escalate to builder)

1. **api-architect**: `njira_fx_api/docs/architecture/tick-sync-contracts.md` does not exist — T7 is blocked; T2 ships on the §5.6 baseline and reconciles (spec §12).
2. **builder**: persist ADR `dec-20260819-fc8319` to the Memory Fabric (router unavailable in this session — spec §0/§12).
3. **builder**: persist this plan + task states via Memory Fabric (`handoff create tick-sync-implementation`) when the router is available.
4. **qa-engineer** owns T9 scenarios (spec §9); **async-engineer** owns T6 implementation (spec §5.5/§5.6) — both delegated by builder per chain-of-command.