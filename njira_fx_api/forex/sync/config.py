"""
SyncConfig — environment-driven tuning for the tick synchronization engine.

Reads every ``SYNC_*`` environment variable with spec defaults (tick-sync
spec §3.3/§3.4/§5.6/§10; plan T3). Invalid values fall back to the default
with a warning, mirroring ``TradingConfig.from_env()`` behavior
(``forex/broker/config.py``).

Canonical field names (used by the engine) are the task-spec names
(``window_max_ticks``, ``window_max_days``, ``gap_threshold_seconds`` ...);
the legacy plan names (``SYNC_MAX_TICKS``, ``SYNC_MAX_AGE``) are accepted as
env aliases.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta

logger = logging.getLogger("forex.sync.config")

#: Env var names that parse to booleans (everything else is False).
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})

_DEDUP_KEYS = frozenset({"ts_seq", "ts_price"})
_RECORD_LAYOUTS = frozenset({"full", "compact"})


def _read_env(name: str, aliases: tuple[str, ...] = ()) -> str | None:
    """First non-empty value among ``name`` and its aliases, else ``None``."""
    for key in (name, *aliases):
        raw = os.environ.get(key)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


def _env_int(
    name: str,
    default: int,
    aliases: tuple[str, ...] = (),
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _read_env(name, aliases)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r (expected integer) — using default %s", name, raw, default
        )
        return default
    if minimum is not None and value < minimum:
        logger.warning(
            "Invalid %s=%r (below minimum %s) — using default %s",
            name, raw, minimum, default,
        )
        return default
    if maximum is not None and value > maximum:
        logger.warning(
            "Invalid %s=%r (above maximum %s) — using default %s",
            name, raw, maximum, default,
        )
        return default
    return value


def _env_float(
    name: str,
    default: float,
    aliases: tuple[str, ...] = (),
    minimum: float | None = None,
) -> float:
    raw = _read_env(name, aliases)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r (expected number) — using default %s", name, raw, default
        )
        return default
    if minimum is not None and value < minimum:
        logger.warning(
            "Invalid %s=%r (below minimum %s) — using default %s",
            name, raw, minimum, default,
        )
        return default
    return value


def _env_bool(name: str, default: bool, aliases: tuple[str, ...] = ()) -> bool:
    raw = _read_env(name, aliases)
    if raw is None:
        return default
    return raw.lower() in _TRUE_VALUES


def _env_choice(
    name: str, default: str, choices: frozenset[str], aliases: tuple[str, ...] = ()
) -> str:
    raw = _read_env(name, aliases)
    if raw is None:
        return default
    if raw.lower() not in choices:
        logger.warning(
            "Invalid %s=%r (expected one of %s) — using default %r",
            name, raw, sorted(choices), default,
        )
        return default
    return raw.lower()


@dataclass(frozen=True)
class SyncConfig:
    """Immutable engine configuration.

    All values come from ``SYNC_*`` environment variables with spec defaults.
    ``from_env()`` is the canonical constructor; the dataclass itself is
    directly constructible for tests.
    """

    #: Feature flag (spec §10 Phase 0). When False the engine is dead code:
    #: ``SyncEngine.start()`` refuses to run (SyncError ENGINE_DISABLED).
    engine_enabled: bool = False  # SYNC_ENGINE_ENABLED
    #: Window tick-count boundary (spec §1.3: MAX_TICKS = 3,400,000).
    window_max_ticks: int = 3_400_000  # SYNC_WINDOW_MAX_TICKS / SYNC_MAX_TICKS
    #: Window age boundary in days (spec §1.3: MAX_AGE = 60 days).
    window_max_days: int = 60  # SYNC_WINDOW_MAX_DAYS / SYNC_MAX_AGE
    #: Segment capacity K (spec §3.4: default 100,000).
    segment_capacity: int = 100_000  # SYNC_SEGMENT_CAPACITY
    #: Record layout (spec §3.3): "full" (40 B/tick) | "compact" (24 B/tick).
    record_layout: str = "full"  # SYNC_RECORD_LAYOUT
    #: Maximum tracked symbols (spec §3.3: SYNC_MAX_SYMBOLS default 4).
    max_symbols: int = 4  # SYNC_MAX_SYMBOLS
    #: Dedup strategy (contracts §4.2): "ts_seq" prefers the provider seq when
    #: present and falls back to (ts, last_bits); "ts_price" always uses
    #: (ts, last_bits).
    dedup_key: str = "ts_seq"  # SYNC_DEDUP_KEY
    #: Gap threshold seconds (task T3; per-symbol override via
    #: ``gap_thresholds``). Effective threshold is
    #: ``max(3 × cadence, gap_threshold_seconds)`` (spec §5.5).
    gap_threshold_seconds: float = 5.0  # SYNC_GAP_THRESHOLD_SECONDS
    #: Absolute floor for the gap threshold (spec §5.5: SYNC_GAP_MIN_SECONDS).
    gap_min_seconds: float = 5.0  # SYNC_GAP_MIN_SECONDS
    #: Gap-timer polling interval in seconds (T6 worker: periodic check
    #: comparing last accepted ts vs now; spec §5.5 "gap timer armed per
    #: symbol"). Default 5 s.
    gap_check_interval: float = 5.0  # SYNC_GAP_CHECK_INTERVAL
    #: Bounded ingest queue size (spec §6.1: default 100,000).
    ingest_queue_size: int = 100_000  # SYNC_INGEST_QUEUE_SIZE
    #: Commit batch size (spec §6.2: default 1000).
    commit_batch_size: int = 1000  # SYNC_COMMIT_BATCH_SIZE
    #: Commit flush window in ms (spec §6.2: default 10 ms).
    commit_flush_ms: float = 10.0  # SYNC_COMMIT_FLUSH_MS
    #: Backfill page size (contracts §2.1 / spec §5.6: default 1000).
    backfill_batch_size: int = 1000  # SYNC_BACKFILL_BATCH_SIZE
    #: Provider reconnection cadence for backfill (SYNC_BACKFILL_RECONNECT_EVERY).
    backfill_reconnect_every: int = 2  # SYNC_BACKFILL_RECONNECT_EVERY
    #: Backfill request rate (spec §8: 5 req/s).
    backfill_rate: float = 5.0  # SYNC_BACKFILL_RATE
    #: Bounded history buffer during SYNCING_HISTORY (spec §5.5; T6 worker).
    history_buffer: int = 10_000  # SYNC_HISTORY_BUFFER
    #: Lower timestamp bound (epoch ms) for tick validation (spec §5.1).
    min_ts: int = 0  # SYNC_MIN_TS
    #: Future-skew allowance for tick validation (spec §5.1: 5 min).
    max_future_skew_seconds: float = 300.0  # SYNC_MAX_FUTURE_SKEW
    #: Log every Nth duplicate (spec §4.2: SYNC_DUP_LOG_RATE).
    dup_log_rate: int = 1000  # SYNC_DUP_LOG_RATE
    #: Retain the window across provider switches (spec §5.5: default true).
    keep_window_on_switch: bool = True  # SYNC_KEEP_WINDOW_ON_SWITCH
    #: Publish SyncEvents to subscribers (spec §5.4).
    publish_events: bool = True  # SYNC_PUBLISH_EVENTS
    #: Snapshot dir (spec §4.4 Phase 3; empty = disabled).
    snapshot_dir: str = ""  # SYNC_SNAPSHOT_DIR
    #: Serve /api/chart-data/ from the engine window (spec §7.2 Phase 2).
    serve_chart_data: bool = False  # SYNC_SERVE_CHART_DATA
    #: Serve the MTF strategy from the engine window (spec §7.2 Phase 3).
    serve_strategy: bool = False  # SYNC_SERVE_STRATEGY
    #: Per-symbol gap-threshold overrides (seconds). Programmatic only.
    gap_thresholds: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def max_age(self) -> timedelta:
        """Window age boundary as a timedelta."""
        return timedelta(days=self.window_max_days)

    def gap_threshold_for(self, symbol: str) -> float:
        """Effective gap threshold (seconds) for ``symbol``.

        A per-symbol override (``gap_thresholds``) wins; otherwise the global
        ``gap_threshold_seconds`` applies. The engine still floors the
        effective threshold at ``gap_min_seconds`` (spec §5.5).
        """
        return self.gap_thresholds.get(symbol, self.gap_threshold_seconds)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> SyncConfig:
        """Build a :class:`SyncConfig` from the process environment.

        Every ``SYNC_*`` variable is read with the spec default; invalid
        values log a warning and fall back (mirrors ``TradingConfig``).
        """
        gap_thresholds: dict[str, float] = {}
        raw_overrides = os.environ.get("SYNC_GAP_THRESHOLDS")
        if raw_overrides:
            # Format: "EURUSD=3.5,GBPUSD=7.0" — per-symbol overrides.
            for chunk in raw_overrides.split(","):
                chunk = chunk.strip()
                if not chunk or "=" not in chunk:
                    continue
                sym, _, value = chunk.partition("=")
                try:
                    gap_thresholds[sym.strip().upper()] = float(value.strip())
                except ValueError:
                    logger.warning(
                        "Invalid SYNC_GAP_THRESHOLDS entry %r — ignored", chunk
                    )

        return cls(
            engine_enabled=_env_bool(
                "SYNC_ENGINE_ENABLED", False, aliases=("SYNC_ENGINE_ENABLED",)
            ),
            window_max_ticks=_env_int(
                "SYNC_WINDOW_MAX_TICKS",
                3_400_000,
                aliases=("SYNC_MAX_TICKS",),
                minimum=1,
            ),
            window_max_days=_env_int(
                "SYNC_WINDOW_MAX_DAYS",
                60,
                aliases=("SYNC_MAX_AGE",),
                minimum=1,
            ),
            segment_capacity=_env_int(
                "SYNC_SEGMENT_CAPACITY", 100_000, minimum=1
            ),
            record_layout=_env_choice(
                "SYNC_RECORD_LAYOUT", "full", _RECORD_LAYOUTS
            ),
            max_symbols=_env_int("SYNC_MAX_SYMBOLS", 4, minimum=1),
            dedup_key=_env_choice("SYNC_DEDUP_KEY", "ts_seq", _DEDUP_KEYS),
            gap_threshold_seconds=_env_float(
                "SYNC_GAP_THRESHOLD_SECONDS", 5.0, minimum=0.1
            ),
            gap_min_seconds=_env_float("SYNC_GAP_MIN_SECONDS", 5.0, minimum=0.1),
            gap_check_interval=_env_float(
                "SYNC_GAP_CHECK_INTERVAL", 5.0, minimum=0.1
            ),
            ingest_queue_size=_env_int("SYNC_INGEST_QUEUE_SIZE", 100_000, minimum=1),
            commit_batch_size=_env_int("SYNC_COMMIT_BATCH_SIZE", 1000, minimum=1),
            commit_flush_ms=_env_float("SYNC_COMMIT_FLUSH_MS", 10.0, minimum=1.0),
            backfill_batch_size=_env_int(
                "SYNC_BACKFILL_BATCH_SIZE", 1000, minimum=1
            ),
            backfill_reconnect_every=_env_int(
                "SYNC_BACKFILL_RECONNECT_EVERY", 2, minimum=1
            ),
            backfill_rate=_env_float("SYNC_BACKFILL_RATE", 5.0, minimum=0.1),
            history_buffer=_env_int("SYNC_HISTORY_BUFFER", 10_000, minimum=1),
            min_ts=_env_int("SYNC_MIN_TS", 0),
            max_future_skew_seconds=_env_float(
                "SYNC_MAX_FUTURE_SKEW", 300.0, minimum=0.0
            ),
            dup_log_rate=_env_int("SYNC_DUP_LOG_RATE", 1000, minimum=1),
            keep_window_on_switch=_env_bool("SYNC_KEEP_WINDOW_ON_SWITCH", True),
            publish_events=_env_bool("SYNC_PUBLISH_EVENTS", True),
            snapshot_dir=os.environ.get("SYNC_SNAPSHOT_DIR", "").strip(),
            serve_chart_data=_env_bool("SYNC_SERVE_CHART_DATA", False),
            serve_strategy=_env_bool("SYNC_SERVE_STRATEGY", False),
            gap_thresholds=gap_thresholds,
        )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return (
            f"SyncConfig(engine_enabled={self.engine_enabled}, "
            f"window_max_ticks={self.window_max_ticks}, "
            f"window_max_days={self.window_max_days}, "
            f"segment_capacity={self.segment_capacity}, "
            f"record_layout={self.record_layout!r}, dedup_key={self.dedup_key!r})"
        )


__all__ = ["SyncConfig"]
