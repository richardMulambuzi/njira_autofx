"""
Liquidity Detection Engine (Phase 3B — improvements.md §26).

Detects liquidity pools (equal highs/lows, PDH/PDL, swing H/L, etc.)
and identifies sweeps with configurable contextual confirmation.

State machine: POOL → SWEPT → CONFIRMED → INVALIDATED

Never calls every wick a sweep — requires displacement or structure shift
for confirmation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.liquidity")


class LiquidityType(Enum):
    EQUAL_HIGH = "EQUAL_HIGH"
    EQUAL_LOW = "EQUAL_LOW"
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"
    PDH = "PDH"  # Previous Day High
    PDL = "PDL"  # Previous Day Low
    PWH = "PWH"  # Previous Week High
    PWL = "PWL"  # Previous Week Low
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"
    RANGE_HIGH = "RANGE_HIGH"
    RANGE_LOW = "RANGE_LOW"


class PoolState(Enum):
    POOL = "POOL"
    SWEPT = "SWEPT"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


@dataclass
class LiquidityPool:
    pool_type: LiquidityType
    price: float
    state: PoolState = PoolState.POOL
    created_at: object = None
    swept_at: object = None
    confirmed_at: object = None
    index: int = 0  # candle index when detected
    touches: int = 1  # how many times price tested this level
    tolerance_pips: float = 0.5  # price tolerance for "equal" levels


class LiquidityEngine:
    """
    Per-symbol liquidity pool detection and sweep identification.

    Usage:
        engine = LiquidityEngine()
        events = engine.analyze(candles, structure_events)
        pools = engine.get_pools(state="SWEPT")
    """

    def __init__(
        self,
        equal_level_tolerance_pips: float = 0.5,
        sweep_confirmation_candles: int = 3,
        max_pools: int = 50,
    ) -> None:
        self._tolerance = equal_level_tolerance_pips
        self._sweep_confirm = sweep_confirmation_candles
        self._max_pools = max_pools
        self._pools: list[LiquidityPool] = []
        self._events: list[dict] = []

    def analyze(
        self,
        candles: list[dict],
        structure_events: list[dict] | None = None,
    ) -> list[dict]:
        """
        Analyze candles for liquidity pools and sweep confirmation.

        Args:
            candles: closed candles from hot cache (chronological)
            structure_events: recent structure events (for contextual confirmation)

        Returns list of liquidity events detected this cycle.
        """
        if len(candles) < 5:
            return []

        new_events: list[dict] = []

        # ── 1. Detect equal highs / lows ────────────────────────────────
        new_events.extend(self._detect_equal_levels(candles))

        # ── 2. Detect swing high/low pools from structure ────────────────
        new_events.extend(self._detect_swing_pools(candles))

        # ── 3. Check for sweeps ─────────────────────────────────────────
        sweep_events = self._check_sweeps(candles, structure_events)
        new_events.extend(sweep_events)

        # ── 4. Invalidate stale pools ───────────────────────────────────
        self._invalidate_stale(candles)

        self._events.extend(new_events)
        if len(self._events) > 50:
            self._events = self._events[-50:]

        return new_events

    def get_pools(self, state: Optional[str] = None, limit: int = 20) -> list[LiquidityPool]:
        """Return pools, optionally filtered by state."""
        pools = self._pools
        if state:
            pools = [p for p in pools if p.state.value == state]
        return pools[-limit:]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    # ── Private ─────────────────────────────────────────────────────────

    def _detect_equal_levels(self, candles: list[dict]) -> list[dict]:
        """Detect clusters of approximately equal highs or lows."""
        events: list[dict] = []
        if len(candles) < 3:
            return events

        # Check last 20 candles for equal levels
        recent = candles[-20:]
        highs = [(i, c["high"]) for i, c in enumerate(recent)]
        lows = [(i, c["low"]) for i, c in enumerate(recent)]

        # Equal highs: two or more highs within tolerance
        for i in range(len(highs)):
            cluster = [highs[i]]
            for j in range(i + 1, len(highs)):
                if abs(highs[i][1] - highs[j][1]) < self._tolerance * 0.0001:
                    cluster.append(highs[j])
            if len(cluster) >= 2:
                avg_price = sum(p for _, p in cluster) / len(cluster)
                # ponytail: dedup by type+price — O(n) scan, fine for max 50 pools
                if any(p.pool_type == LiquidityType.EQUAL_HIGH
                       and abs(p.price - avg_price) < self._tolerance * 0.0001
                       for p in self._pools):
                    continue
                pool = LiquidityPool(
                    pool_type=LiquidityType.EQUAL_HIGH,
                    price=avg_price,
                    touches=len(cluster),
                    tolerance_pips=self._tolerance,
                )
                self._pools.append(pool)
                evt = {
                    "type": "liquidity",
                    "event": "POOL_DETECTED",
                    "pool_type": "EQUAL_HIGH",
                    "price": avg_price,
                    "touches": len(cluster),
                }
                events.append(evt)

        # Equal lows
        for i in range(len(lows)):
            cluster = [lows[i]]
            for j in range(i + 1, len(lows)):
                if abs(lows[i][1] - lows[j][1]) < self._tolerance * 0.0001:
                    cluster.append(lows[j])
            if len(cluster) >= 2:
                avg_price = sum(p for _, p in cluster) / len(cluster)
                if any(p.pool_type == LiquidityType.EQUAL_LOW
                       and abs(p.price - avg_price) < self._tolerance * 0.0001
                       for p in self._pools):
                    continue
                pool = LiquidityPool(
                    pool_type=LiquidityType.EQUAL_LOW,
                    price=avg_price,
                    touches=len(cluster),
                    tolerance_pips=self._tolerance,
                )
                self._pools.append(pool)
                evt = {
                    "type": "liquidity",
                    "event": "POOL_DETECTED",
                    "pool_type": "EQUAL_LOW",
                    "price": avg_price,
                    "touches": len(cluster),
                }
                events.append(evt)

        # Cap pools
        if len(self._pools) > self._max_pools:
            self._pools = self._pools[-self._max_pools:]

        return events

    def _detect_swing_pools(self, candles: list[dict]) -> list[dict]:
        """Register swing highs/lows as liquidity pools."""
        events: list[dict] = []
        if len(candles) < 5:
            return events

        last = candles[-1]
        prev = candles[-2]

        # Simple swing detection (3-candle pattern)
        if len(candles) >= 3:
            c = candles[-2]
            if c["high"] > candles[-3]["high"] and c["high"] > last["high"]:
                pool = LiquidityPool(
                    pool_type=LiquidityType.SWING_HIGH,
                    price=c["high"],
                    index=len(candles) - 2,
                )
                self._pools.append(pool)
                events.append({
                    "type": "liquidity",
                    "event": "POOL_DETECTED",
                    "pool_type": "SWING_HIGH",
                    "price": c["high"],
                })

            if c["low"] < candles[-3]["low"] and c["low"] < last["low"]:
                pool = LiquidityPool(
                    pool_type=LiquidityType.SWING_LOW,
                    price=c["low"],
                    index=len(candles) - 2,
                )
                self._pools.append(pool)
                events.append({
                    "type": "liquidity",
                    "event": "POOL_DETECTED",
                    "pool_type": "SWING_LOW",
                    "price": c["low"],
                })

        return events

    def _check_sweeps(
        self, candles: list[dict], structure_events: list[dict] | None,
    ) -> list[dict]:
        """
        Check if recent price action swept any active liquidity pool.

        A sweep = price breaches a pool level then reverses back.
        Requires contextual confirmation (displacement or structure event).
        """
        events: list[dict] = []
        if len(candles) < 3:
            return events

        last = candles[-1]
        prev = candles[-2]
        has_structure_shift = False

        if structure_events:
            for se in structure_events:
                evt_name = se.get("event", "")
                if "MSS" in evt_name or "CHOCH" in evt_name:
                    has_structure_shift = True
                    break

        for pool in self._pools:
            if pool.state != PoolState.POOL:
                continue

            swept = False
            if pool.pool_type in (LiquidityType.EQUAL_HIGH, LiquidityType.SWING_HIGH,
                                   LiquidityType.SWING_LOW, LiquidityType.EQUAL_LOW):
                # High pool: wick above then close below
                if pool.pool_type.value.endswith("HIGH"):
                    if last["high"] > pool.price and last["close"] < pool.price:
                        swept = True
                # Low pool: wick below then close above
                else:
                    if last["low"] < pool.price and last["close"] > pool.price:
                        swept = True

            if swept:
                # Require contextual confirmation
                has_displacement = self._check_displacement(candles)
                if has_structure_shift or has_displacement:
                    pool.state = PoolState.SWEPT
                    pool.swept_at = last.get("open_time")
                    evt = {
                        "type": "liquidity",
                        "event": "SWEEP_DETECTED",
                        "pool_type": pool.pool_type.value,
                        "price": pool.price,
                        "confirmation": "structure_shift" if has_structure_shift else "displacement",
                    }
                    events.append(evt)

        return events

    def _check_displacement(self, candles: list[dict]) -> bool:
        """Check if recent candle shows displacement (strong directional move)."""
        if len(candles) < 6:
            return False
        last = candles[-1]
        avg_range = sum(
            c["high"] - c["low"] for c in candles[-6:-1]
        ) / 5
        return avg_range > 0 and (last["high"] - last["low"]) > avg_range * 1.5

    def _invalidate_stale(self, candles: list[dict]) -> None:
        """Invalidate pools that price has moved decisively beyond."""
        if not candles:
            return
        last_close = candles[-1]["close"]

        for pool in self._pools:
            if pool.state in (PoolState.INVALIDATED, PoolState.CONFIRMED):
                continue
            # Invalidate if price moved > 2x tolerance beyond the level
            buffer = self._tolerance * 0.0001 * 2
            if pool.pool_type.value.endswith("HIGH") and last_close < pool.price - buffer:
                pool.state = PoolState.INVALIDATED
            elif pool.pool_type.value.endswith("LOW") and last_close > pool.price + buffer:
                pool.state = PoolState.INVALIDATED

    def reset(self) -> None:
        self._pools.clear()
        self._events.clear()
