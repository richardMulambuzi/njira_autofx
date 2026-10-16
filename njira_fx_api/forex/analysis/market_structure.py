"""
Market Structure Engine (Phase 3A — improvements.md §25).

Detects:
- Swing highs / swing lows (configurable lookback)
- HH / HL / LH / LL classification
- BOS (Break of Structure)
- CHoCH (Change of Character)
- MSS (Market Structure Shift = CHoCH + displacement)
- Trend / range / consolidation / expansion classification

Runs independently per timeframe from the hot cache. No direct
ClickHouse queries in the hot path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.market_structure")


# ── Enums ──────────────────────────────────────────────────────────────
class SwingType(Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureType(Enum):
    HH = "HH"  # Higher High
    HL = "HL"  # Higher Low
    LH = "LH"  # Lower High
    LL = "LL"  # Lower Low


class Bias(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MarketRegime(Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    CONSOLIDATING = "CONSOLIDATING"
    EXPANSION = "EXPANSION"


class StructureEvent(Enum):
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"
    MSS_BULLISH = "MSS_BULLISH"
    MSS_BEARISH = "MSS_BEARISH"


# ── Data ───────────────────────────────────────────────────────────────
@dataclass
class SwingPoint:
    swing_type: SwingType
    price: float
    index: int  # candle index in the cache
    timestamp: object  # open_time of the candle


@dataclass
class StructureState:
    """Mutable state tracked per timeframe."""
    swing_highs: list[SwingPoint] = field(default_factory=list)
    swing_lows: list[SwingPoint] = field(default_factory=list)
    last_structure: Optional[StructureType] = None
    bias: Bias = Bias.NEUTRAL
    regime: MarketRegime = MarketRegime.RANGING
    events: list[dict] = field(default_factory=list)  # recent events (capped)


# ── Config ─────────────────────────────────────────────────────────────
DEFAULT_SWING_LOOKBACK = 5       # candles each side for swing detection
DEFAULT_SWING_WINDOW = 50        # max swing points kept
DEFAULT_EVENT_BUFFER = 20        # recent events kept
DEFAULT_DISPLACEMENT_MULT = 1.5  # candle range > mult * avg range = displacement
DEFAULT_RANGE_RATIO = 0.5        # ATR range / ATR > this = trending


class MarketStructureEngine:
    """
    Per-symbol, per-timeframe market structure analysis.

    Usage:
        engine = MarketStructureEngine()
        events = engine.analyze(candles)  # candles from hot cache
        state = engine.get_state(tf)
    """

    def __init__(
        self,
        swing_lookback: int = DEFAULT_SWING_LOOKBACK,
        swing_window: int = DEFAULT_SWING_WINDOW,
        event_buffer: int = DEFAULT_EVENT_BUFFER,
        displacement_mult: float = DEFAULT_DISPLACEMENT_MULT,
        range_ratio: float = DEFAULT_RANGE_RATIO,
    ) -> None:
        self._lookback = swing_lookback
        self._swing_window = swing_window
        self._event_buffer = event_buffer
        self._displacement_mult = displacement_mult
        self._range_ratio = range_ratio
        self._states: dict[int, StructureState] = {}  # tf → state

    def analyze(self, candles: list[dict], tf: int) -> list[dict]:
        """
        Run full structure analysis on the candle list for one timeframe.

        Returns a list of structure events detected this cycle (empty if
        nothing new).  Always safe to call repeatedly — the engine tracks
        state incrementally.
        """
        if len(candles) < self._lookback * 2 + 1:
            return []

        state = self._get_state(tf)
        new_events: list[dict] = []

        # ── 1. Detect swing points ──────────────────────────────────────
        for i in range(self._lookback, len(candles) - self._lookback):
            self._detect_swing(candles, i, state)

        # ── 2. Classify swing sequence (HH/HL/LH/LL) ───────────────────
        classifications = self._classify_swings(state)

        # ── 3. Detect BOS / CHoCH / MSS ────────────────────────────────
        events = self._detect_structure_breaks(candles, state, classifications, tf)
        new_events.extend(events)

        # ── 4. Detect displacement ──────────────────────────────────────
        self._detect_displacement(candles, state)

        # ── 5. Classify regime ──────────────────────────────────────────
        self._classify_regime(candles, state)

        return new_events

    def get_state(self, tf: int) -> Optional[StructureState]:
        return self._states.get(tf)

    def get_bias(self, tf: int) -> Bias:
        state = self._states.get(tf)
        return state.bias if state else Bias.NEUTRAL

    def get_regime(self, tf: int) -> MarketRegime:
        state = self._states.get(tf)
        return state.regime if state else MarketRegime.RANGING

    def get_events(self, tf: int, n: int = 5) -> list[dict]:
        state = self._states.get(tf)
        if state is None:
            return []
        return state.events[-n:]

    # ── Private ─────────────────────────────────────────────────────────

    def _get_state(self, tf: int) -> StructureState:
        if tf not in self._states:
            self._states[tf] = StructureState()
        return self._states[tf]

    def _detect_swing(self, candles: list[dict], i: int, state: StructureState) -> None:
        """Detect if candle i is a swing high or swing low."""
        candle = candles[i]
        high = candle["high"]
        low = candle["low"]
        lb = self._lookback

        # Swing high: highest high in the lookback window
        is_high = all(
            candles[j]["high"] <= high
            for j in range(i - lb, i + lb + 1)
            if j != i and 0 <= j < len(candles)
        )
        if is_high:
            sp = SwingPoint(SwingType.HIGH, high, i, candle.get("open_time"))
            # Avoid duplicates at same price
            if not state.swing_highs or state.swing_highs[-1].price != high:
                state.swing_highs.append(sp)
                if len(state.swing_highs) > self._swing_window:
                    state.swing_highs.pop(0)

        # Swing low: lowest low in the lookback window
        is_low = all(
            candles[j]["low"] >= low
            for j in range(i - lb, i + lb + 1)
            if j != i and 0 <= j < len(candles)
        )
        if is_low:
            sp = SwingPoint(SwingType.LOW, low, i, candle.get("open_time"))
            if not state.swing_lows or state.swing_lows[-1].price != low:
                state.swing_lows.append(sp)
                if len(state.swing_lows) > self._swing_window:
                    state.swing_lows.pop(0)

    def _classify_swings(self, state: StructureState) -> list[StructureType]:
        """Classify the sequence of swing highs/lows as HH/HL/LH/LL."""
        classifications: list[StructureType] = []
        highs = state.swing_highs
        lows = state.swing_lows

        # Classify highs: compare each to previous
        for i in range(1, len(highs)):
            if highs[i].price > highs[i - 1].price:
                classifications.append(StructureType.HH)
            elif highs[i].price < highs[i - 1].price:
                classifications.append(StructureType.LH)

        # Classify lows
        for i in range(1, len(lows)):
            if lows[i].price > lows[i - 1].price:
                classifications.append(StructureType.HL)
            elif lows[i].price < lows[i - 1].price:
                classifications.append(StructureType.LL)

        return classifications

    def _detect_structure_breaks(
        self, candles: list[dict], state: StructureState,
        classifications: list[StructureType], tf: int = 0,
    ) -> list[dict]:
        """Detect BOS, CHoCH, and MSS from swing sequence."""
        events: list[dict] = []
        if len(state.swing_highs) < 2 or len(state.swing_lows) < 2:
            return events

        last_close = candles[-1]["close"]
        prev_bias = state.bias

        # ── BOS Bullish: close > last swing high ────────────────────────
        last_sh = state.swing_highs[-1]
        if last_close > last_sh.price:
            event_type = StructureEvent.BOS_BULLISH
            state.bias = Bias.BULLISH
            evt = self._make_event(event_type, tf=tf, price=last_close,
                                   ref_price=last_sh.price)
            events.append(evt)
            state.events.append(evt)
            if len(state.events) > self._event_buffer:
                state.events.pop(0)

        # ── BOS Bearish: close < last swing low ────────────────────────
        last_sl = state.swing_lows[-1]
        if last_close < last_sl.price:
            event_type = StructureEvent.BOS_BEARISH
            state.bias = Bias.BEARISH
            evt = self._make_event(event_type, tf=tf, price=last_close,
                                   ref_price=last_sl.price)
            events.append(evt)
            state.events.append(evt)
            if len(state.events) > self._event_buffer:
                state.events.pop(0)

        # ── CHoCH: bias reversal ────────────────────────────────────────
        if prev_bias != Bias.NEUTRAL and state.bias != prev_bias:
            if state.bias == Bias.BULLISH:
                event_type = StructureEvent.CHOCH_BULLISH
            else:
                event_type = StructureEvent.CHOCH_BEARISH
            evt = self._make_event(event_type, tf=tf, price=last_close,
                                   ref_price=0.0)
            events.append(evt)
            state.events.append(evt)
            if len(state.events) > self._event_buffer:
                state.events.pop(0)

            # ── MSS: CHoCH + displacement ───────────────────────────────
            if self._has_displacement(candles):
                if state.bias == Bias.BULLISH:
                    mss_type = StructureEvent.MSS_BULLISH
                else:
                    mss_type = StructureEvent.MSS_BEARISH
                evt = self._make_event(mss_type, tf=tf, price=last_close,
                                       ref_price=0.0)
                events.append(evt)
                state.events.append(evt)
                if len(state.events) > self._event_buffer:
                    state.events.pop(0)

        return events

    def _has_displacement(self, candles: list[dict], lookback: int = 5) -> bool:
        """Check if the most recent candle(s) show displacement (strong move)."""
        if len(candles) < lookback + 1:
            return False
        recent = candles[-1]
        recent_range = recent["high"] - recent["low"]
        avg_range = sum(
            candles[i]["high"] - candles[i]["low"]
            for i in range(-lookback - 1, -1)
        ) / lookback
        return avg_range > 0 and recent_range > avg_range * self._displacement_mult

    def _detect_displacement(self, candles: list[dict], state: StructureState) -> None:
        """Annotate the latest candle with displacement info."""
        # Displacement is checked inline during structure break detection.

    def _classify_regime(self, candles: list[dict], state: StructureState) -> None:
        """Classify market regime based on recent candle behavior."""
        if len(candles) < 20:
            state.regime = MarketRegime.RANGING
            return

        recent = candles[-20:]
        ranges = [c["high"] - c["low"] for c in recent]
        avg_range = sum(ranges) / len(ranges) if ranges else 0

        # Count directional candles
        up = sum(1 for c in recent if c["close"] > c["open"])
        down = len(recent) - up

        # Expansion: large recent candles
        if ranges and ranges[-1] > avg_range * 2:
            state.regime = MarketRegime.EXPANSION
        # Consolidation: very small range
        elif avg_range > 0 and ranges[-1] < avg_range * 0.3:
            state.regime = MarketRegime.CONSOLIDATING
        # Trending: strong directional bias
        elif abs(up - down) > len(recent) * 0.6:
            state.regime = MarketRegime.TRENDING
        else:
            state.regime = MarketRegime.RANGING

    def _make_event(
        self, event_type: StructureEvent, tf: int,
        price: float, ref_price: float,
    ) -> dict:
        return {
            "type": "structure",
            "event": event_type.value,
            "timeframe": tf,
            "price": price,
            "ref_price": ref_price,
        }

    def reset(self) -> None:
        self._states.clear()
