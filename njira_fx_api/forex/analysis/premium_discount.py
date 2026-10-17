"""
Premium / Discount / Equilibrium detector (ICT register item §18).

Deals with one question only: *where is price inside its dealing range?*

    premium      = price above the range midpoint
    equilibrium  = price within ``equilibrium_tolerance_pct`` of the midpoint
    discount     = price below the range midpoint

Sources disagree on the exact fib band that "counts", so both bands are stored
in Config and BOTH are always reported.  ``active_band`` only decides which band
is labelled primary — the disagreement is never silently resolved.

    litefinance.org ICT article : 50.0% – 61.8%
    theinnercircletraders roadmap: 62.0% – 79.0%   (ICT "OTE")
    innercircletraders glossary  : 50.0% – 61.8%

Standalone by design (project rule §1): imports nothing but stdlib.  The dealing
range is computed here from the candles it is handed, so tuning this module can
never change any other detector's behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.premium_discount")

CONCEPT_NAME = "premium_discount"
CONCEPT_SOURCES = ("litefinance", "theinnercircletraders", "ict_glossary")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class PremiumDiscountConfig:
    range_lookback: int = 120          # bars used to build the dealing range
    swing_lookback: int = 5            # bars each side for a swing pivot
    equilibrium_tolerance_pct: float = 0.05   # fraction of range = still "equilibrium"
    deep_zone_threshold: float = 0.79  # beyond this retracement = deep premium/discount
    active_band: str = "litefinance"   # "litefinance" | "roadmap"
    band_litefinance_low: float = 0.50
    band_litefinance_high: float = 0.618
    band_roadmap_low: float = 0.62
    band_roadmap_high: float = 0.79
    pip_size: float = 0.0001
    min_range_pips: float = 5.0        # ignore degenerate ranges


DEFAULT_PREMIUM_DISCOUNT_CONFIG = PremiumDiscountConfig()


# ── Result types ────────────────────────────────────────────────────────────

class PDPosition(Enum):
    PREMIUM = "PREMIUM"
    EQUILIBRIUM = "EQUILIBRIUM"
    DISCOUNT = "DISCOUNT"
    UNKNOWN = "UNKNOWN"


class PDEvent(Enum):
    RANGE_LOCKED = "PD_RANGE_LOCKED"
    ZONE_CHANGED = "PD_ZONE_CHANGED"
    OTE_ENTERED = "PD_OTE_ENTERED"
    OTE_EXITED = "PD_OTE_EXITED"


@dataclass
class PremiumDiscount:
    symbol: str = ""
    timeframe: int = 0
    range_high: float = 0.0
    range_low: float = 0.0
    equilibrium: float = 0.0
    current_price: float = 0.0
    leg_direction: str = "NEUTRAL"      # "BULLISH" | "BEARISH" | "NEUTRAL"
    position: PDPosition = PDPosition.UNKNOWN
    retracement: float = 0.0            # 0.0 at leg origin extreme, 1.0 at leg end
    in_litefinance_band: bool = False
    in_roadmap_band: bool = False
    in_active_band: bool = False
    deep_zone: bool = False
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "range_high": round(self.range_high, 6),
            "range_low": round(self.range_low, 6),
            "equilibrium": round(self.equilibrium, 6),
            "current_price": round(self.current_price, 6),
            "leg_direction": self.leg_direction,
            "position": self.position.value,
            "retracement": round(self.retracement, 4),
            "in_litefinance_band": self.in_litefinance_band,
            "in_roadmap_band": self.in_roadmap_band,
            "in_active_band": self.in_active_band,
            "deep_zone": self.deep_zone,
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


def dealing_range(candles: list[dict], cfg: PremiumDiscountConfig
                  ) -> tuple[float, float, str]:
    """Return (range_low, range_high, leg_direction) from the recent window.

    leg_direction describes which extreme formed LAST — that is the leg price is
    currently retracing, i.e. BULLISH if the low came first and the high after.
    """
    window = candles[-cfg.range_lookback:] if cfg.range_lookback else list(candles)
    if not window:
        return 0.0, 0.0, "NEUTRAL"

    hi_idx = max(range(len(window)), key=lambda i: float(_val(window[i], "high", "h", default=0.0)))
    lo_idx = min(range(len(window)), key=lambda i: float(_val(window[i], "low", "l", default=0.0)))
    range_high = float(_val(window[hi_idx], "high", "h", default=0.0))
    range_low = float(_val(window[lo_idx], "low", "l", default=0.0))

    if range_high <= range_low:
        return range_low, range_high, "NEUTRAL"
    if lo_idx < hi_idx:
        return range_low, range_high, "BULLISH"
    return range_low, range_high, "BEARISH"


def retracement_of(price: float, range_low: float, range_high: float,
                   leg_direction: str) -> float:
    """Fraction retraced into the leg.  0.0 = at the leg end, 1.0 = at the origin."""
    span = range_high - range_low
    if span <= 0:
        return 0.0
    if leg_direction == "BULLISH":       # leg low → high, retracing downward
        return (range_high - price) / span
    if leg_direction == "BEARISH":       # leg high → low, retracing upward
        return (price - range_low) / span
    return 0.0


def in_band(retracement: float, low: float, high: float) -> bool:
    return low <= retracement <= high


# ── Engine ──────────────────────────────────────────────────────────────────

class PremiumDiscountEngine:
    """Per-symbol premium/discount state.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: PremiumDiscountConfig = DEFAULT_PREMIUM_DISCOUNT_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], PremiumDiscount] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        range_low, range_high, leg = dealing_range(candles, self.config)
        span = range_high - range_low
        if span <= 0 or span / self.config.pip_size < self.config.min_range_pips:
            return []

        price = float(_val(candles[-1], "close", "c", default=range_high))
        equilibrium = (range_high + range_low) / 2.0
        tolerance = span * self.config.equilibrium_tolerance_pct

        if price > equilibrium + tolerance:
            position = PDPosition.PREMIUM
        elif price < equilibrium - tolerance:
            position = PDPosition.DISCOUNT
        else:
            position = PDPosition.EQUILIBRIUM

        retr = retracement_of(price, range_low, range_high, leg)
        record = PremiumDiscount(
            symbol=symbol,
            timeframe=tf,
            range_high=range_high,
            range_low=range_low,
            equilibrium=equilibrium,
            current_price=price,
            leg_direction=leg,
            position=position,
            retracement=retr,
            in_litefinance_band=in_band(retr, self.config.band_litefinance_low,
                                        self.config.band_litefinance_high),
            in_roadmap_band=in_band(retr, self.config.band_roadmap_low,
                                    self.config.band_roadmap_high),
            in_active_band=self._in_active_band(retr),
            deep_zone=retr >= self.config.deep_zone_threshold,
            created_at=_time_key(candles[-1]),
        )

        events = self._diff(record)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    def _in_active_band(self, retr: float) -> bool:
        if self.config.active_band == "roadmap":
            return in_band(retr, self.config.band_roadmap_low, self.config.band_roadmap_high)
        return in_band(retr, self.config.band_litefinance_low, self.config.band_litefinance_high)

    def _diff(self, record: PremiumDiscount) -> list[dict]:
        key = (record.symbol, record.timeframe)
        prev: Optional[PremiumDiscount] = self._state.get(key)
        events: list[dict] = []
        if prev is None:
            events.append(self._event(PDEvent.RANGE_LOCKED, record))
            return events
        if prev.position != record.position:
            events.append(self._event(PDEvent.ZONE_CHANGED, record,
                                      previous=prev.position.value))
        if not prev.in_active_band and record.in_active_band:
            events.append(self._event(PDEvent.OTE_ENTERED, record))
        elif prev.in_active_band and not record.in_active_band:
            events.append(self._event(PDEvent.OTE_EXITED, record))
        return events

    def _event(self, event: PDEvent, record: PremiumDiscount, **extra) -> dict:
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
