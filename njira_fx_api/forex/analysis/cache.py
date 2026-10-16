"""
Hot in-memory cache for closed candles.

Holds the last N candles per symbol per timeframe for analytical access
(market structure, liquidity, OB detection, etc.).  Never queried directly
from ClickHouse in the hot path — all reads go through this cache.

Eviction: oldest candles are dropped when the deque exceeds ``max_size``.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

logger = logging.getLogger("forex.analysis.cache")


class HotCache:
    """
    Per-symbol, per-timeframe candle cache.

    Parameters
    ----------
    max_size:
        Maximum closed candles kept per (symbol, timeframe) pair.
        Default 500 is enough for 200-period MA + structural analysis.
    """

    def __init__(self, max_size: int = 500) -> None:
        self._max_size = max_size
        # {symbol: {tf: deque[dict]}}
        self._data: dict[str, dict[int, deque]] = {}
        # {symbol: {tf: list[dict]}} — stable snapshot lists returned by get().
        #
        # get() used to build `list(deque)` per call, so every caller received a
        # NEW list object.  Downstream memos keyed on id() could therefore never
        # hit (and, worse, id() recycling made a stale hit possible).  Keeping one
        # list per (symbol, tf) and mutating it IN PLACE gives callers a stable
        # identity while staying pure for readers.  Readers must not mutate the
        # returned list (documented on get()).
        self._snap: dict[str, dict[int, list[dict]]] = {}

    def _snapshot(self, symbol: str, tf: int, deq: deque) -> list[dict]:
        """Return (creating if needed) the stable snapshot list for this key."""
        sym = self._snap.get(symbol)
        if sym is None:
            sym = self._snap[symbol] = {}
        snap = sym.get(tf)
        if snap is None:
            snap = sym[tf] = []
        # Refresh contents in place (identity preserved for memoisation).
        if len(snap) != len(deq):
            snap.clear()
            snap.extend(deq)
        else:
            # Same length: a bounded deque evicted the oldest and appended a new
            # one, so the tail differs.  Check cheaply before rewriting.
            changed = False
            for i in range(len(snap)):
                if snap[i] is not deq[i]:
                    changed = True
                    break
            if changed:
                snap.clear()
                snap.extend(deq)
        return snap

    def add(self, candle: dict[str, Any]) -> None:
        """
        Append a closed candle to the cache.

        The candle dict must have ``symbol`` and ``timeframe`` keys.
        """
        symbol = candle["symbol"]
        tf = int(candle["timeframe"])
        if symbol not in self._data:
            self._data[symbol] = {}
        if tf not in self._data[symbol]:
            self._data[symbol][tf] = deque(maxlen=self._max_size)
        self._data[symbol][tf].append(candle)
        # Invalidate the snapshot's length assumption (contents refresh lazily).
        snap = self._snap.get(symbol, {}).get(tf)
        if snap is not None:
            snap.clear()
            snap.extend(self._data[symbol][tf])

    def add_batch(self, candles: list[dict[str, Any]]) -> None:
        """Append multiple closed candles (e.g. from ClickHouse preload)."""
        for c in candles:
            self.add(c)

    def set_candles(self, symbol: str, tf: int, candles: list[dict]) -> None:
        """
        Replace the (symbol, tf) deque with ``candles`` (chronological order).

        Used by the backtest engine to preload REAL higher-timeframe bars that
        override the aggregated ones (the signal engine reads only via
        ``get``).  An empty list removes the (symbol, tf) entry.
        """
        tf = int(tf)
        if not candles:
            self._data.get(symbol, {}).pop(tf, None)
            self._snap.get(symbol, {}).pop(tf, None)
            return
        deq = deque(maxlen=self._max_size)
        deq.extend(candles)
        self._data.setdefault(symbol, {})[tf] = deq
        snap = self._snap.setdefault(symbol, {})[tf] = []
        snap.clear()
        snap.extend(deq)

    def get(self, symbol: str, tf: int, n: int = 0) -> list[dict[str, Any]]:
        """
        Return up to ``n`` most-recent closed candles for ``symbol``/``tf``.

        ``n=0`` returns all cached candles (up to ``max_size``).
        Candles are in chronological order (oldest first).

        The returned list is a STABLE, engine-owned snapshot — the same object
        across calls until the key changes, so callers may memoise on its
        identity.  It must be treated as READ-ONLY: mutating it corrupts the
        cache (use add()/set_candles() to change cache contents).
        """
        deq = self._data.get(symbol, {}).get(tf)
        if deq is None:
            return []
        if n > 0:
            return list(deq)[-n:]
        return self._snapshot(symbol, tf, deq)

    def get_candles(self, symbol: str, tf: int, limit: int = 0) -> list[dict]:
        """Return up to ``limit`` most-recent closed candles (alias of get with n=limit)."""
        return self.get(symbol, tf, limit)

    def latest(self, symbol: str, tf: int) -> Optional[dict[str, Any]]:
        """Return the most recently cached candle, or None."""
        deq = self._data.get(symbol, {}).get(tf)
        if deq is None or len(deq) == 0:
            return None
        return deq[-1]

    def symbols(self) -> list[str]:
        return list(self._data.keys())

    def timeframes(self, symbol: str) -> list[int]:
        return list(self._data.get(symbol, {}).keys())

    def count(self, symbol: str, tf: int) -> int:
        deq = self._data.get(symbol, {}).get(tf)
        return len(deq) if deq is not None else 0

    def reset(self) -> None:
        """Clear the entire cache (e.g. between backtest runs)."""
        self._data.clear()

    def reset_symbol(self, symbol: str) -> None:
        """Clear cache for a single symbol."""
        self._data.pop(symbol, None)
