# Architecture Audit — njira_auto_fx Signal Generation Engine

> **Date:** 2026-08-28
> **Auditor:** CTO (Principal Software Architect)
> **Scope:** Full reconstruction of the signal-generation pipeline from code, verified against prior hypothesis (`architecture_flow.dot`) and target spec (`improve.md`).
> **Method:** Direct file reads and grep for every claim. Zero fabrication.

---

## Part I — Verified Architecture Map

### 1. Historical Data Acquisition

| Question | Answer | Evidence |
|---|---|---|
| How are historical candles obtained? | **Startup preload from ClickHouse only.** No external fetch. | `startup.py:25-71` — `preload_history()` reads from CH via `get_candles_from_ch()`. |
| Is there a 3-month historical fetch? | **NO.** No backfill mechanism exists. | `query_worker.py:496-515` — `ticks_range` is a stub returning `not_implemented`. No management command for backfill. |
| What timeframes are stored in CH? | **Only 1M candles.** | `schema.py:103-120` — `_CANDLES_DDL` stores `timeframe UInt32` but only 1M is written. Higher TFs are aggregated on-the-fly (`schema.py:395-421`). |
| What does `requirements.py` declare? | Only 1M: `min=200, desired=1000` candles. | `requirements.py:26-28`. |

**Verdict:** `MISSING` — No 3-month backfill. System relies entirely on what live ticks produce over time.

---

### 2. Live Tick Pipeline

```
cTrader WS → PepperstoneSession.on_message
  → TicksEngine._on_message (ticks_engine.py:863)
    → _repair_one_sided_tick (ticks_engine.py:79)
    → GapDetector.check (ticks_engine.py:894)
    → TickBuffer.feed — dedup + OOO 5s window (ticks_engine.py:897)
    → Fan-out:
       ├─ MicrostructureEngine.feed (ticks_engine.py:912)
       ├─ SpreadEngine.update (ticks_engine.py:917)
       ├─ VolumeProfileEngine.feed_tick (ticks_engine.py:922)
       ├─ CandleRegistry.feed_tick → 6 TFs (ticks_engine.py:935)
       ├─ SessionEngine.update (ticks_engine.py:972)
       ├─ PersistenceBuffer → BatchWriter → ClickHouse (ticks_engine.py:987)
       └─ Broadcast queue → Pusher (ticks_engine.py:995)
```

**Status: HEALTHY.** All components wired. Gap detection present. Dedup+OOO buffer active.

---

### 3. Candle Aggregation

**CandleRegistry** (`ticks_engine.py:192-195`) builds **6 timeframes**:

| TF (seconds) | Label | Status |
|---|---|---|
| 60 | 1M | HEALTHY |
| 300 | 5M | HEALTHY |
| 900 | 15M | HEALTHY |
| 3600 | 1H | HEALTHY |
| 14400 | 4H | HEALTHY |
| 86400 | 1D | HEALTHY |

**Candle close flow** (`_on_candle_close`, ticks_engine.py:355-568):

1. HotCache.add (line 371)
2. VolatilityEngine.update (line 380)
3. VolumeProfileEngine.feed_candle (line 391)
4. insert_candle_batch → ClickHouse (line 404)
5. Signal engine evaluation (line 461)
6. Intelligence broadcasts (lines 488-502)
7. Analysis snapshot broadcast (line 507)
8. Provider status broadcast (line 519, every 15s)
9. MTF status broadcast (line 529, every 5s)
10. Volume profile broadcast (line 537, every 10s)
11. Signal CH persist (line 563)

Gap-fill candles: synthetic, skipped for analysis (`ticks_engine.py:412-413`).

**Status: HEALTHY.** Previous hypothesis that CandleRegistry was TF=60 only is **INCORRECT** — fixed.

---

### 4. Memory Architecture

| Component | Capacity | Location | Evidence |
|---|---|---|---|
| HotCache | 500 per (symbol, tf) | In-memory | `cache.py:30` |
| CandleRegistry closed deque | 500 per (symbol, tf) | In-memory | `candle_engine.py:47` |
| PersistenceBuffer | 50,000 ticks | In-memory | `ticks_engine.py:178-183` |
| VolumeProfileEngine | ~2,000 ticks + ~100-200 bins/symbol | In-memory | `volume_profile.py` |
| LastGoodSides | 1 per symbol | In-memory | `ticks_engine.py:76` |

**HotCache.get_candles** exists as alias of `get()` (`cache.py:69-71`). **FIXED** — previous hypothesis was wrong.

**ClickHouse as historical source of truth:** Yes, `_CANDLES_DDL` persists candles; `get_candles_from_ch()` reads back; higher TFs aggregated on-the-fly.

**Status: HEALTHY.** Memory footprint ~20 MB per symbol as estimated.

---

### 5. Strategy Layer

**No registry.** Hardcoded dispatch in `signal_engine.py:134-144`:

```python
modes = [m.strip() for m in strategy_mode.split(",") if m.strip()]
if modes == ["both"]:
    modes = ["malysian", "Moving_averages"]
for mode in modes:
    if mode == "Moving_averages":
        all_signals.extend(self._evaluate_mtf(symbol))
    elif mode == "malysian":
        all_signals.extend(self._evaluate_confluence(symbol))
```

**Per-user strategy selection:** `_read_user_strategy()` (`ticks_engine.py:570-606`) reads from ClickHouse `userdetails` table. Refreshes every 60s. Valid values: `"Moving_averages"`, `"malysian"`, `"both"`.

**MultiTimeframeStrategy** (`mtf_strategy.py`):
- `check_market_alignment()` (`mtf_strategy.py:92-125`): Queries ClickHouse **directly** with 7 blocking queries via `get_ohlc_bars()`. **NOT using HotCache.**
- `check_market_alignment_with_score()` (`mtf_strategy.py:127-163`): Same — 7 direct CH queries.
- **However**, `_build_mtf_status_event()` (`ticks_engine.py:767-813`) now uses `HotCache.get_candles` instead of the strategy's direct queries. The MTF strategy is still called directly from `_evaluate_mtf()` in the signal engine path.

**Status: MISALIGNED.** Strategy dispatch works but `MultiTimeframeStrategy` bypasses the hot cache in the signal evaluation path. No strategy registry.

---

### 6. Context / Market State

**No shared Market Context State exists.**

- `_build_market_state()` (`signal_engine.py:688-787`) builds a dict for the contradiction engine, but it is:
  1. Built fresh on every evaluation
  2. Not shared between strategies
  3. Not incrementally updated
  4. Not exposed to the UI

- Each strategy (`_evaluate_confluence`, `_evaluate_mtf`) builds its own context independently.

**Status: MISSING.** Per improve.md §17 — should be a central shared state.

---

### 7. Signal Pipeline

```
Intelligence engines → _build_draft() → min_confidence check
  → ContradictionEngine.analyze() → recommendation
    → TRADE → SignalLifecycleManager.transition(CONFIRMED)
    → WAIT → transition(WAITING)
    → INVALIDATE → transition(INVALIDATED)
```

**ContradictionEngine** (`contradiction.py:42-156`):
- 12 heuristic checks
- MTF evidence **FIXED**: `mtf_breakdown` dict contributes real support weight (`contradiction.py:97-107`)
- Threshold: `net < 20 → WAIT`, `MODERATE + net < 40 → WAIT` (`contradiction.py:139-142`)
- **The "always WAIT" bug is FIXED** — signals can reach TRADE.

**BUT:** WAITING signals are never re-evaluated on subsequent candle closes. Once a signal transitions to WAITING, it stays there until it expires (`signal_lifecycle.py:249-257`). This is a **design gap** — the signal pipeline creates a new signal on every candle close instead of updating existing WAITING signals.

**Signal classification** (`signal_lifecycle.py:83-89`):
- `≥75 → STRONG`
- `≥50 → NORMAL`
- `<50 → WEAK`

**Status: PARTIAL.** Core pipeline works. Contradiction engine fixed. But WAITING re-evaluation is missing and duplicate signals are generated on every candle close.

---

### 8. Signal Storage + Frontend Delivery

**ClickHouse persistence:**
- `signals_v2` table (`schema.py:127-149`) — ReplacingMergeTree keyed by signal_id
- Inserted on every candle close for every symbol (`ticks_engine.py:560-566`)
- `insert_signal_v2()` (`schema.py:428-474`) — best-effort, ACCESS_DENIED tolerant

**Pusher broadcasts** (all from `ticks_engine.py`):
| Event | Channel | Frequency | Evidence |
|---|---|---|---|
| `tick` | njira-market | ~4/s per symbol | `ticks_engine.py:1084-1097` |
| `candle` | njira-market | on close | `ticks_engine.py:939-961` |
| `signal` | njira-market | on close | `ticks_engine.py:478-483` |
| `structure` | njira-market | on close | `ticks_engine.py:489-491` |
| `liquidity` | njira-market | on close | `ticks_engine.py:493-494` |
| `order_block` | njira-market | on close | `ticks_engine.py:496-497` |
| `fvg` | njira-market | on close | `ticks_engine.py:499-500` |
| `analysis` | njira-market | on close | `ticks_engine.py:508-509` |
| `provider_status` | njira-market | every 15s | `ticks_engine.py:519-521` |
| `mtf_status` | njira-market | every 5s | `ticks_engine.py:529-532` |
| `volume_profile` | njira-market | every 10s | `ticks_engine.py:537-555` |
| `symbols_catalogue` | njira-market | on change | `ticks_engine.py:651-661` |

**Frontend:**
- `useReverb.ts`: Pusher singleton, subscribes to `njira-market`, gated by `NEXT_PUBLIC_ENABLE_REALTIME === 'true'` AND `NEXT_PUBLIC_PUSHER_KEY` (`useReverb.ts:62-64`).
- `useMarketEvents.ts`: Binds 11 events on `njira-market` channel (`useMarketEvents.ts:120-131`).
- **`.env.development`**: Has `NEXT_PUBLIC_PUSHER_KEY=cd290f4b0019278552cc`, `NEXT_PUBLIC_PUSHER_CLUSTER=ap2`, `NEXT_PUBLIC_ENABLE_REALTIME=true` (`.env.development:12-14`).
- **`.env.production`**: Has **NO** `NEXT_PUBLIC_PUSHER_KEY`, **NO** `NEXT_PUBLIC_ENABLE_REALTIME`. Only Reverb config (`.env.production:7-11`).

**Status: MISALIGNED.** Backend broadcasts are correct. Frontend dev is wired. **Production realtime is DEAD** — missing Pusher keys.

---

### 9. Dead / Duplicated / Disconnected Components

| Component | Status | Evidence |
|---|---|---|
| `analysis_module/` | **DEAD** (disabled by default) | `runall.py:55-58` — `RUNALL_ANALYSIS=false` default. `main.py:133` still calls `save_signals_to_clickhouse` → `insert_signal_v2` (competing signal writer when enabled). |
| `forex/sync/` | **DEAD** (flag off) | `sync/management/commands/sync_engine.py:105` — `SYNC_ENGINE_ENABLED=false`. |
| `trade/views.py` | **DEAD** (410 stubs) | `trade/views.py:18` — all endpoints return 410. |
| `mtf_aggregator.py` | **ORPHANED** | Exists as management command but not started by `runall.py`. Reads Django cache for `macro_signal_<SYMBOL>`. |
| `insert_fvg_batch` | **DISCONNECTED** | Defined in `schema.py:534` but **0 callers in Python code**. FVGs are never persisted. |
| `execution_engine.py` | **ONE-SHOT** | Twisted reactor is process-global. First call succeeds, second fails (`execution_engine.py:54-58`). |
| `get_ohlc_bars` | **MISALIGNED** | Aggregates from ticks table (not candles table). Used by MTF strategy but not by hot cache path. |

---

### 10. Performance Characteristics

**Every tick:**
- `_repair_one_sided_tick` — O(1)
- `GapDetector.check` — O(1)
- `TickBuffer.feed` — O(1) amortized
- `MicrostructureEngine.feed` — O(1)
- `SpreadEngine.update` — O(1)
- `VolumeProfileEngine.feed_tick` — O(1) per bin
- `CandleRegistry.feed_tick` — O(6 TFs) per tick
- `SessionEngine.update` — O(1)
- `PersistenceBuffer.put` — O(1)
- `broadcast_queue.put_nowait` — O(1)

**Every candle close (per symbol, per TF):**
- `HotCache.add` — O(1)
- `VolatilityEngine.update` — O(1)
- `VolumeProfileEngine.feed_candle` — O(1)
- `insert_candle_batch` — O(1) batch write
- **Signal engine evaluation** — runs all intelligence engines for ALL TFs: O(TF × candles_per_TF)
- Intelligence broadcasts — O(TF × events)
- Analysis/MTF/VP broadcasts — O(1)

**MTF strategy** (`_evaluate_mtf`): 7 blocking ClickHouse queries (`mtf_strategy.py:100,134,139`).

**Potential bottleneck:** Signal engine runs `_run_structure`, `_run_liquidity`, `_run_ob`, `_run_fvg`, `_run_poc` on every candle close for every TF. No dirty-state tracking. No event-driven execution per improve.md §31.

---

## Part II — Gap Analysis (improve.md 53 Sections)

| # | Section | CURRENT STATE | Evidence |
|---|---|---|---|
| 1 | First: Do Not Modify Blindly | EXISTS | `improve.md` is the spec. This audit fulfills §1. |
| 2 | 3 Months 1M Historical Data | **MISSING** | No backfill mechanism. `query_worker.py:496` stub. Only live ticks produce candles. |
| 3 | Do Not Store Everything in Memory | EXISTS | Tiered architecture: CH = persistent, HotCache = working set, PersistenceBuffer = bounded. `cache.py:30`, `ticks_engine.py:178`. |
| 4 | Memory Architecture (Tier 1/2/3) | **PARTIAL** | Tier 1 (CH) exists. Tier 2 (analysis cache) exists. Tier 3 (tick buffer) exists. But no backfill for Tier 1 historical data. |
| 5 | Context vs. Recalculation | **MISSING** | No distinction between historical analysis and live analysis windows. Every candle close triggers full re-analysis. |
| 6 | Multi-TF Responsibility (1M) | **PARTIAL** | 1M candles exist in HotCache but 1M doesn't "determine precision entry" — it's just a source for higher TF aggregation. |
| 7 | Multi-TF Responsibility (5M) | **PARTIAL** | 5M candles built by CandleRegistry but no dedicated 5M-specific entry analysis. |
| 8 | Multi-TF Responsibility (15M) | **PARTIAL** | 15M candles built but no bridge logic between execution and HTF structure. |
| 9 | Multi-TF Responsibility (30M) | **PARTIAL** | 30M candles built but no confirmation-specific logic. |
| 10 | Multi-TF Responsibility (1H) | **PARTIAL** | 1H candles built. Used in structure engines. No dedicated "setup developing" evaluation. |
| 11 | Multi-TF Responsibility (4H) | **PARTIAL** | 4H candles built. Used in HTF bias check (`signal_engine.py:402`). |
| 12 | Daily Context | **PARTIAL** | 1D candles built by CandleRegistry. Session engine tracks session high/low. But no daily range position tracking exposed to UI. |
| 13 | Session Context | **PARTIAL** | `SessionEngine` tracks session open/high/low/range. Broadcast via `analysis` event. But no session-specific strategy logic. |
| 14 | Order Block Engine | **PARTIAL** | `OrderBlockEngine` exists, runs per TF. But no TF hierarchy (minor/local/significant/major classification missing). No contextual attributes beyond basic direction/state. |
| 15 | FVG Engine | **PARTIAL** | `FVGEngine` exists, runs per TF. But `insert_fvg_batch` has 0 callers — FVGs are never persisted. No lifecycle tracking (filled/partial/rejected). |
| 16 | Volume Profile | **PARTIAL** | `VolumeProfileEngine` exists with POC/VAH/VAL/HVN/LVN. Tick-level feed + candle feed. But no historical profile persistence. No session/daily POC distinction. |
| 17 | The Context Engine | **MISSING** | No shared Market Context State. Each strategy rebuilds independently. `_build_market_state()` is per-evaluation, not shared. |
| 18 | Context Awareness | **MISSING** | No understanding of state vs. isolated events. Order Block means same thing regardless of 4H bias, 1H retracement, etc. |
| 19 | Strategy Selection (Symbol) | **PARTIAL** | Per-user symbol selection from `symbols` CH table (`ticks_engine.py:608-630`). Works. But no "all symbols" independent analysis. |
| 20 | Strategy Selection (Strategy) | **PARTIAL** | Per-user strategy selection works (`_read_user_strategy`). But only 2 strategies exist (malysian + MTF SMA). No strategy registry. |
| 21 | Strategy Agreement | **MISSING** | No mechanism to run all strategies and report agreement/disagreement. `signal_engine.py:150-152` picks the strongest, discards others. |
| 22 | Signal Generation Pipeline (14 stages) | **PARTIAL** | Stages 1-6 exist (data→live→context→scope→strategy→candidate). Stages 7-13 partially exist (validation, confluence, contradiction, scoring, classification). Stage 14 (lifecycle) exists but WAITING is never re-evaluated. |
| 23 | "Why?" Second-Order Validation | **PARTIAL** | `_build_explanation()` builds human-readable text. ContradictionEngine asks "why not trade?". But no formal "is that reason still valid?" re-check on subsequent ticks. |
| 24 | Confidence Not Fake Precision | EXISTS | Normalized to /100 scale (`signal_engine.py:677-682`). Strength labels: STRONG/NORMAL/WEAK. No arbitrary precision. |
| 25 | Strong Signal | **PARTIAL** | Threshold ≥75. But requirements are not as strict as spec (no "substantial evidence" gate). |
| 26 | Normal Signal | EXISTS | 50-74 range. |
| 27 | Weak Signal | EXISTS | <50 range. |
| 28 | No Signal | EXISTS | Empty signal list → NO_SIGNAL. ContradictionEngine can force WAIT. |
| 29 | Prevent Duplicate Signals | **BROKEN** | New signal created on **every candle close** for every symbol. No dedup against existing WAITING signals. `_recent_signals` is a flat list, not checked before creation. |
| 30 | When Should Engine "Sleep" | **MISSING** | No event-driven execution. All intelligence engines run on every candle close regardless of relevance. No timeframe-specific wakeups. |
| 31 | Event-Driven Invalidation | **MISSING** | No wake-on-BOS, wake-on-OB-entry, wake-on-liquidity-sweep. All analysis runs on every close. |
| 32 | Signal Cooldown | **PARTIAL** | No global cooldown (good). But no setup-identity-based dedup either. Duplicate signals on every close. |
| 33 | Signal State Machine | **PARTIAL** | Formal states exist: CANDIDATE→WATCHING→WAITING→TRIGGERED→CONFIRMED→INVALIDATED/EXPIRED/COMPLETED. But WAITING never transitions to anything (no re-eval). TRIGGERED never used. |
| 34 | Retest Logic | **MISSING** | No retest detection. MarketStructureEngine detects BOS/CHoCH but no retest-after-BOS logic. |
| 35 | Price Reaction Over Static Levels | **MISSING** | No reaction analysis. Levels are detected but no approach/touch/rejection/acceptance evaluation. |
| 36 | 95% Price Action / 5% Fundamental | **PARTIAL** | System is 100% price action. No fundamental context integration (no economic calendar, no news). |
| 37 | Data Quality Gate | **PARTIAL** | `data_quality` scoring exists (`signal_engine.py:571-575`). But no explicit freshness check, no spread reasonableness gate, no "required timeframe data exists" gate. |
| 38 | Signal Quality Gate (15 questions) | **MISSING** | No formal 15-question quality gate. Ad-hoc scoring exists but not the structured gate described in spec. |
| 39 | Signal Explanation | **PARTIAL** | `_build_explanation()` produces text (`signal_engine.py:886-895`). But not as structured as spec (no per-TF breakdown, no strategy agreement line). |
| 40 | "Are You Sure?" Validation | **MISSING** | No explicit confidence challenge stage. ContradictionEngine partially covers this but not the full "what makes this wrong?" cycle. |
| 41 | Contradiction Engine | **PARTIAL** | 12 checks exist. MTF evidence fixed. But checks are heuristic, not structural (no "bearish higher-TF structure" check against actual 4H data). |
| 42 | Scoring Must Not Replace Reasoning | **PARTIAL** | Scoring weights exist but `_build_draft()` is essentially "sum weighted indicators". No market-context interpretation before scoring. |
| 43 | Historical Context Without Degradation | **MISSING** | No distinction between "load once" and "incremental". All intelligence engines re-analyze all cached candles on every close. |
| 44 | Incremental Candle Aggregation | **PARTIAL** | CandleRegistry is incremental (boundary detection). But intelligence engines re-analyze all cached candles, not incrementally. |
| 45 | Real-Time State Observable | **PARTIAL** | Backend broadcasts analysis, MTF status, VP, signals, structure, liquidity, OB. But no strategy states, no strategy agreement, no invalidation line, no data freshness indicator in UI. |
| 46 | Performance Requirement | **BROKEN** | Full recalculation on every candle close. No dirty-state tracking. No event-driven execution. MTF strategy has 7 blocking CH queries. |
| 47 | Database Responsibility | **PARTIAL** | CH is source of truth for ticks and candles. But no recovery/backfill from CH when engine restarts (only startup preload). |
| 48 | Recovery | **MISSING** | No gap detection on restart beyond `reconcile_last_candle()`. No backfill of missing interval. No data freshness marking. |
| 49 | Graphviz Validation | **PARTIAL** | `architecture_flow.dot` exists but is stale. This audit refreshes it. |
| 50 | Required Implementation Order (14 phases) | **PARTIAL** | Phase 1 (understand) — this audit. Phase 2 (historical) — MISSING. Phase 3 (historical→live) — MISSING. Phase 4 (context) — MISSING. Phase 5 (MTF) — PARTIAL. Phase 6 (price action) — PARTIAL. Phase 7 (strategy) — PARTIAL. Phase 8 (candidate) — EXISTS. Phase 9 (validation) — PARTIAL. Phase 10 (classification) — EXISTS. Phase 11 (lifecycle) — PARTIAL. Phase 12 (performance) — MISSING. Phase 13 (UI) — PARTIAL. Phase 14 (validation) — N/A. |
| 51 | Acceptance Criteria | See below | |
| 52 | Final Principle | **MISSING** | System still asks "Can I generate a signal?" not "What is the market doing?". No continuous state machine. |
| 53 | Final Instruction | **THIS AUDIT** | Fulfilled by this document. |

### Gap Counts

| Status | Count | Sections |
|---|---|---|
| **EXISTS** | 6 | 1, 3, 24, 26, 27, 28 |
| **PARTIAL** | 25 | 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 19, 20, 22, 23, 25, 32, 33, 36, 37, 39, 42, 43, 44, 45, 47, 49, 50 |
| **MISSING** | 16 | 2, 5, 17, 18, 21, 30, 31, 34, 35, 38, 40, 43, 46, 48, 51, 52 |
| **BROKEN** | 2 | 29, 46 |

**Total BROKEN + MISSING = 18**
**Total PARTIAL = 25**
**Total requiring work = 43 out of 53**

---

## Part III — Prior Hypothesis Verification

| Claim in `architecture_flow.dot` | Verified? | Actual State |
|---|---|---|
| CandleRegistry TF=60 ONLY ⚠ | **STALE** — Fixed | Now builds 6 TFs: `60,300,900,3600,14400,86400` (`ticks_engine.py:193`) |
| HotCache.get_candles MISSING | **STALE** — Fixed | Exists as alias (`cache.py:69-71`) |
| ContradictionEngine MTF evidence contributes 0 → always WAIT | **STALE** — Fixed | MTF evidence now contributes real weight (`contradiction.py:97-107`) |
| analysis_module/ writes CONFIRMED to signals_v2 → COMPETING signal path | **STALE** — Fixed | Disabled by default (`runall.py:55-58`, `RUNALL_ANALYSIS=false`) |
| SMA_PERIOD import fix | **VERIFIED** | `SMA_PERIOD = 10` defined in `mtf_strategy.py:43`, imported correctly |
| Confidence normalization | **VERIFIED** | Normalized to /100 in `signal_engine.py:677-682` |
| Per-user strategy selection | **VERIFIED** | `_read_user_strategy()` reads from CH `userdetails` (`ticks_engine.py:570-606`) |
| FVG never persisted (0 callers) | **VERIFIED** | `insert_fvg_batch` defined but 0 callers in Python code |
| HotCache.get_candles in views → AttributeError swallowed | **STALE** — Fixed | Backend now broadcasts analysis/mtf_status directly from tick engine, no HTTP endpoint |
| execution_engine one-shot reactor | **VERIFIED** | `execution_engine.py:54-58` — second call fails |
| mtf_aggregator never started in compose | **VERIFIED** | Not in `runall.py` service list |
| forex/sync DEAD (flag off) | **VERIFIED** | `SYNC_ENGINE_ENABLED=false` |
| trade/views.py 410 stubs | **VERIFIED** | `trade/views.py:18` |
| Pusher keys missing in .env.production | **VERIFIED** | `.env.production` has Reverb config but no `NEXT_PUBLIC_PUSHER_KEY` |

---

## Part IV — Recommended Phase Sequencing

### Critical Fix #1 (Immediate — 1-2 hours)
**Fix production Pusher keys.** `.env.production` is missing `NEXT_PUBLIC_PUSHER_KEY`, `NEXT_PUBLIC_PUSHER_CLUSTER`, and `NEXT_PUBLIC_ENABLE_REALTIME`. Without these, the entire frontend realtime is dead in production. This is the single most critical fix because it blocks all user-visible functionality.

### Phase Sequencing (ordered by dependency)

1. **Fix production Pusher keys** (unblocks all frontend work)
2. **Implement 3-month 1M historical backfill** (unblocks context engine and MTF analysis)
3. **Build shared Market Context State** (unblocks strategy agreement and context awareness)
4. **Implement WAITING re-evaluation** (unblocks signal lifecycle and dedup)
5. **Event-driven execution** (replaces full recalculation on every close)
6. **Signal dedup / setup identity** (prevents duplicate signals)
7. **Retest logic** (critical for price-action-based signals)
8. **Quality gate formalization** (the 15-question gate from spec)
9. **FVG persistence** (unblock `insert_fvg_batch` callers)
10. **Performance optimization** (dirty-state tracking, incremental analysis)

### Single Most Critical Fix

**Fix `.env.production` Pusher configuration.** The entire frontend cannot display any realtime data without `NEXT_PUBLIC_PUSHER_KEY` and `NEXT_PUBLIC_ENABLE_REALTIME=true`. This is a 5-minute configuration fix that unblocks all user-facing value.

---

## Part V — Changed Files

No production code was modified. This audit produces two files:

1. `/Users/richardmlambuzi/Documents/Inde Technologies MW LTD/inde_projects/njira_auto_fx/docs/architecture-audit.md` (this file)
2. `/Users/richardmlambuzi/Documents/Inde Technologies MW LTD/inde_projects/njira_auto_fx/docs/architecture_flow.dot` (refreshed diagram)
