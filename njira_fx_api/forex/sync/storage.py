"""
SegmentStore — in-memory segmented sorted arrays (numpy) per symbol.

Spec §3-4 / contracts §4. One instance per symbol; the engine holds
``dict[symbol → SegmentStore]`` (contracts §4).

Structure (spec §3.4):
  * A ``collections.deque`` of segments; each segment is a pre-allocated
    numpy structured array of capacity ``K`` (default 100,000) with a fill
    pointer. Appends write at ``arr[fill]`` — O(1) amortized, no copies.
  * The ``first_ts`` boundaries are cached in a numpy array for ``bisect`` —
    O(log S) segment locate (S ≈ 34 segments at 3.4M ticks).
  * Late-tick insert: ``searchsorted`` + one memmove within one segment
    (O(K)); split when full (upper half → new segment).

Invariants (contracts §4.2, binding):
  * I1 Ordering: the window is always sorted by ``ts`` — ``ts[i] <= ts[i+1]``.
    Physical position NEVER determines logical position.
  * I2 Dedup: key ``(ts_ms, seq)`` when ``seq != -1`` else
    ``(ts_ms, float_bits(last))``. Duplicates counted, never stored twice.
  * I3 Segments: every segment ``count <= K``; sorted, non-overlapping,
    time-contiguous; split preserves invariants.
  * I4 Dual-boundary eviction: count and age — whichever hits first wins.
  * I5 Single-writer: mutations only from the engine writer thread; readers
    via the shared ``threading.RLock``.
  * I6 Out-of-window: a tick older than the oldest segment is dropped and
    counted — never stored.
  * I7 Efficiency: locate O(log S), append O(1) amortized, late insert O(K),
    dedup O(log K), query O(m + S), evict O(1)/segment (spec §3.5).
"""
from __future__ import annotations

import bisect
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Sequence

import numpy as np

from forex.broker.models import MarketTick
from forex.sync.normalizer import float_bits, ms_to_datetime, to_epoch_ms

logger = logging.getLogger("forex.sync.storage")

#: Canonical record layout (spec §3.3) — FULL: 40 B/tick.
TICK_DTYPE = np.dtype(
    [
        ("ts", "int64"),  # epoch milliseconds UTC — THE ordering key
        ("bid", "float64"),
        ("ask", "float64"),
        ("last", "float64"),
        ("seq", "int64"),  # provider tick id/sequence; -1 when unavailable
    ]
)

#: COMPACT layout (spec §3.3) — 24 B/tick (bid/ask dropped).
TICK_DTYPE_COMPACT = np.dtype(
    [
        ("ts", "int64"),
        ("last", "float64"),
        ("seq", "int64"),
    ]
)


def make_tick_dtype(record_layout: str = "full") -> np.dtype:
    """Return the numpy dtype for a record layout (spec §3.3)."""
    if record_layout == "compact":
        return TICK_DTYPE_COMPACT
    return TICK_DTYPE


# ---------------------------------------------------------------------------
# DTOs (contracts §4.1 — api-architect-owned shapes)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InsertResult:
    """Result of a batch append (spec §5.2 field list)."""

    inserted: int
    duplicates: int
    out_of_window: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    total_count: int


class LateInsertOutcome(str, Enum):
    """Outcome of a single late-tick insert (spec §4.1 return values)."""

    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    OUT_OF_WINDOW = "out_of_window"


@dataclass(frozen=True)
class EvictionResult:
    """Result of an eviction pass (contracts §4.1)."""

    evicted_segments: int
    evicted_ticks: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    total_count: int


@dataclass(frozen=True)
class WindowStats:
    """Per-symbol window statistics (spec §5.2 field list)."""

    count: int
    oldest_ts: datetime | None
    newest_ts: datetime | None
    segment_count: int
    evictions: int
    duplicates: int


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------
class _Segment:
    """One pre-allocated numpy segment with a fill pointer (spec §3.4)."""

    __slots__ = ("arr", "fill", "capacity")

    def __init__(self, dtype: np.dtype, capacity: int) -> None:
        self.arr = np.empty(capacity, dtype=dtype)
        self.fill = 0
        self.capacity = capacity

    @property
    def first_ts(self) -> int | None:
        return int(self.arr["ts"][0]) if self.fill else None

    @property
    def last_ts(self) -> int | None:
        return int(self.arr["ts"][self.fill - 1]) if self.fill else None


# ---------------------------------------------------------------------------
# SegmentStore
# ---------------------------------------------------------------------------
class SegmentStore:
    """One symbol's segmented sorted rolling window (spec §3-4).

    Single-writer (engine thread only, spec §6.1); readers via the shared
    ``RLock``. ``append``/``insert_late`` accept :class:`MarketTick` objects
    OR mappings with the canonical fields (``ts``/``timestamp``, ``bid``,
    ``ask``, ``last``, optional ``seq``) — the engine passes mappings so the
    provider ``seq`` flows into the storage record (contracts §3 composition
    rule).
    """

    def __init__(
        self,
        symbol: str,
        *,
        capacity: int = 100_000,
        record_layout: str = "full",
        provider: str = "",
        dedup_key: str = "ts_seq",
        dup_log_rate: int = 1000,
    ) -> None:
        self.symbol = symbol.upper()
        #: Provider name used when reconstructing MarketTick values (updated
        #: by the engine on provider switch).
        self.provider = provider
        self._capacity = int(capacity)
        self._dtype = make_tick_dtype(record_layout)
        self._dedup_key = dedup_key
        self._dup_log_rate = max(1, int(dup_log_rate))
        self._segments: deque[_Segment] = deque()
        self._boundaries = np.empty(0, dtype="int64")
        self._total = 0
        self._evictions = 0
        self._duplicates = 0
        self._out_of_window = 0
        #: Shared reader lock (spec §6.2): writer takes exclusive, readers
        #: take shared. RLock is re-entrant so nested reads are safe.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API (contracts §4)
    # ------------------------------------------------------------------
    def append(self, ticks: Sequence[object]) -> InsertResult:
        """Commit a batch of ticks (spec §4.1 append path).

        Ticks with ``ts >= active.last_ts`` take the O(1) amortized
        fill-pointer path with tail dedup; any tick older than the active
        segment's last_ts is routed internally to the late-insert path.
        Deduped/out-of-window ticks are counted, never stored (spec §4.2).
        """
        with self._lock:
            inserted = duplicates = out_of_window = 0
            for item in ticks:
                rec = self._extract_record(item)
                outcome = self._append_one(rec)
                if outcome == "inserted":
                    inserted += 1
                elif outcome == "duplicate":
                    duplicates += 1
                else:
                    out_of_window += 1
            return InsertResult(
                inserted=inserted,
                duplicates=duplicates,
                out_of_window=out_of_window,
                oldest_ts=self.oldest_ts(),
                newest_ts=self.newest_ts(),
                total_count=self._total,
            )

    def insert_late(self, tick: object) -> LateInsertOutcome:
        """Insert one tick with ``ts < active.last_ts`` (spec §4.1 late path).

        Locate segment O(log S) via the boundary index; dedup check O(log K)
        on ``searchsorted``; O(K) memmove insert; split the segment when full.
        Returns ``INSERTED`` | ``DUPLICATE`` | ``OUT_OF_WINDOW``.
        """
        with self._lock:
            rec = self._extract_record(tick)
            outcome = self._insert_late_rec(rec)
            if outcome == "inserted":
                self._total += 1
            return outcome

    def query_range(
        self,
        start: datetime | int,
        end: datetime | int,
        limit: int | None = None,
    ) -> list[MarketTick]:
        """Chronological slice of ``[start, end]``, oldest first (spec §5.2).

        O(m + S): slice filled portions of ≤ S segments, concatenate (spec
        §3.5). ``limit`` applies to the NEWEST end — when set, the newest
        ``limit`` ticks of the window are returned.
        """
        with self._lock:
            start_ms = _coerce_ts(start)
            end_ms = _coerce_ts(end)
            if start_ms > end_ms:
                raise ValueError("query_range: start > end")
            parts: list[np.ndarray] = []
            for seg in self._segments:
                if seg.fill == 0:
                    continue
                if seg.last_ts < start_ms:
                    continue
                if seg.first_ts > end_ms:
                    break  # segments are time-sorted — nothing further matches
                lo = int(np.searchsorted(seg.arr["ts"][: seg.fill], start_ms, side="left"))
                hi = int(np.searchsorted(seg.arr["ts"][: seg.fill], end_ms, side="right"))
                if lo < hi:
                    parts.append(seg.arr[lo:hi])
            if parts:
                arr = np.concatenate(parts)
            else:
                arr = np.empty(0, dtype=self._dtype)
            if limit is not None and limit > 0 and len(arr) > limit:
                arr = arr[-limit:]  # NEWEST end (contracts §4)
            return [self._to_tick(rec) for rec in arr]

    def snapshot(self) -> list[MarketTick]:
        """Materialized chronological copy of the whole window (spec §6.3).

        O(n), rare. The consumer-visible immutable view.
        """
        with self._lock:
            parts = [seg.arr[: seg.fill] for seg in self._segments if seg.fill]
            if parts:
                arr = np.concatenate(parts)
            else:
                arr = np.empty(0, dtype=self._dtype)
            return [self._to_tick(rec) for rec in arr]

    def stats(self) -> WindowStats:
        """WindowStats: count, oldest_ts, newest_ts, segment_count,
        evictions, duplicates (spec §5.2)."""
        with self._lock:
            return WindowStats(
                count=self._total,
                oldest_ts=self.oldest_ts(),
                newest_ts=self.newest_ts(),
                segment_count=sum(1 for seg in self._segments if seg.fill),
                evictions=self._evictions,
                duplicates=self._duplicates,
            )

    def evict_older_than(self, ts: datetime | int) -> EvictionResult:
        """Age boundary (spec §4.3): drop oldest segments while
        ``oldest_segment.last_ts < ts``. Segment-granular; the age boundary
        is exact to within one segment's span (state in ``stats()``)."""
        with self._lock:
            ts_ms = _coerce_ts(ts)
            evicted_segments = 0
            evicted_ticks = 0
            while self._segments:
                seg = self._segments[0]
                if seg.fill == 0 or seg.last_ts >= ts_ms:
                    break
                evicted_ticks += seg.fill
                evicted_segments += 1
                self._segments.popleft()
            if evicted_segments:
                self._total -= evicted_ticks
                self._evictions += evicted_segments
                self._rebuild_boundaries()
            return EvictionResult(
                evicted_segments=evicted_segments,
                evicted_ticks=evicted_ticks,
                oldest_ts=self.oldest_ts(),
                newest_ts=self.newest_ts(),
                total_count=self._total,
            )

    def evict_to_max_count(self, n: int) -> EvictionResult:
        """Count boundary (spec §4.3): drop oldest segments while
        ``total_count > n``. O(1) per segment ``popleft()`` (spec §3.5)."""
        with self._lock:
            n = int(n)
            evicted_segments = 0
            evicted_ticks = 0
            while self._total > n and self._segments:
                seg = self._segments[0]
                if seg.fill == 0:
                    self._segments.popleft()
                    continue
                evicted_ticks += seg.fill
                evicted_segments += 1
                self._total -= seg.fill
                self._segments.popleft()
            if evicted_segments:
                self._evictions += evicted_segments
                self._rebuild_boundaries()
            return EvictionResult(
                evicted_segments=evicted_segments,
                evicted_ticks=evicted_ticks,
                oldest_ts=self.oldest_ts(),
                newest_ts=self.newest_ts(),
                total_count=self._total,
            )

    def clear(self) -> None:
        """Drop the whole window (provider-switch path when
        ``SYNC_KEEP_WINDOW_ON_SWITCH=false``, spec §5.5)."""
        with self._lock:
            self._segments.clear()
            self._boundaries = np.empty(0, dtype="int64")
            self._total = 0

    def __len__(self) -> int:
        """Total stored tick count (== ``stats().count``)."""
        return self._total

    # ------------------------------------------------------------------
    # Timestamp helpers (public for engine/status use)
    # ------------------------------------------------------------------
    def oldest_ts(self) -> datetime | None:
        """Oldest stored tick timestamp (tz-aware UTC) or ``None``."""
        for seg in self._segments:
            if seg.fill:
                return ms_to_datetime(int(seg.arr["ts"][0]))
        return None

    def newest_ts(self) -> datetime | None:
        """Newest stored tick timestamp (tz-aware UTC) or ``None``."""
        for seg in reversed(self._segments):
            if seg.fill:
                return ms_to_datetime(int(seg.arr["ts"][seg.fill - 1]))
        return None

    # ------------------------------------------------------------------
    # Internals (single-writer; callers hold the lock)
    # ------------------------------------------------------------------
    def _extract_record(self, item: object) -> tuple[int, float, float, float, int]:
        """Normalize an input item to ``(ts_ms, bid, ask, last, seq)``.

        Accepts :class:`MarketTick` (seq = -1) or a mapping with the
        canonical fields (``ts`` or ``timestamp``, ``bid``/``ask``/``last``,
        optional ``seq``).
        """
        if isinstance(item, MarketTick):
            ts = to_epoch_ms(item.timestamp)
            if ts is None:
                raise ValueError(f"MarketTick timestamp invalid: {item.timestamp!r}")
            return (ts, float(item.bid), float(item.ask), float(item.last), -1)
        if isinstance(item, Mapping):
            if "ts" in item:
                ts = int(item["ts"])
            else:
                ts = to_epoch_ms(item.get("timestamp"))
            if ts is None:
                raise ValueError(f"record missing valid ts/timestamp: {item!r}")
            last = float(item["last"])
            bid = float(item.get("bid", last))
            ask = float(item.get("ask", last))
            seq = int(item.get("seq", -1))
            return (ts, bid, ask, last, seq)
        raise TypeError(
            f"SegmentStore input must be MarketTick or mapping, got {type(item).__name__}"
        )

    def _append_one(self, rec: tuple[int, float, float, float, int]) -> str:
        """Append one record; returns 'inserted' | 'duplicate' |
        'out_of_window'. Caller holds the lock."""
        ts = rec[0]
        if not self._segments:
            self._segments.append(_Segment(self._dtype, self._capacity))
            self._rebuild_boundaries()
            self._write(self._segments[-1], 0, rec)
            self._total += 1
            return "inserted"

        active = self._segments[-1]
        if active.fill and ts < active.arr["ts"][active.fill - 1]:
            # Late tick — route to the late-insert path (spec §4.1).
            outcome = self._insert_late_rec(rec)
            if outcome == "inserted":
                self._total += 1
            return outcome

        # Tail dedup: duplicates at the live boundary are the only realistic
        # case (spec §4.1). Scan the equal-ts tail for the dedup key.
        if active.fill and ts == active.arr["ts"][active.fill - 1]:
            key = self._key_from_rec(rec)
            i = active.fill - 1
            while i >= 0 and active.arr["ts"][i] == ts:
                if self._matches(active.arr[i], key):
                    self._duplicates += 1
                    self._log_dup(ts)
                    return "duplicate"
                i -= 1

        if active.fill == self._capacity:
            active = _Segment(self._dtype, self._capacity)
            self._segments.append(active)
            self._rebuild_boundaries()
        self._write(active, active.fill, rec)
        self._total += 1
        return "inserted"

    def _insert_late_rec(self, rec: tuple[int, float, float, float, int]) -> str:
        """Late-insert path (spec §4.1). Caller holds the lock."""
        ts = rec[0]
        if not self._segments:
            self._out_of_window += 1
            return "out_of_window"
        if ts < self._segments[0].arr["ts"][0]:
            self._out_of_window += 1
            return "out_of_window"

        idx = bisect.bisect_right(self._boundaries, ts) - 1
        if idx < 0:
            self._out_of_window += 1
            return "out_of_window"
        seg = self._segments[idx]

        pos = int(np.searchsorted(seg.arr["ts"][: seg.fill], ts, side="left"))
        key = self._key_from_rec(rec)
        if pos < seg.fill and seg.arr["ts"][pos] == ts:
            # Scan the equal-ts block for the dedup key (spec §4.2).
            end = pos
            while end < seg.fill and seg.arr["ts"][end] == ts:
                end += 1
            for i in range(pos, end):
                if self._matches(seg.arr[i], key):
                    self._duplicates += 1
                    self._log_dup(ts)
                    return "duplicate"

        if seg.fill == self._capacity:
            # Split: move the upper half to a new segment (spec §4.1).
            mid = seg.fill // 2
            new_seg = _Segment(self._dtype, self._capacity)
            new_seg.arr[: seg.fill - mid] = seg.arr[mid : seg.fill]
            new_seg.fill = seg.fill - mid
            seg.fill = mid
            self._segments.insert(idx + 1, new_seg)
            self._rebuild_boundaries()
            if ts > seg.arr["ts"][seg.fill - 1]:
                seg = new_seg
                pos = int(
                    np.searchsorted(seg.arr["ts"][: seg.fill], ts, side="left")
                )

        # O(K) memmove insert (spec §4.1).
        seg.arr[pos + 1 : seg.fill + 1] = seg.arr[pos : seg.fill]
        self._write(seg, pos, rec)
        return "inserted"

    def _write(self, seg: _Segment, idx: int, rec: tuple[int, float, float, float, int]) -> None:
        """Write one record into a segment at ``idx`` and bump the fill."""
        ts, bid, ask, last, seq = rec
        names = seg.arr.dtype.names
        seg.arr["ts"][idx] = ts
        if "bid" in names:
            seg.arr["bid"][idx] = bid
            seg.arr["ask"][idx] = ask
        seg.arr["last"][idx] = last
        seg.arr["seq"][idx] = seq
        seg.fill += 1

    def _key_from_rec(self, rec: tuple[int, float, float, float, int]) -> tuple[int, int]:
        """Dedup key for an incoming record (spec §4.2)."""
        ts, _bid, _ask, last, seq = rec
        if self._dedup_key == "ts_price" or seq == -1:
            return (ts, float_bits(last))
        return (ts, seq)

    def _matches(self, stored: np.void, key: tuple[int, int]) -> bool:
        """True when a stored record matches the dedup key (spec §4.2)."""
        stored_ts = int(stored["ts"])
        seq = int(stored["seq"])
        if self._dedup_key == "ts_price" or seq == -1:
            return stored_ts == key[0] and float_bits(float(stored["last"])) == key[1]
        return stored_ts == key[0] and seq == key[1]

    def _log_dup(self, ts: int) -> None:
        if self._duplicates % self._dup_log_rate == 0:
            logger.info(
                "sync storage %s: duplicate tick at ts=%d (total dups=%d)",
                self.symbol,
                ts,
                self._duplicates,
            )

    def _rebuild_boundaries(self) -> None:
        """Recompute the ``first_ts`` boundary cache (O(S), S ≈ 34)."""
        firsts = [int(seg.arr["ts"][0]) for seg in self._segments if seg.fill]
        self._boundaries = np.array(firsts, dtype="int64")

    def _to_tick(self, rec: np.void) -> MarketTick:
        """Reconstruct a :class:`MarketTick` from a storage record."""
        ts = int(rec["ts"])
        last = float(rec["last"])
        if "bid" in rec.dtype.names:
            bid = float(rec["bid"])
            ask = float(rec["ask"])
        else:
            bid = ask = last  # COMPACT layout degrades to last-price only
        return MarketTick(
            provider=self.provider,
            symbol=self.symbol,
            bid=bid,
            ask=ask,
            last=last,
            timestamp=ms_to_datetime(ts),
        )

    # ------------------------------------------------------------------
    # Test/verification helpers
    # ------------------------------------------------------------------
    def _all_ts(self) -> np.ndarray:
        """All stored timestamps as a flat int64 array (chronological).

        Internal — used by the unit self-check to verify the I1 ordering
        invariant without materializing MarketTick objects.
        """
        with self._lock:
            parts = [seg.arr["ts"][: seg.fill] for seg in self._segments if seg.fill]
            if not parts:
                return np.empty(0, dtype="int64")
            return np.concatenate(parts)


def _coerce_ts(value: datetime | int) -> int:
    """Coerce a query boundary to epoch ms (contracts §1.2: tz-aware UTC)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected — use tz-aware UTC")
        return int(value.timestamp() * 1000)
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid timestamp")
    if isinstance(value, int):
        return value
    raise TypeError(f"timestamp must be datetime or int, got {type(value).__name__}")


__all__ = [
    "EvictionResult",
    "InsertResult",
    "LateInsertOutcome",
    "SegmentStore",
    "TICK_DTYPE",
    "TICK_DTYPE_COMPACT",
    "WindowStats",
    "make_tick_dtype",
]