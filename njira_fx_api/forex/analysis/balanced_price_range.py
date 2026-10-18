"""Balanced price range (BPR): where opposing fair value gaps overlap.

This module answers one question: *has a bullish fair value gap and a bearish
fair value gap overlapped in price, leaving a balanced zone, and is that zone
still intact?*

An ICT fair value gap is a three candle imbalance. A bullish gap exists at the
middle candle ``i`` when ``candles[i-1].high < candles[i+1].low``; the gap is
the space between those two prices and it is only knowable once candle
``i+1`` closes. A bearish gap is the mirror: ``candles[i-1].low >
candles[i+1].high``. A balanced price range is what is left where one of each
overlaps. The overlap is reported as ``bpr_low``/``bpr_high``, with both
contributing gaps carried on the record so a caller can see how the zone was
built, and never has to take this module's word for it.

Direction rule (stated, not hidden): the later of the two gaps sets the bias,
because the later gap is the one price has most recently agreed with. Both
formation timestamps are reported as ``bull_formation_index`` and
``bear_formation_index`` so a caller that disagrees can re-derive the label
instead of arguing with a silent default.

The word "balanced" is the whole point: an overlap means buyers and sellers
each left a void in the same place, so price has already been accepted on both
sides of that band. That is a different animal from a single gap, and it is why
this detector is its own file.

A break is a close beyond either edge of the overlap band, evaluated on the
last closed bar, so a still-forming candle can neither raise nor break a zone.

Standalone by design (project rule 1): imports nothing but stdlib.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.balanced_price_range")

CONCEPT_NAME = "balanced_price_range"
CONCEPT_SOURCES = ("ict_bpr", "balanced_price_range")


@dataclass(frozen=True)
class BprConfig:
    min_overlap_pct: float = 0.0         # overlap depth as a share of the smaller gap
    max_bars_apart: int = 20             # how far the two gaps may form from each other
    pip_size: float = 0.0001             # unit for reported heights; the caller sets it per symbol
    tick_size: float = 0.00001


DEFAULT_BPR_CONFIG = BprConfig()


class BprState(Enum):
    ACTIVE = "ACTIVE"
    BROKEN = "BROKEN"
    UNKNOWN = "UNKNOWN"


class BprEvent(Enum):
    FORMED = "BPR_FORMED"
    BROKEN = "BPR_BROKEN"


class BprDirection(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    UNKNOWN = "UNKNOWN"


@dataclass
class BalancedPriceRange:
    symbol: str
    timeframe: int
    bpr_low: float
    bpr_high: float
    height_pips: float
    direction: str
    bull_zone_low: float
    bull_zone_high: float
    bear_zone_low: float
    bear_zone_high: float
    bull_formation_index: int
    bear_formation_index: int
    bars_apart: int
    overlap_pct: float
    state: str
    bars_since_formed: int
    created_at: Optional[int] = None
    broken_at: Optional[int] = None
    broken_side: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bpr_low": round(self.bpr_low, 6),
            "bpr_high": round(self.bpr_high, 6),
            "height_pips": round(self.height_pips, 2),
            "direction": self.direction,
            "bull_zone_low": round(self.bull_zone_low, 6),
            "bull_zone_high": round(self.bull_zone_high, 6),
            "bear_zone_low": round(self.bear_zone_low, 6),
            "bear_zone_high": round(self.bear_zone_high, 6),
            "bull_formation_index": self.bull_formation_index,
            "bear_formation_index": self.bear_formation_index,
            "bars_apart": self.bars_apart,
            "overlap_pct": round(self.overlap_pct, 2),
            "state": self.state,
            "bars_since_formed": self.bars_since_formed,
            "created_at": self.created_at,
            "broken_at": self.broken_at,
            "broken_side": self.broken_side,
        }

    @property
    def key(self) -> tuple:
        return (self.symbol, self.timeframe, round(self.bpr_low, 6), round(self.bpr_high, 6))


def _val(candle: dict, *keys, default=None):
    """Tolerant candle access, same tolerance as simple_snr._val."""
    if not isinstance(candle, dict):
        return default
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    return _val(candle, "open_time", "time", "timestamp", "date")


def _bound(candle, low_key, high_key) -> Optional[tuple]:
    low = _val(candle, low_key, low_key[0])
    high = _val(candle, high_key, high_key[0])
    if low is None or high is None:
        return None
    try:
        return float(low), float(high)
    except (TypeError, ValueError):
        return None


def find_fair_value_gaps(candles: list) -> list:
    """Every three candle imbalance in the window.

    Candle ``i`` is the void; it is only knowable once ``i+1`` has closed, so
    ``formation_index`` is ``i + 1`` and not ``i``.
    """
    gaps: list = []
    if len(candles) < 3:
        return gaps
    for i in range(1, len(candles) - 1):
        prev = _bound(candles[i - 1], "high", "low")
        nxt = _bound(candles[i + 1], "high", "low")
        if prev is None or nxt is None:
            continue
        prev_high, prev_low = prev
        next_high, next_low = nxt
        if prev_high < next_low:
            gaps.append(
                {
                    "kind": "BULLISH",
                    "low": prev_high,
                    "high": next_low,
                    "formation_index": i + 1,
                }
            )
        if prev_low > next_high:
            gaps.append(
                {
                    "kind": "BEARISH",
                    "low": next_high,
                    "high": prev_low,
                    "formation_index": i + 1,
                }
            )
    return gaps


def overlap_of(bull: dict, bear: dict) -> Optional[tuple]:
    """The shared band of two gaps, or None when they only touch."""
    low = max(bull["low"], bear["low"])
    high = min(bull["high"], bear["high"])
    if low >= high:
        return None
    return low, high


class BprEngine:
    def __init__(self, config: BprConfig = DEFAULT_BPR_CONFIG):
        self.config = config
        self._zones: dict[tuple, BalancedPriceRange] = {}
        self._last_bar: dict[tuple, object] = {}
        self._events: list = []

    def analyze(self, candles: list, tf: int = 0, symbol: str = "") -> list:
        if len(candles) < 3:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        bar = candles[-2]
        raw_price = _val(bar, "close", "c")
        if raw_price is None:
            return []
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return []
        ts = _time_key(candles[-1])
        bar_key = (symbol, tf)
        new_bar = ts is not None and ts != self._last_bar.get(bar_key)

        events: list = []

        if new_bar:
            for zone in self._zones.values():
                if zone.symbol == symbol and zone.timeframe == tf and zone.state == BprState.ACTIVE.value:
                    zone.bars_since_formed += 1

        gaps = find_fair_value_gaps(candles)
        bulls = [g for g in gaps if g["kind"] == "BULLISH"]
        bears = [g for g in gaps if g["kind"] == "BEARISH"]

        formed_now: set = set()
        for bull in bulls:
            for bear in bears:
                bars_apart = abs(bull["formation_index"] - bear["formation_index"])
                if bars_apart > self.config.max_bars_apart:
                    continue
                shared = overlap_of(bull, bear)
                if shared is None:
                    continue
                band_low, band_high = shared
                smaller = min(bull["high"] - bull["low"], bear["high"] - bear["low"])
                if smaller <= 0:
                    continue
                overlap_pct = (band_high - band_low) / smaller
                if overlap_pct < self.config.min_overlap_pct:
                    continue
                key = (symbol, tf, round(band_low, 6), round(band_high, 6))
                if key in self._zones:
                    continue
                if bull["formation_index"] > bear["formation_index"]:
                    direction = BprDirection.BULLISH.value
                else:
                    direction = BprDirection.BEARISH.value
                record = BalancedPriceRange(
                    symbol=symbol,
                    timeframe=tf,
                    bpr_low=band_low,
                    bpr_high=band_high,
                    height_pips=(band_high - band_low) / self.config.pip_size,
                    direction=direction,
                    bull_zone_low=bull["low"],
                    bull_zone_high=bull["high"],
                    bear_zone_low=bear["low"],
                    bear_zone_high=bear["high"],
                    bull_formation_index=bull["formation_index"],
                    bear_formation_index=bear["formation_index"],
                    bars_apart=bars_apart,
                    overlap_pct=overlap_pct,
                    state=BprState.ACTIVE.value,
                    bars_since_formed=0,
                    created_at=ts,
                )
                self._zones[key] = record
                formed_now.add(key)
                events.append(self._event(BprEvent.FORMED, record, ts))

        for zone in list(self._zones.values()):
            if zone.symbol != symbol or zone.timeframe != tf:
                continue
            if zone.state != BprState.ACTIVE.value or zone.key in formed_now:
                continue
            if price > zone.bpr_high:
                broken_side = "HIGH"
            elif price < zone.bpr_low:
                broken_side = "LOW"
            else:
                continue
            zone.state = BprState.BROKEN.value
            zone.broken_at = ts
            zone.broken_side = broken_side
            events.append(self._event(BprEvent.BROKEN, zone, ts))

        if ts is not None:
            self._last_bar[bar_key] = ts
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        if len(self._zones) > 200:
            self._zones = dict(list(self._zones.items())[-200:])
        return events

    def _event(self, event: BprEvent, record: BalancedPriceRange, timestamp, **extra) -> dict:
        payload = {"event": event.value, "timestamp": timestamp}
        payload.update(record.to_dict())
        payload.update(extra)
        return payload

    def get_active(self, limit: int = 10) -> list:
        return [z.to_dict() for z in list(self._zones.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list:
        return [z.to_dict() for z in list(self._zones.values())[-limit:]]

    def get_events(self, n: int = 5) -> list:
        return self._events[-n:]

    def reset(self) -> None:
        self._zones.clear()
        self._last_bar.clear()
        self._events.clear()

