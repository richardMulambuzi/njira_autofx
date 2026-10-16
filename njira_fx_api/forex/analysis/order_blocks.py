"""
Order Block Engine (Phase 3C — improvements.md §27–28, §48).

Detects OB candidates (last opposing candle before displacement move),
scores quality, and manages lifecycle.

Lifecycle: CANDIDATE → VALID → PARTIALLY_MITIGATED → MITIGATED → INVALIDATED → EXPIRED
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.order_blocks")


class OBDirection(Enum):
    BULLISH = "BULLISH_OB"
    BEARISH = "BEARISH_OB"


class OBState(Enum):
    CANDIDATE = "CANDIDATE"
    VALID = "VALID"
    PARTIALLY_MITIGATED = "PARTIALLY_MITIGATED"
    MITIGATED = "MITIGATED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass
class OrderBlock:
    direction: OBDirection
    state: OBState = OBState.CANDIDATE
    high: float = 0.0
    low: float = 0.0
    created_at: object = None
    timeframe: int = 0
    source_time: object = None  # open_time of the OB candle (stable across deque shifts)
    quality_score: float = 0.0
    mitigation_count: int = 0
    displacement_strength: float = 0.0
    invalidation_reason: str = ""
    structure_event_id: str = ""
    liquidity_interacted: bool = False
    poc_proximity: bool = False


@dataclass
class OBQualityWeights:
    """Configurable weights for OB quality scoring."""
    displacement: float = 0.25
    structure_alignment: float = 0.20
    body_strength: float = 0.15
    wick_ratio: float = 0.10
    liquidity: float = 0.10
    poc_proximity: float = 0.10
    freshness: float = 0.10


DEFAULT_WEIGHTS = OBQualityWeights()


class OrderBlockEngine:
    """
    Per-symbol, per-timeframe order block detection and lifecycle.

    Usage:
        engine = OrderBlockEngine()
        events = engine.analyze(candles, structure_events, poc_price)
        obs = engine.get_active()
    """

    def __init__(
        self,
        displacement_threshold: float = 1.5,
        max_obs: int = 30,
        max_age_candles: int = 100,
        weights: OBQualityWeights | None = None,
    ) -> None:
        self._displacement_threshold = displacement_threshold
        self._max_obs = max_obs
        self._max_age = max_age_candles
        self._weights = weights or DEFAULT_WEIGHTS
        self._obs: list[OrderBlock] = []
        self._events: list[dict] = []

    def analyze(
        self,
        candles: list[dict],
        structure_events: list[dict] | None = None,
        poc_price: float | None = None,
    ) -> list[dict]:
        """
        Analyze candles for order blocks.

        Returns list of OB events detected this cycle.
        """
        if len(candles) < 6:
            return []

        new_events: list[dict] = []

        # ── 1. Detect new OB candidates ─────────────────────────────────
        new_events.extend(self._detect_candidates(candles, structure_events, poc_price))

        # ── 2. Update lifecycle of existing OBs ─────────────────────────
        new_events.extend(self._update_lifecycle(candles))

        # ── 3. Score quality of valid OBs ───────────────────────────────
        self._score_quality(candles, structure_events, poc_price)

        self._events.extend(new_events)
        if len(self._events) > 50:
            self._events = self._events[-50:]

        return new_events

    def get_active(self, limit: int = 10) -> list[OrderBlock]:
        """Return active (VALID / PARTIALLY_MITIGATED) order blocks."""
        active = [
            ob for ob in self._obs
            if ob.state in (OBState.VALID, OBState.PARTIALLY_MITIGATED)
        ]
        return sorted(active, key=lambda o: o.quality_score, reverse=True)[:limit]

    def get_all(self, limit: int = 20) -> list[OrderBlock]:
        return self._obs[-limit:]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    # ── Private ─────────────────────────────────────────────────────────

    def _detect_candidates(
        self, candles: list[dict], structure_events: list[dict] | None,
        poc_price: float | None,
    ) -> list[dict]:
        """
        Detect OB candidates: last opposing candle before a displacement move.

        Bullish OB: last bearish candle before a strong bullish move.
        Bearish OB: last bullish candle before a strong bearish move.
        """
        events: list[dict] = []
        if len(candles) < 5:
            return events

        last_5 = candles[-5:]
        last = last_5[-1]
        prev = last_5[-2]

        # Calculate average range for displacement detection
        ranges = [c["high"] - c["low"] for c in candles[-10:-5]] if len(candles) >= 10 else [1.0]
        avg_range = sum(ranges) / len(ranges) if ranges else 1.0

        last_range = last["high"] - last["low"]
        is_displacement = avg_range > 0 and last_range > avg_range * self._displacement_threshold

        if not is_displacement:
            return events

        # ── Bullish OB: bearish candle → strong bullish displacement ────
        if last["close"] > last["open"] and prev["close"] < prev["open"]:
            ob = OrderBlock(
                direction=OBDirection.BULLISH,
                state=OBState.CANDIDATE,
                high=prev["high"],
                low=prev["low"],
                created_at=prev.get("open_time"),
                source_time=prev.get("open_time"),
                displacement_strength=last_range / avg_range if avg_range > 0 else 1.0,
            )
            self._obs.append(ob)
            evt = {
                "type": "order_block",
                "event": "CANDIDATE_DETECTED",
                "direction": "BULLISH_OB",
                "high": ob.high,
                "low": ob.low,
                "displacement": round(ob.displacement_strength, 2),
            }
            events.append(evt)

        # ── Bearish OB: bullish candle → strong bearish displacement ────
        elif last["close"] < last["open"] and prev["close"] > prev["open"]:
            ob = OrderBlock(
                direction=OBDirection.BEARISH,
                state=OBState.CANDIDATE,
                high=prev["high"],
                low=prev["low"],
                created_at=prev.get("open_time"),
                source_time=prev.get("open_time"),
                displacement_strength=last_range / avg_range if avg_range > 0 else 1.0,
            )
            self._obs.append(ob)
            evt = {
                "type": "order_block",
                "event": "CANDIDATE_DETECTED",
                "direction": "BEARISH_OB",
                "high": ob.high,
                "low": ob.low,
                "displacement": round(ob.displacement_strength, 2),
            }
            events.append(evt)

        # Cap order blocks
        if len(self._obs) > self._max_obs:
            self._obs = self._obs[-self._max_obs:]

        return events

    def _update_lifecycle(self, candles: list[dict]) -> list[dict]:
        """Update OB states based on price interaction."""
        events: list[dict] = []
        if not candles:
            return events

        last_close = candles[-1]["close"]
        candle_count = len(candles)

        for ob in self._obs:
            if ob.state in (OBState.MITIGATED, OBState.INVALIDATED, OBState.EXPIRED):
                continue

            # ── Age expiration (find OB candle by open_time) ─────────────
            src_idx = next((i for i, c in enumerate(candles)
                           if c.get("open_time") == ob.source_time), None)
            if src_idx is None:
                ob.state = OBState.EXPIRED
                events.append(self._make_event("EXPIRED", ob))
                continue
            age = candle_count - src_idx
            if age > self._max_age:
                ob.state = OBState.EXPIRED
                events.append(self._make_event("EXPIRED", ob))
                continue

            # ── Check mitigation (price trades through the OB) ──────────
            if ob.direction == OBDirection.BULLISH:
                if last_close < ob.low:
                    ob.mitigation_count += 1
                    if ob.mitigation_count >= 2:
                        ob.state = OBState.MITIGATED
                        events.append(self._make_event("MITIGATED", ob))
                    else:
                        ob.state = OBState.PARTIALLY_MITIGATED
                        events.append(self._make_event("PARTIALLY_MITIGATED", ob))
            else:  # BEARISH
                if last_close > ob.high:
                    ob.mitigation_count += 1
                    if ob.mitigation_count >= 2:
                        ob.state = OBState.MITIGATED
                        events.append(self._make_event("MITIGATED", ob))
                    else:
                        ob.state = OBState.PARTIALLY_MITIGATED
                        events.append(self._make_event("PARTIALLY_MITIGATED", ob))

            # ── Promote candidate to valid (survived 2+ candles) ────────
            if ob.state == OBState.CANDIDATE and age >= 2:
                ob.state = OBState.VALID
                events.append(self._make_event("VALID", ob))

        return events

    def _score_quality(
        self, candles: list[dict], structure_events: list[dict] | None,
        poc_price: float | None,
    ) -> None:
        """Score quality of active order blocks."""
        w = self._weights

        for ob in self._obs:
            if ob.state not in (OBState.VALID, OBState.PARTIALLY_MITIGATED):
                continue

            score = 0.0

            # Displacement strength (0-25)
            score += min(25, ob.displacement_strength * 10) * w.displacement / 0.25

            # Structure alignment — check if structure events support this OB
            if structure_events:
                for se in structure_events:
                    evt = se.get("event", "")
                    if ob.direction == OBDirection.BULLISH and "BULLISH" in evt:
                        score += 20 * w.structure_alignment / 0.20
                        break
                    elif ob.direction == OBDirection.BEARISH and "BEARISH" in evt:
                        score += 20 * w.structure_alignment / 0.20
                        break

            # Body strength — OB candle body vs range
            src_idx = next((i for i, c in enumerate(candles)
                           if c.get("open_time") == ob.source_time), None)
            if src_idx is not None:
                src = candles[src_idx]
                body = abs(src["close"] - src["open"])
                rng = src["high"] - src["low"]
                if rng > 0:
                    score += (body / rng) * 15 * w.body_strength / 0.15

            # Freshness — newer is better
            if src_idx is not None:
                age = len(candles) - src_idx
                freshness = max(0, 1 - age / self._max_age)
                score += freshness * 10 * w.freshness / 0.10

            # POC proximity
            if poc_price and ob.low <= poc_price <= ob.high:
                score += 10 * w.poc_proximity / 0.10
                ob.poc_proximity = True

            # Mitigation penalty
            score -= ob.mitigation_count * 5

            ob.quality_score = round(max(0, min(100, score)), 1)

    def _make_event(self, event: str, ob: OrderBlock) -> dict:
        return {
            "type": "order_block",
            "event": event,
            "direction": ob.direction.value,
            "state": ob.state.value,
            "high": ob.high,
            "low": ob.low,
            "quality_score": ob.quality_score,
            "timeframe": ob.timeframe,
        }

    def reset(self) -> None:
        self._obs.clear()
        self._events.clear()
