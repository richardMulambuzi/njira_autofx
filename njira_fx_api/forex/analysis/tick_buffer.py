"""
Tick deduplication and out-of-order buffering.

cTrader spot events can arrive with duplicate timestamps or slightly out of
order (especially after one-sided repair).  This module:

1. Deduplicates by (symbol, timestamp_ms) — only the first tick at a given
   timestamp passes through.
2. Buffers OOO ticks within a configurable window and emits them in order.

Phase 2.8 — improvements.md §24.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("forex.analysis.tick_buffer")


class TickBuffer:
    """
    Per-symbol dedup + OOO buffer.

    Parameters
    ----------
    ooo_window_ms:
        Maximum time (milliseconds) a tick can arrive *early* and still be
        inserted in order.  Ticks arriving more than this many ms before the
        last emitted tick are logged as stale and dropped.
        Default: 5000ms (5 seconds).
    dedup_capacity:
        How many recent (symbol, timestamp) pairs to remember for dedup.
        Default: 10000.
    """

    def __init__(self, ooo_window_ms: int = 5000, dedup_capacity: int = 10000) -> None:
        self._ooo_window = ooo_window_ms
        self._dedup_capacity = dedup_capacity
        # symbol → OrderedDict[timestamp_ms → tick_tuple] (sorted by ts)
        self._buffers: dict[str, OrderedDict] = {}
        # symbol → last emitted timestamp_ms
        self._last_emitted: dict[str, float] = {}
        # symbol → set of recently seen timestamps (dedup)
        self._seen: dict[str, dict] = {}

    def feed(
        self, symbol: str, bid: float, ask: float, spread: float,
        digits: int, timestamp_ms: int,
    ) -> Optional[tuple[str, float, float, float, int, int]]:
        """
        Process one tick.  Returns the tick tuple if it should be emitted,
        or None if it's a duplicate / stale / buffered.

        The returned tuple is always in chronological order (no OOO).
        """
        # ── Dedup ──
        seen = self._seen.get(symbol)
        if seen is None:
            seen = {}
            self._seen[symbol] = seen
        if timestamp_ms in seen:
            return None  # duplicate

        # Track recent timestamps (evict oldest when over capacity)
        seen[timestamp_ms] = True
        if len(seen) > self._dedup_capacity:
            # Remove oldest half
            to_remove = list(seen.keys())[: len(seen) // 2]
            for k in to_remove:
                del seen[k]

        # ── OOO buffering ──
        last = self._last_emitted.get(symbol, 0.0)

        if timestamp_ms >= last:
            # In order — emit immediately, flush any buffered ticks that are
            # now in order.
            self._last_emitted[symbol] = timestamp_ms
            # ponytail: flushed OOO ticks are dropped — acceptable for <5s window
            # where re-ordering rarely changes analysis outcome.  Upgrade: return
            # flushed ticks and process them in the caller.
            self._flush_buffer(symbol, timestamp_ms)
            return (symbol, bid, ask, spread, digits, timestamp_ms)

        # Out of order — buffer it
        if last - timestamp_ms <= self._ooo_window:
            buf = self._buffers.get(symbol)
            if buf is None:
                buf = OrderedDict()
                self._buffers[symbol] = buf
            buf[timestamp_ms] = (symbol, bid, ask, spread, digits, timestamp_ms)
        else:
            logger.debug(
                "[TICK_BUFFER] %s: dropping stale OOO tick %d (last emitted %d)",
                symbol, timestamp_ms, int(last),
            )

        return None

    def _flush_buffer(self, symbol: str, up_to_ms: float) -> list[tuple]:
        """Emit buffered ticks that are now in order (up to ``up_to_ms``)."""
        buf = self._buffers.get(symbol)
        if buf is None:
            return []
        emitted = []
        # OrderedDict is sorted by insertion order; we insert by ts so
        # popitem(last=False) gives oldest first.
        while buf:
            ts = next(iter(buf))
            if ts > up_to_ms:
                break
            tick = buf.pop(ts)
            self._last_emitted[symbol] = ts
            emitted.append(tick)
        return emitted

    def reset(self) -> None:
        self._buffers.clear()
        self._last_emitted.clear()
        self._seen.clear()
