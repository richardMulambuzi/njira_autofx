"""
Flip-zone / polarity-reversal detector (ICT register item §12).

Deals with one question only: *has this zone stopped acting as support and
started acting as resistance, or the other way round?*

    SUPPLY   price is below the zone   -> the zone is overhead resistance
    DEMAND   price is above the zone   -> the zone is footing

The zone FLIPS when a bar travels past the far edge by more than
``confirm_close_pct`` of the zone's own height: a SUPPLY zone flips to DEMAND on
a push above its high, a DEMAND zone flips to SUPPLY on a push below its low.
The buffer is a fraction of the zone's own height, so "confirmed" means the same
thing for a wide zone and a narrow one.

No formation logic lives here, on purpose.  Zones are handed in by the caller,
so this module cannot inherit any other detector's opinion about what a zone is,
and tuning it can never change any other detector's behaviour.  That also means
there is no source disagreement to record: the sources disagree about how zones
are *drawn*, and drawing is not this module's job.

Standalone by design (project rule §1): imports nothing but stdlib.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.flip_zone")

CONCEPT_NAME = "flip_zone"
CONCEPT_SOURCES = ("ict_flip_zone", "polarity_reversal")



@dataclass(frozen=True)
class FlipZoneConfig:
    confirm_close_pct: float = 0.0      # how far past the far edge, as a fraction of zone height
    allow_wick_confirm: bool = False    # False = a CLOSE must lead; True = a wick alone is enough
    max_age_bars: int = 30              # a zone older than this is dropped
    pip_size: float = 0.0001            # unit for the reported height; 0.01 for gold


DEFAULT_FLIP_ZONE_CONFIG = FlipZoneConfig()



class FlipZoneDirection(Enum):
    SUPPLY = "SUPPLY"
    DEMAND = "DEMAND"
    UNKNOWN = "UNKNOWN"


class FlipZoneState(Enum):
    UNBROKEN = "UNBROKEN"    # polarity has never changed
    FLIPPED = "FLIPPED"      # polarity has changed at least once
    UNKNOWN = "UNKNOWN"


class FlipZoneEvent(Enum):
    ZONE_REGISTERED = "FZ_ZONE_REGISTERED"
    POLARITY_FLIPPED = "FZ_POLARITY_FLIPPED"
    ZONE_EXPIRED = "FZ_ZONE_EXPIRED"


# "BEARISH" maps to supply: breaker_blocks.py uses it for the ORIGINAL block.
DIRECTION_ALIASES = {
    "SUPPLY": FlipZoneDirection.SUPPLY,
    "RESISTANCE": FlipZoneDirection.SUPPLY,
    "BEARISH": FlipZoneDirection.SUPPLY,
    "BEAR": FlipZoneDirection.SUPPLY,
    "DEMAND": FlipZoneDirection.DEMAND,
    "SUPPORT": FlipZoneDirection.DEMAND,
    "BULLISH": FlipZoneDirection.DEMAND,
    "BULL": FlipZoneDirection.DEMAND,
}


@dataclass
class FlipZone:
    symbol: str = ""
    timeframe: int = 0
    zone_low: float = 0.0
    zone_high: float = 0.0
    height_pips: float = 0.0
    direction: FlipZoneDirection = FlipZoneDirection.UNKNOWN
    original_direction: FlipZoneDirection = FlipZoneDirection.UNKNOWN
    state: FlipZoneState = FlipZoneState.UNKNOWN
    flip_count: int = 0
    bars_since_created: int = 0
    created_at: object = None
    flipped_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "zone_low": round(self.zone_low, 6),
            "zone_high": round(self.zone_high, 6),
            "height_pips": round(self.height_pips, 2),
            "direction": self.direction.value,
            "original_direction": self.original_direction.value,
            "state": self.state.value,
            "flip_count": self.flip_count,
            "bars_since_created": self.bars_since_created,
            "created_at": self.created_at,
            "flipped_at": self.flipped_at,
        }



def _val(candle: dict, *keys, default=None):
    """Tolerant candle access, same tolerance as simple_snr._val."""
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    return _val(candle, "open_time", "time", "timestamp", "date")


def normalize_zone(zone) -> Optional[tuple[float, float]]:
    """Return (low, high) for a zone of either documented shape, else None.

    Accepted: ``{"low","high"}`` or ``{"lower_boundary","upper_boundary"}``.
    A zone of zero height carries no far edge to confirm against, so it is
    rejected rather than silently treated as a line.
    """
    if not isinstance(zone, dict):
        return None
    low = _val(zone, "low", "lower_boundary")
    high = _val(zone, "high", "upper_boundary")
    if low is None or high is None:
        return None
    try:
        low = float(low)
        high = float(high)
    except (TypeError, ValueError):
        return None
    if high <= low:
        return None
    return low, high


def direction_from_metadata(zone) -> Optional[FlipZoneDirection]:
    """Read the caller's own labelling of a zone, if it gave one."""
    if not isinstance(zone, dict):
        return None
    for key in ("direction", "side", "kind", "type"):
        raw = zone.get(key)
        if raw is None:
            continue
        if isinstance(raw, FlipZoneDirection):
            return raw
        if isinstance(raw, Enum):
            raw = raw.value
        found = DIRECTION_ALIASES.get(str(raw).upper())
        if found is not None:
            return found
    return None


def initial_direction(zone, price: float) -> FlipZoneDirection:
    """The caller's label if present, otherwise infer it from where price sits.

    A zone price is above is footing (DEMAND); a zone price is at or below is
    overhead (SUPPLY).  With no usable label and no usable bounds the direction
    stays UNKNOWN, and an UNKNOWN zone never flips.
    """
    labelled = direction_from_metadata(zone)
    if labelled is not None:
        return labelled
    bounds = normalize_zone(zone)
    if bounds is None:
        return FlipZoneDirection.UNKNOWN
    return FlipZoneDirection.DEMAND if price > bounds[1] else FlipZoneDirection.SUPPLY


def flip_direction(direction: FlipZoneDirection, close: float, high: float, low: float,
                   zone_low: float, zone_high: float,
                   confirm_close_pct: float = 0.0,
                   allow_wick_confirm: bool = False) -> Optional[FlipZoneDirection]:
    """Return the direction this bar flips the zone to, or None if it held.

    "Past the far edge" means the edge price would have to reach to leave the
    zone: the HIGH for a SUPPLY zone (price is under it), the LOW for a DEMAND
    zone (price is over it).  The margin is ``confirm_close_pct * height``.
    """
    height = zone_high - zone_low
    if height <= 0:
        return None
    buffer = confirm_close_pct * height
    up = high if allow_wick_confirm else close
    down = low if allow_wick_confirm else close
    if direction is FlipZoneDirection.SUPPLY and up > zone_high + buffer:
        return FlipZoneDirection.DEMAND
    if direction is FlipZoneDirection.DEMAND and down < zone_low - buffer:
        return FlipZoneDirection.SUPPLY
    return None



class FlipZoneEngine:
    """Per-symbol flip-zone state.  Pure: no clock, no I/O, no network.

    Zones arrive through ``analyze(..., zones=[...])`` and are tracked from then
    on.  Re-passing the same zone is free: a zone is identified by its own
    bounds rounded to six decimals, so registration happens exactly once.

    The bar under test is ``candles[-2]`` - the last CLOSED bar - so a still
    forming ``candles[-1]`` can never produce, or undo, a flip.  ``candles[-1]``
    still supplies the event timestamp, exactly as the sibling detectors do.
    """

    def __init__(self, config: FlipZoneConfig = DEFAULT_FLIP_ZONE_CONFIG) -> None:
        self.config = config
        self._zones: dict[tuple, FlipZone] = {}
        self._last_bar: dict[tuple[str, int], object] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "",
                zones: Optional[list[dict]] = None) -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        bar = candles[-2]
        raw_price = _val(bar, "close", "c")
        if raw_price is None:
            return []
        price = float(raw_price)
        high = float(_val(bar, "high", "h", default=price))
        low = float(_val(bar, "low", "l", default=price))

        ts = _time_key(candles[-1])
        bar_key = (symbol, tf)
        new_bar = ts is not None and ts != self._last_bar.get(bar_key)

        events: list[dict] = []

        if new_bar:
            for zone in self._zones.values():
                if zone.symbol == symbol and zone.timeframe == tf:
                    zone.bars_since_created += 1

        for raw in zones or []:
            bounds = normalize_zone(raw)
            if bounds is None:
                logger.debug("flip_zone: skipped malformed zone %r", raw)
                continue
            zone_low, zone_high = bounds
            key = self._zone_key(symbol, tf, zone_low, zone_high)
            if key in self._zones:
                continue
            direction = initial_direction(raw, price)
            record = FlipZone(
                symbol=symbol,
                timeframe=tf,
                zone_low=zone_low,
                zone_high=zone_high,
                height_pips=(zone_high - zone_low) / self.config.pip_size,
                direction=direction,
                original_direction=direction,
                state=FlipZoneState.UNBROKEN,
                created_at=ts,
            )
            self._zones[key] = record
            events.append(self._event(FlipZoneEvent.ZONE_REGISTERED, record, ts))

        expired: list[tuple] = []
        for key, zone in self._zones.items():
            if zone.symbol != symbol or zone.timeframe != tf:
                continue
            flipped = flip_direction(
                zone.direction, price, high, low, zone.zone_low, zone.zone_high,
                self.config.confirm_close_pct, self.config.allow_wick_confirm,
            )
            if flipped is not None:
                before = zone.direction
                zone.direction = flipped
                zone.state = FlipZoneState.FLIPPED
                zone.flip_count += 1
                zone.flipped_at = ts
                events.append(self._event(FlipZoneEvent.POLARITY_FLIPPED, zone, ts,
                                          direction_before=before.value,
                                          direction_after=flipped.value))
            if zone.bars_since_created > self.config.max_age_bars:
                expired.append(key)
                events.append(self._event(FlipZoneEvent.ZONE_EXPIRED, zone, ts))

        for key in expired:
            self._zones.pop(key, None)

        if ts is not None:
            self._last_bar[bar_key] = ts

        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    @staticmethod
    def _zone_key(symbol: str, tf: int, zone_low: float, zone_high: float) -> tuple:
        return (symbol, tf, round(zone_low, 6), round(zone_high, 6))

    def _event(self, event: FlipZoneEvent, record: FlipZone, timestamp, **extra) -> dict:
        payload = {"event": event.value, "timestamp": timestamp}
        payload.update(record.to_dict())
        payload.update(extra)
        return payload

    def get_active(self, limit: int = 10) -> list[dict]:
        return [z.to_dict() for z in list(self._zones.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list[dict]:
        return [z.to_dict() for z in list(self._zones.values())[-limit:]]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    def reset(self) -> None:
        self._zones.clear()
        self._last_bar.clear()
        self._events.clear()
