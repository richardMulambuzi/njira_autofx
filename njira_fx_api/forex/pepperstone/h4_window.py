"""
Strict 300-bar H4 window with "Latest IN, Last OUT" eviction.

One window per symbol. It is backed by ``collections.deque(maxlen=300)`` so an
append always evicts the oldest bar automatically: the newest bar comes IN at
the tail, the last (oldest) bar goes OUT at the head. That single structure
gives the invariant the design asks for with no manual trimming and no
off-by-one:

    exactly 300 bars at all times = 1 forming + 299 closed
    never a stale snapshot, never a gap, never a corrupt bar, never > 300

Ingest rules (H4 = 4 hours = 14_400 s):

    ts == last.ts          overwrite the tail in place (forming bar), no eviction
    ts == last.ts + 4h     append (Latest IN), oldest evicted (Last OUT)
    ts >  last.ts + 4h     gap: record it, flag degraded, alert; never pad
    ts <  last.ts          stale: ignore and count, no eviction

Gaps are closed by re-requesting the missing bars from history and merging them
through :meth:`H4Window.backfill`. Nothing is ever fabricated to hide a hole,
and a hole is never accepted silently.

Intended reuse: the transport (``WindowSession``) subscribes to spots then live
H4 trendbars, hands every decoded bar to :meth:`H4Window.ingest`, and hands any
reported gap to the history API, whose result goes to :meth:`H4Window.backfill`.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from datetime import date, timedelta
from typing import Any, Iterable, Optional

# Locked design parameters (do not retune without a spec change).
H4_SECONDS = 14_400
WINDOW_SIZE = 300
BACKFILL_CAL_DAYS = 91
H4_BARS_PER_TRADING_DAY = 6
MAX_WEEKDAYS_PER_MONTH = 23

_MS = 1_000
_GRID_MS = H4_SECONDS * _MS
_REQUIRED_FIELDS = ("epoch", "open", "high", "low", "close")


def align_to_h4(epoch_ms: int) -> int:
    """Snap a timestamp down to the open of the H4 bar that contains it.

    One market clock: every symbol derives its bar boundary from the same
    timestamp grid, so all windows roll at the same instant.
    """
    value = int(epoch_ms)
    return value - (value % _GRID_MS)


def backfill_start_ms(now_ms: int) -> int:
    """Start of the backfill window: 91 calendar days before ``now_ms``.

    91 days yields ~60-63 trading days, enough to slice the newest 300 bars
    even after Christmas/Easter/bank-holiday losses. 70 days would yield 50
    weekdays only with zero holidays.
    """
    return int(now_ms) - BACKFILL_CAL_DAYS * 86_400 * _MS


def is_trading_day(day: date) -> bool:
    """Pepperstone is a 5-day market: Mon-Fri only, weekends produce no bars."""
    return day.weekday() < 5


def trading_days_between(start: date, end: date) -> int:
    """Count Mon-Fri trading days in the inclusive range ``start..end``."""
    if end < start:
        return 0
    return sum(
        1
        for offset in range((end - start).days + 1)
        if is_trading_day(start + timedelta(days=offset))
    )


def trading_days_covered(bars: int) -> float:
    """Trading days represented by ``bars`` H4 candles (6 per Mon-Fri day)."""
    return bars / H4_BARS_PER_TRADING_DAY


class H4Window:
    """One symbol's live 300-bar H4 window.

    A single writer (the reactor callback thread) drives :meth:`ingest`; any
    thread may read via :meth:`snapshot`. The lock is cheap and keeps the class
    safe if a worker ever writes back a merged history batch.
    """

    def __init__(self, symbol: str = "", window_size: int = WINDOW_SIZE) -> None:
        self.symbol = symbol
        self._maxlen = max(1, int(window_size))
        self._bars: deque = deque(maxlen=self._maxlen)
        self._lock = threading.Lock()
        self._degraded = False
        self._alerts: list = []
        self._quarantined: list = []
        self._gaps: list = []
        self._counts = {
            "overwrite": 0,
            "append": 0,
            "gap": 0,
            "stale": 0,
            "quarantined": 0,
        }

    def ingest(self, bar: Any) -> str:
        """Feed one live bar and return the action taken.

        One of ``overwrite``, ``append``, ``gap``, ``stale`` or ``quarantined``.
        """
        clean, reason = self._validate(bar)
        with self._lock:
            if clean is None:
                self._quarantine(bar, reason)
                return "quarantined"

            if not self._bars:
                self._bars.append(clean)
                self._counts["append"] += 1
                return "append"

            last_ts = self._bars[-1]["epoch"]
            ts = clean["epoch"]

            if ts == last_ts:
                self._bars[-1] = clean
                self._counts["overwrite"] += 1
                return "overwrite"

            if ts == last_ts + _GRID_MS:
                self._bars.append(clean)
                self._counts["append"] += 1
                return "append"

            if ts > last_ts + _GRID_MS:
                self._record_gap(last_ts + _GRID_MS, ts)
                self._bars.append(clean)
                self._counts["gap"] += 1
                return "gap"

            self._counts["stale"] += 1
            return "stale"

    def backfill(self, bars: Iterable[Any]) -> int:
        """Merge history bars (any order) and re-trim to the newest window.

        Returns the number of bars accepted. Invalid bars are quarantined, not
        merged. Bars newer than the current tail still land at the tail, so a
        backfill can never move the window backwards.
        """
        accepted = 0
        with self._lock:
            merged = {bar["epoch"]: bar for bar in self._bars}
            for raw in bars:
                clean, reason = self._validate(raw)
                if clean is None:
                    self._quarantine(raw, reason)
                    continue
                merged[clean["epoch"]] = clean
                accepted += 1
            ordered = [merged[key] for key in sorted(merged)]
            self._bars.clear()
            self._bars.extend(ordered)
            self._prune_gaps()
        return accepted

    def sweep(self) -> int:
        """Drop off-grid, duplicate or out-of-order bars. Returns how many.

        Bars are quarantined rather than silently dropped, so a corrupt feed is
        visible in metrics instead of papered over.
        """
        dropped = 0
        with self._lock:
            kept: list = []
            seen: set = set()
            prev: Optional[int] = None
            for bar in self._bars:
                ts = bar["epoch"]
                if ts % _GRID_MS or ts in seen or (prev is not None and ts <= prev):
                    self._quarantine(bar, "sweep: off-grid, duplicate or out-of-order")
                    dropped += 1
                    continue
                kept.append(bar)
                seen.add(ts)
                prev = ts
            if dropped:
                self._bars.clear()
                self._bars.extend(kept)
        return dropped

    def snapshot(self) -> tuple:
        """Immutable copy, oldest first. Safe to hand to a worker thread."""
        with self._lock:
            return tuple(dict(bar) for bar in self._bars)

    def latest(self) -> Optional[dict]:
        """The forming bar (or ``None`` when the window is empty)."""
        with self._lock:
            return dict(self._bars[-1]) if self._bars else None

    def drain_alerts(self) -> list:
        """Take and clear the pending gap/quarantine alerts."""
        with self._lock:
            alerts, self._alerts = self._alerts, []
            return alerts

    def is_ready(self) -> bool:
        """True when the window is full (300 bars) and not degraded."""
        with self._lock:
            return len(self._bars) == self._maxlen and not self._degraded

    @property
    def degraded(self) -> bool:
        with self._lock:
            return self._degraded

    def get_metrics(self) -> dict:
        with self._lock:
            first = self._bars[0]["epoch"] if self._bars else None
            last = self._bars[-1]["epoch"] if self._bars else None
            return {
                "symbol": self.symbol,
                "bars": len(self._bars),
                "window_size": self._maxlen,
                "forming": 1 if self._bars else 0,
                "closed": max(0, len(self._bars) - 1),
                "trading_days": trading_days_covered(len(self._bars)),
                "first_epoch": first,
                "last_epoch": last,
                "degraded": self._degraded,
                "open_gaps": len(self._gaps),
                **self._counts,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._bars)

    def _validate(self, bar: Any):
        """Return ``(clean_bar, None)`` or ``(None, reason)``."""
        if not isinstance(bar, dict):
            return None, "not a mapping"
        missing = [key for key in _REQUIRED_FIELDS if key not in bar]
        if missing:
            return None, "missing " + ",".join(missing)
        try:
            epoch = align_to_h4(bar["epoch"])
            open_ = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            volume = float(bar.get("volume", 0) or 0)
        except (TypeError, ValueError):
            return None, "non-numeric field"
        for value in (open_, high, low, close, volume):
            if math.isnan(value) or math.isinf(value):
                return None, "non-finite field"
        if epoch <= 0:
            return None, "non-positive epoch"
        if volume < 0:
            return None, "negative volume"
        if high < low or not (low <= open_ <= high) or not (low <= close <= high):
            return None, "OHLC inconsistent"
        return {
            "epoch": epoch,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }, None

    def _quarantine(self, bar: Any, reason: Optional[str]) -> None:
        """Caller must hold the lock."""
        self._counts["quarantined"] += 1
        self._quarantined.append((bar, reason))
        self._alerts.append(
            {"kind": "quarantined", "symbol": self.symbol, "reason": reason}
        )

    def _record_gap(self, start_ms: int, end_ms: int) -> None:
        """Caller must hold the lock."""
        self._gaps.append((start_ms, end_ms))
        self._degraded = True
        self._alerts.append(
            {
                "kind": "gap",
                "symbol": self.symbol,
                "from": start_ms,
                "to": end_ms,
                "missing": len(range(start_ms, end_ms, _GRID_MS)),
            }
        )

    def _prune_gaps(self) -> None:
        """Drop gaps that the current bars now cover. Caller holds the lock."""
        have = {bar["epoch"] for bar in self._bars}
        remaining = []
        for start, end in self._gaps:
            if any(ts not in have for ts in range(start, end, _GRID_MS)):
                remaining.append((start, end))
            else:
                self._alerts.append(
                    {"kind": "gap_filled", "symbol": self.symbol, "from": start, "to": end}
                )
        self._gaps = remaining
        self._degraded = bool(self._gaps)
