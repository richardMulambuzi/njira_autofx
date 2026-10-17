"""
Premium-Discount Array (PD Array) classifier/taxonomy.

Labels each injected zone by its kind/type, merges zones within price tolerance,
and ranks by priority order. Standalone by design (project rule §1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from forex.analysis import sessions

logger = logging.getLogger("forex.analysis.pd_array")

CONCEPT_NAME = "pd_array"
CONCEPT_SOURCES = ("ict_pd_array", "smart_money_concepts")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class PdArrayConfig:
    priority_order: tuple = (
        "FVG",
        "ORDER_BLOCK",
        "BREAKER",
        "BREAKER_BLOCK",
        "MITIGATION_BLOCK",
        "LIQUIDITY_VOID",
        "OLD_HIGH_LOW",
        "EQUAL_HIGHS_LOWS",
        "REJECTION_BLOCK",
        "PROPULSION_BLOCK",
    )
    known_types: tuple = (
        "FVG",
        "ORDER_BLOCK",
        "BREAKER",
        "MITIGATION_BLOCK",
        "LIQUIDITY_VOID",
        "OLD_HIGH_LOW",
        "EQUAL_HIGHS_LOWS",
        "REJECTION_BLOCK",
        "PROPULSION_BLOCK",
    )
    price_tolerance_ticks: float = 5.0
    max_arrays: int = 5
    tick_size: float = 0.00001
    pip_size: float = 0.0001


DEFAULT_PD_ARRAY_CONFIG = PdArrayConfig()


# ── Result types ────────────────────────────────────────────────────────────

class PdArrayState(Enum):
    NONE = "NONE"
    CLASSIFIED = "CLASSIFIED"
    UNKNOWN = "UNKNOWN"


class PdArrayEvent(Enum):
    PD_ARRAY_CLASSIFIED = "PD_ARRAY_CLASSIFIED"


@dataclass
class PdArray:
    symbol: str = ""
    timeframe: int = 0
    price: float = 0.0
    types: tuple = ()
    known: bool = True
    rank: int = 0
    matched: bool = False
    state: PdArrayState = PdArrayState.UNKNOWN
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "price": round(self.price, 6),
            "types": list(self.types),
            "known": self.known,
            "rank": self.rank,
            "matched": self.matched,
            "state": self.state.value,
        }


# ── Pure helpers (no state, no I/O) ─────────────────────────────────────────

def _val(candle: dict, *keys, default=None):
    """Tolerant candle access — same tolerance as simple_snr._val."""
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    return _val(candle, "open_time", "time", "timestamp", "date")


def _zone_bounds(zone: dict) -> tuple[float, float] | None:
    """Extract (low, high) from a zone dict. Accepts multiple key styles."""
    if not isinstance(zone, dict):
        return None
    low = _val(zone, "low", "lower_boundary", default=None)
    high = _val(zone, "high", "upper_boundary", default=None)
    if low is None or high is None:
        return None
    low_f = float(low)
    high_f = float(high)
    if low_f > high_f:
        low_f, high_f = high_f, low_f
    return low_f, high_f


def _zone_kind(zone: dict) -> str:
    """Extract kind/type label from a zone dict."""
    if not isinstance(zone, dict):
        return "UNKNOWN"
    return str(_val(zone, "kind", "type", default="UNKNOWN"))


def _zone_midpoint(zone: dict) -> float | None:
    """Get midpoint of a zone."""
    bounds = _zone_bounds(zone)
    if bounds is None:
        return None
    return (bounds[0] + bounds[1]) / 2.0


def _merge_zones(zones: list[dict], tolerance_ticks: float, tick_size: float) -> list[dict]:
    """
    Merge zones whose bounds are within price_tolerance_ticks.
    Returns list of merged zone dicts with aggregated types.
    """
    if not zones:
        return []

    # Sort by midpoint
    zones_with_mid = []
    for z in zones:
        mid = _zone_midpoint(z)
        if mid is not None:
            zones_with_mid.append((mid, z))

    if not zones_with_mid:
        return []

    zones_with_mid.sort(key=lambda x: x[0])

    merged = []
    current_group = [zones_with_mid[0][1]]
    current_mid = zones_with_mid[0][0]
    tolerance_price = tolerance_ticks * tick_size

    for mid, z in zones_with_mid[1:]:
        if abs(mid - current_mid) <= tolerance_price:
            current_group.append(z)
            # Update current_mid to be the average of the group
            current_mid = sum(_zone_midpoint(g) or 0 for g in current_group) / len(current_group)
        else:
            merged.append(current_group)
            current_group = [z]
            current_mid = mid

    merged.append(current_group)

    # Convert groups to merged zone dicts
    result = []
    for group in merged:
        bounds_list = [_zone_bounds(g) for g in group if _zone_bounds(g) is not None]
        if not bounds_list:
            continue
        low = min(b[0] for b in bounds_list)
        high = max(b[1] for b in bounds_list)
        types = tuple(sorted({_zone_kind(g) for g in group}))
        result.append({"low": low, "high": high, "types": types})

    return result


# ── Engine ──────────────────────────────────────────────────────────────────

class PdArrayEngine:
    """Per-symbol PD Array state. Pure: no clock, no I/O, no network."""

    def __init__(self, config: PdArrayConfig = DEFAULT_PD_ARRAY_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], PdArray] = {}
        self._events: list[dict] = []
        # Identity keys already announced, so a repeat of the same classified
        # array is silent while a genuinely new one still fires (events fire once).
        self._seen: set = set()

    def analyze(
        self,
        candles: list[dict],
        tf: int = 0,
        symbol: str = "",
        zones: list[dict] | None = None,
        context: dict | None = None,
    ) -> list[dict]:
        if len(candles) < 2:
            return []

        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        zones = zones or []
        context = context or {}

        # Merge nearby zones
        merged = _merge_zones(zones, self.config.price_tolerance_ticks, self.config.tick_size)

        # Get current price
        current_price = float(_val(candles[-1], "close", "c", default=0.0))

        # Classify each merged zone
        arrays = []
        for m in merged:
            types = m["types"]
            # Check if all types are known
            known = all(t in self.config.known_types for t in types)
            # Rank: index in priority_order (unknown types rank after all known)
            ranks = []
            for t in types:
                try:
                    ranks.append(self.config.priority_order.index(t))
                except ValueError:
                    ranks.append(len(self.config.priority_order))
            rank = min(ranks) if ranks else len(self.config.priority_order)

            # Check if matched (within tolerance of current price)
            mid = (m["low"] + m["high"]) / 2.0
            matched = abs(mid - current_price) <= (self.config.price_tolerance_ticks * self.config.tick_size)

            arrays.append({
                "price": mid,
                "low": m["low"],
                "high": m["high"],
                "types": types,
                "known": known,
                "rank": rank,
                "matched": matched,
            })

        # Sort by rank (lower = higher priority), then by matched (matched first), then by price distance
        arrays.sort(key=lambda a: (a["rank"], not a["matched"], abs(a["price"] - current_price)))

        # Limit to max_arrays
        arrays = arrays[: self.config.max_arrays]

        events: list[dict] = []
        current_time = _time_key(candles[-1])

        for arr in arrays:
            record = PdArray(
                symbol=symbol,
                timeframe=tf,
                price=arr["price"],
                types=arr["types"],
                known=arr["known"],
                rank=arr["rank"],
                matched=arr["matched"],
                state=PdArrayState.CLASSIFIED,
                created_at=current_time,
            )

            key = (symbol, tf, round(arr["price"], 6), tuple(arr["types"]))
            if key not in self._seen:
                self._seen.add(key)
                events.append(self._event(PdArrayEvent.PD_ARRAY_CLASSIFIED, record))
            self._state[(symbol, tf)] = record

        # Cap events
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]

        return events

    def _event(self, event: PdArrayEvent, record: PdArray, **extra) -> dict:
        payload = {"event": event.value, "timestamp": record.created_at}
        payload.update(record.to_dict())
        payload.update(extra)
        return payload

    def get_active(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    def reset(self) -> None:
        self._state.clear()
        self._events.clear()
        self._seen.clear()