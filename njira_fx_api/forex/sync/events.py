"""
SyncEvent builders + EventPublisher — in-process pub/sub (contracts §6).

Events are plain JSON dicts (the ``SyncEvent`` wire form), each with an
``"event"`` discriminator. Datetimes render as ISO-8601 UTC strings
(contracts §1.3, mirroring ``ProviderHealth.to_dict()``). Field names are
snake_case. The six events (contracts §6.1–§6.6):

  * ``tick_committed``        — a tick is committed to the window
  * ``window_advanced``       — after the eviction check (spec §4.3)
  * ``recovery_state_changed``— any state transition (contracts §5.2)
  * ``gap_detected``          — live stream stalls past the threshold
  * ``backfill_progress``     — during backfill, per page
  * ``provider_switched``     — factory switch detected (engine-wide)

The publisher fans events out to subscribers with per-callback error
isolation (spec §9.2: "subscriber registration, event fan-out, error
isolation").
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Callable

logger = logging.getLogger("forex.sync.events")


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 UTC wire form (None-safe, contracts §1.3)."""
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Event builders (contracts §6.1–§6.6 — exact field schemas)
# ---------------------------------------------------------------------------
def tick_committed(
    symbol: str,
    provider: str,
    bid: float,
    ask: float,
    last: float,
    timestamp: datetime,
    volume: float = 0.0,
    seq: int = -1,
) -> dict:
    """§6.1 — a tick is committed to the window."""
    return {
        "event": "tick_committed",
        "symbol": symbol,
        "provider": provider,
        "bid": bid,
        "ask": ask,
        "last": last,
        "timestamp": _iso(timestamp),
        "volume": volume,
        "seq": seq,
    }


def window_advanced(
    symbol: str,
    evicted_count: int,
    oldest_ts: datetime | None,
    newest_ts: datetime | None,
    total_count: int,
) -> dict:
    """§6.2 — after the eviction check (spec §4.3)."""
    return {
        "event": "window_advanced",
        "symbol": symbol,
        "evicted_count": evicted_count,
        "oldest_ts": _iso(oldest_ts),
        "newest_ts": _iso(newest_ts),
        "total_count": total_count,
    }


def recovery_state_changed(
    symbol: str,
    old_state: str,
    new_state: str,
    reason: str,
) -> dict:
    """§6.3 — any state transition (contracts §5.2)."""
    return {
        "event": "recovery_state_changed",
        "symbol": symbol,
        "old_state": old_state,
        "new_state": new_state,
        "reason": reason,
    }


def gap_detected(
    symbol: str,
    from_ts: datetime,
    to_ts: datetime,
    expected_cadence_seconds: float,
) -> dict:
    """§6.4 — live stream stalls past the threshold."""
    return {
        "event": "gap_detected",
        "symbol": symbol,
        "from_ts": _iso(from_ts),
        "to_ts": _iso(to_ts),
        "expected_cadence_seconds": expected_cadence_seconds,
    }


def backfill_progress(
    symbol: str,
    pages_done: int,
    pages_total: int,
    oldest_ts: datetime | None,
) -> dict:
    """§6.5 — during backfill, per page."""
    return {
        "event": "backfill_progress",
        "symbol": symbol,
        "pages_done": pages_done,
        "pages_total": pages_total,
        "oldest_ts": _iso(oldest_ts),
    }


def provider_switched(old_provider: str, new_provider: str) -> dict:
    """§6.6 — factory switch detected (engine-wide, no symbol)."""
    return {
        "event": "provider_switched",
        "old_provider": old_provider,
        "new_provider": new_provider,
    }


# ---------------------------------------------------------------------------
# EventPublisher
# ---------------------------------------------------------------------------
class EventPublisher:
    """In-process pub/sub with per-subscriber error isolation (spec §9.2).

    The SSE/WS bridge (Phase 4, spec §7.2) will consume these events; the
    publisher itself is transport-agnostic.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, Callable[[dict], None]] = {}
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[dict], None]) -> str:
        """Register a callback; returns a subscription id."""
        subscription_id = uuid.uuid4().hex
        with self._lock:
            self._subscribers[subscription_id] = callback
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription (no-op for unknown ids)."""
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def publish(self, event: dict) -> None:
        """Fan out one event dict to all subscribers.

        A failing subscriber is logged and isolated — it never breaks the
        fan-out to other subscribers (spec §9.2).
        """
        with self._lock:
            callbacks = list(self._subscribers.values())
        for callback in callbacks:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 — subscriber isolation
                logger.exception(
                    "sync event subscriber error (event=%s)",
                    event.get("event"),
                )

    def __len__(self) -> int:
        with self._lock:
            return len(self._subscribers)


__all__ = [
    "EventPublisher",
    "backfill_progress",
    "gap_detected",
    "provider_switched",
    "recovery_state_changed",
    "tick_committed",
    "window_advanced",
]