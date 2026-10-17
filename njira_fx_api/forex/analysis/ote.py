"""
Optimal Trade Entry (OTE) band detector (ICT register item §20).

OTE is the Fibonacci retracement band of the current leg where ICT entries are
taken.  The sources do not agree on the band, so both bands live in Config and
both are reported; ``active_band`` decides which one is primary.  The
disagreement is deliberately NOT resolved in code.

    litefinance.org ICT article  : 0.500 – 0.618
    theinnercircletraders roadmap: 0.620 – 0.790   (ICT's published OTE)
    tradingfinder.com            : 0.620 – 0.790

Optionally the band must overlap an injected zone (order block / FVG) before it
counts as actionable — that is the "OTE combined with OB or FVG" rule.  Zones
arrive as plain dicts with ``high``/``low`` (or ``upper_boundary`` /
``lower_boundary``), injected by the caller; this module never imports a sibling
detector.

Standalone by design (project rule §1): stdlib imports only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.ote")

CONCEPT_NAME = "ote"
CONCEPT_SOURCES = ("litefinance", "theinnercircletraders", "tradingfinder")

# Band edges count as inside.  Float division can land a hair under/over the edge,
# so the state decision allows this much slack.
_EPS = 1e-9


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class OTEConfig:
    range_lookback: int = 120
    active_band: str = "roadmap"          # "roadmap" | "litefinance"
    band_litefinance_low: float = 0.500   # source: litefinance.org ICT article
    band_litefinance_high: float = 0.618
    band_roadmap_low: float = 0.620       # source: theinnercircletraders roadmap / ICT
    band_roadmap_high: float = 0.790
    require_zone_overlap: bool = True     # "OTE combined with OB or FVG"
    zone_overlap_tolerance_pips: float = 0.0
    min_range_pips: float = 5.0
    pip_size: float = 0.0001


DEFAULT_OTE_CONFIG = OTEConfig()


# ── Result types ────────────────────────────────────────────────────────────

class OTEState(Enum):
    IN_BAND = "IN_BAND"
    SHALLOW = "SHALLOW"       # not retraced far enough yet
    DEEP = "DEEP"             # retraced past the band
    UNKNOWN = "UNKNOWN"


class OTEEvent(Enum):
    OTE_ZONE_SET = "OTE_ZONE_SET"
    OTE_ENTERED = "OTE_ENTERED"
    OTE_EXITED = "OTE_EXITED"


@dataclass
class OTESetup:
    symbol: str = ""
    timeframe: int = 0
    leg_direction: str = "NEUTRAL"
    range_high: float = 0.0
    range_low: float = 0.0
    band_low: float = 0.0             # retracement value used (active band)
    band_high: float = 0.0
    entry_zone_low: float = 0.0       # same band expressed in price
    entry_zone_high: float = 0.0
    current_price: float = 0.0
    retracement: float = 0.0
    state: OTEState = OTEState.UNKNOWN
    in_litefinance_band: bool = False
    in_roadmap_band: bool = False
    zone_overlap: bool = False
    actionable: bool = False
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "leg_direction": self.leg_direction,
            "range_high": round(self.range_high, 6),
            "range_low": round(self.range_low, 6),
            "band_low": self.band_low,
            "band_high": self.band_high,
            "entry_zone_low": round(self.entry_zone_low, 6),
            "entry_zone_high": round(self.entry_zone_high, 6),
            "current_price": round(self.current_price, 6),
            "retracement": round(self.retracement, 4),
            "state": self.state.value,
            "in_litefinance_band": self.in_litefinance_band,
            "in_roadmap_band": self.in_roadmap_band,
            "zone_overlap": self.zone_overlap,
            "actionable": self.actionable,
        }


# ── Pure helpers ────────────────────────────────────────────────────────────

def _val(candle: dict, *keys, default=None):
    if not isinstance(candle, dict):       # injected zones may be malformed (None, {})
        return default
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    return _val(candle, "open_time", "time", "timestamp", "date")


def leg_range(candles: list[dict], range_lookback: int) -> tuple[float, float, str]:
    """(low, high, leg_direction); leg_direction = which extreme formed last."""
    window = candles[-range_lookback:] if range_lookback else list(candles)
    if not window:
        return 0.0, 0.0, "NEUTRAL"
    hi_idx = max(range(len(window)), key=lambda i: float(_val(window[i], "high", "h", default=0.0)))
    lo_idx = min(range(len(window)), key=lambda i: float(_val(window[i], "low", "l", default=0.0)))
    high = float(_val(window[hi_idx], "high", "h", default=0.0))
    low = float(_val(window[lo_idx], "low", "l", default=0.0))
    if high <= low:
        return low, high, "NEUTRAL"
    return low, high, ("BULLISH" if lo_idx < hi_idx else "BEARISH")


def retracement_of(price: float, range_low: float, range_high: float,
                   leg_direction: str) -> float:
    span = range_high - range_low
    if span <= 0:
        return 0.0
    if leg_direction == "BULLISH":
        return (range_high - price) / span
    if leg_direction == "BEARISH":
        return (price - range_low) / span
    return 0.0


def band_prices(range_low: float, range_high: float, leg_direction: str,
                band_low: float, band_high: float) -> tuple[float, float]:
    """Express a retracement band as a price zone, ordered low → high."""
    span = range_high - range_low
    if leg_direction == "BULLISH":
        a = range_high - band_high * span
        b = range_high - band_low * span
    else:
        a = range_low + band_low * span
        b = range_low + band_high * span
    return (min(a, b), max(a, b))


def zone_bounds(zone: dict) -> Optional[tuple[float, float]]:
    high = _val(zone, "high", "upper_boundary", "zone_high", default=None)
    low = _val(zone, "low", "lower_boundary", "zone_low", default=None)
    if high is None or low is None:
        return None
    high, low = float(high), float(low)
    return (min(low, high), max(low, high))


def overlaps(band: tuple[float, float], zone: tuple[float, float]) -> bool:
    return band[0] <= zone[1] and zone[0] <= band[1]


# ── Engine ──────────────────────────────────────────────────────────────────

class OTEEngine:
    """Per-symbol OTE state.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: OTEConfig = DEFAULT_OTE_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], OTESetup] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "",
                zones: Optional[list[dict]] = None) -> list[dict]:
        """zones: injected order blocks / FVGs (plain dicts), not imported detectors."""
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        range_low, range_high, leg = leg_range(candles, self.config.range_lookback)
        span = range_high - range_low
        if span <= 0 or span / self.config.pip_size < self.config.min_range_pips:
            return []

        band_low, band_high = self._active_band()
        entry_low, entry_high = band_prices(range_low, range_high, leg, band_low, band_high)

        price = float(_val(candles[-1], "close", "c", default=range_high))
        retr = retracement_of(price, range_low, range_high, leg)

        if retr < band_low - _EPS:
            state = OTEState.SHALLOW
        elif retr > band_high + _EPS:
            state = OTEState.DEEP
        else:
            state = OTEState.IN_BAND

        zone_overlap = self._zone_overlap((entry_low, entry_high), zones or [])
        setup = OTESetup(
            symbol=symbol,
            timeframe=tf,
            leg_direction=leg,
            range_high=range_high,
            range_low=range_low,
            band_low=band_low,
            band_high=band_high,
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            current_price=price,
            retracement=retr,
            state=state,
            in_litefinance_band=self.config.band_litefinance_low <= retr <= self.config.band_litefinance_high,
            in_roadmap_band=self.config.band_roadmap_low <= retr <= self.config.band_roadmap_high,
            zone_overlap=zone_overlap,
            actionable=state is OTEState.IN_BAND and (
                zone_overlap or not self.config.require_zone_overlap),
            created_at=_time_key(candles[-1]),
        )

        events = self._diff(setup)
        self._state[(symbol, tf)] = setup
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _active_band(self) -> tuple[float, float]:
        if self.config.active_band == "litefinance":
            return self.config.band_litefinance_low, self.config.band_litefinance_high
        return self.config.band_roadmap_low, self.config.band_roadmap_high

    def _zone_overlap(self, band: tuple[float, float], zones: list[dict]) -> bool:
        tol = self.config.zone_overlap_tolerance_pips * self.config.pip_size
        padded = (band[0] - tol, band[1] + tol)
        for zone in zones:
            bounds = zone_bounds(zone)
            if bounds and overlaps(padded, bounds):
                return True
        return False

    def _diff(self, setup: OTESetup) -> list[dict]:
        key = (setup.symbol, setup.timeframe)
        prev: Optional[OTESetup] = self._state.get(key)
        if prev is None:
            return [self._event(OTEEvent.OTE_ZONE_SET, setup)]
        events: list[dict] = []
        was_band = prev.state is OTEState.IN_BAND
        now_band = setup.state is OTEState.IN_BAND
        if not was_band and now_band:
            events.append(self._event(OTEEvent.OTE_ENTERED, setup))
        elif prev.state is OTEState.SHALLOW and setup.state is OTEState.DEEP:
            # ran straight through the band between two bars — still an entry
            events.append(self._event(OTEEvent.OTE_ENTERED, setup))
        elif was_band and not now_band:
            events.append(self._event(OTEEvent.OTE_EXITED, setup))
        return events

    def _event(self, event: OTEEvent, setup: OTESetup, **extra) -> dict:
        payload = {"event": event.value, "timestamp": setup.created_at}
        payload.update(setup.to_dict())
        payload.update(extra)
        return payload

    def get_active(self, limit: int = 10) -> list[dict]:
        return [s.to_dict() for s in list(self._state.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list[dict]:
        return [s.to_dict() for s in list(self._state.values())[-limit:]]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    def reset(self) -> None:
        self._state.clear()
        self._events.clear()
