"""
Point of Interest (POI) detector — confluence zone where injected zones overlap.

Standalone by design (project rule §1): imports nothing but stdlib + sessions.
All thresholds live in the frozen Config dataclass. No sibling imports.
Zones are injected via `analyze(candles, tf, symbol, zones, context)`.

Sources: ICT concept of confluence / point of interest where multiple
reference levels (FVG, OB, Breaker, etc.) stack.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from forex.analysis import sessions

logger = logging.getLogger("forex.analysis.poi")

CONCEPT_NAME = "poi"
CONCEPT_SOURCES = ("ict_confluence", "smart_money_concepts")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class PoiConfig:
    require_overlap: bool = True
    min_overlap_ticks: float = 0.0
    require_killzone: bool = False
    require_discount_for_longs: bool = False
    max_pois: int = 3
    tick_size: float = 0.00001
    pip_size: float = 0.0001


DEFAULT_POI_CONFIG = PoiConfig()


# ── Result types ────────────────────────────────────────────────────────────

class PoiState(Enum):
    NONE = "NONE"
    CANDIDATE = "CANDIDATE"
    ELIGIBLE = "ELIGIBLE"
    UNKNOWN = "UNKNOWN"


class PoiEvent(Enum):
    POI_FOUND = "POI_FOUND"
    POI_ELIGIBLE = "POI_ELIGIBLE"


@dataclass
class Poi:
    symbol: str = ""
    timeframe: int = 0
    low: float = 0.0
    high: float = 0.0
    overlap_depth_ticks: float = 0.0
    contributing_kinds: tuple = ()
    eligible: bool = False
    state: PoiState = PoiState.UNKNOWN
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "low": round(self.low, 6),
            "high": round(self.high, 6),
            "overlap_depth_ticks": round(self.overlap_depth_ticks, 4),
            "contributing_kinds": list(self.contributing_kinds),
            "eligible": self.eligible,
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


def _overlap_of_zones(zones: list[dict], tick_size: float) -> list[tuple[float, float, float, tuple]]:
    """
    Find all pairwise overlaps among zones.
    Returns list of (overlap_low, overlap_high, overlap_depth_ticks, contributing_kinds).
    """
    bounds_list = []
    for z in zones:
        b = _zone_bounds(z)
        if b is not None:
            bounds_list.append((b[0], b[1], _zone_kind(z)))
    if len(bounds_list) < 2:
        return []

    overlaps = []
    for i in range(len(bounds_list)):
        for j in range(i + 1, len(bounds_list)):
            low_i, high_i, kind_i = bounds_list[i]
            low_j, high_j, kind_j = bounds_list[j]
            overlap_low = max(low_i, low_j)
            overlap_high = min(high_i, high_j)
            if overlap_low < overlap_high:
                depth_ticks = (overlap_high - overlap_low) / tick_size
                kinds = tuple(sorted({kind_i, kind_j}))
                overlaps.append((overlap_low, overlap_high, depth_ticks, kinds))
    return overlaps


# ── Engine ──────────────────────────────────────────────────────────────────

class PoiEngine:
    """Per-symbol POI state. Pure: no clock, no I/O, no network."""

    def __init__(self, config: PoiConfig = DEFAULT_POI_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], Poi] = {}
        self._events: list[dict] = []
        # Identity keys already announced, so a repeat of the same overlap is
        # silent while a genuinely new POI still fires (events fire once).
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

        # Find overlaps
        overlaps = _overlap_of_zones(zones, self.config.tick_size)

        # Filter by min_overlap_ticks if required
        if self.config.require_overlap:
            overlaps = [o for o in overlaps if o[2] >= self.config.min_overlap_ticks]

        # Sort by overlap depth (deepest first)
        overlaps.sort(key=lambda x: x[2], reverse=True)

        # Limit to max_pois
        overlaps = overlaps[: self.config.max_pois]

        events: list[dict] = []
        current_time = _time_key(candles[-1])

        for overlap_low, overlap_high, depth_ticks, kinds in overlaps:
            # Check eligibility
            eligible = True
            if self.config.require_killzone:
                ts = current_time if isinstance(current_time, (int, float)) else 0
                eligible = eligible and sessions.in_killzone(ts)
            if self.config.require_discount_for_longs:
                # Long POI = price is in discount (below equilibrium)
                in_discount = context.get("position") == "DISCOUNT" or context.get("in_discount") is True
                eligible = eligible and in_discount

            state = PoiState.ELIGIBLE if eligible else PoiState.CANDIDATE

            record = Poi(
                symbol=symbol,
                timeframe=tf,
                low=overlap_low,
                high=overlap_high,
                overlap_depth_ticks=depth_ticks,
                contributing_kinds=kinds,
                eligible=eligible,
                state=state,
                created_at=current_time,
            )

            key = (symbol, tf, round(overlap_low, 6), round(overlap_high, 6))
            if key not in self._seen:
                self._seen.add(key)
                events.append(self._event(PoiEvent.POI_FOUND, record))
                if eligible:
                    events.append(self._event(PoiEvent.POI_ELIGIBLE, record))

            self._state[(symbol, tf)] = record

        # Cap events
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]

        return events

    def _event(self, event: PoiEvent, record: Poi, **extra) -> dict:
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