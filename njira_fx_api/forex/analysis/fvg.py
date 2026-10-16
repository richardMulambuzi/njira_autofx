"""
Fair Value Gap Engine (Phase 3D — m.md §24).

Detects Fair Value Gaps (three-candle imbalances) and tracks their lifecycle.

A Fair Value Gap is a three-candle relationship where price leaves an untraded
"gap" between candle 1 and candle 3:

    Bullish FVG:  c3.low  > c1.high   (gap up,   imbalance to the upside)
    Bearish FVG:  c3.high < c1.low    (gap down, imbalance to the downside)

The untraded zone is the FVG: [lower_boundary, upper_boundary].  Price later
"fills" the gap by trading back into that zone.

Lifecycle: FORMED → OPEN → PARTIALLY_FILLED → FILLED → INVALIDATED

IDs are deterministic (symbol_timeframe_direction_formation_ms) so the same
FVG is never rediscovered across candle-close cycles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.fvg")


class FVGState(Enum):
    FORMED = "FORMED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    INVALIDATED = "INVALIDATED"


@dataclass
class FairValueGap:
    fvg_id: str                      # deterministic: "{symbol}_{tf}_{direction}_{open_time_ms}"
    symbol: str
    timeframe: int
    formation_time: datetime
    direction: str                  # "BULLISH" or "BEARISH"
    upper_boundary: float
    lower_boundary: float
    size: float                     # upper_boundary - lower_boundary
    origin: str = "three_candle_imbalance"
    status: FVGState = FVGState.FORMED
    filled_percentage: float = 0.0  # 0.0 to 1.0 (monotonic max penetration)
    fully_filled: bool = False
    invalidated: bool = False
    created_at: object = None
    # Internal extremes of the origin (c1) candle — used for invalidation only.
    origin_low: float = 0.0
    origin_high: float = 0.0

    def to_dict(self) -> dict:
        return {
            "fvg_id": self.fvg_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "formation_time": self.formation_time.isoformat() if isinstance(self.formation_time, datetime) else str(self.formation_time),
            "direction": self.direction,
            "upper_boundary": self.upper_boundary,
            "lower_boundary": self.lower_boundary,
            "size": self.size,
            "origin": self.origin,
            "status": self.status.value,
            "filled_percentage": round(self.filled_percentage, 4),
            "fully_filled": self.fully_filled,
            "invalidated": self.invalidated,
        }


class FVGEngine:
    """
    Per-symbol, per-timeframe Fair Value Gap detection and lifecycle.

    Usage:
        engine = FVGEngine()
        events = engine.analyze(candles, timeframe)
        fvgs = engine.get_active()
    """

    def __init__(
        self,
        max_fvgs: int = 50,
        invalidation_buffer: float = 0.0,
    ) -> None:
        self._max_fvgs = max_fvgs
        self._invalidation_buffer = invalidation_buffer
        self._fvgs: dict[str, FairValueGap] = {}  # fvg_id → FairValueGap
        self._events: list[dict] = []

    def analyze(
        self,
        candles: list[dict],
        timeframe: int = 0,
    ) -> list[dict]:
        """
        Analyze candles for FVG patterns and update existing FVG lifecycles.

        Args:
            candles: closed candles (chronological).  The last three define the
                     detection window; the last candle drives fill updates.
            timeframe: timeframe in seconds.  If 0, derived from the candle dict.

        Returns list of FVG events (new formations + lifecycle transitions).
        """
        if len(candles) < 3:
            return []

        tf = timeframe or int(candles[-1].get("timeframe", 0))
        if tf == 0:
            return []

        new_events: list[dict] = []

        # ── 1. Detect a new FVG from the last three candles ───────────────
        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        fvg = self._detect_fvg(c1, c2, c3, tf)
        if fvg is not None and fvg.fvg_id not in self._fvgs:
            self._fvgs[fvg.fvg_id] = fvg
            new_events.append(self._make_event("FVG_FORMED", fvg))
            self._prune()

        # ── 2. Update fill / lifecycle of all tracked FVGs ────────────────
        current = candles[-1]
        for fvg in self._fvgs.values():
            if fvg.status in (FVGState.FILLED, FVGState.INVALIDATED):
                continue
            prev_status = fvg.status
            self._update_fill(fvg, current)
            if fvg.status != prev_status:
                evt_name = {
                    FVGState.OPEN: "FVG_OPEN",
                    FVGState.PARTIALLY_FILLED: "FVG_PARTIALLY_FILLED",
                    FVGState.FILLED: "FVG_FILLED",
                    FVGState.INVALIDATED: "FVG_INVALIDATED",
                }.get(fvg.status, "FVG_UPDATED")
                new_events.append(self._make_event(evt_name, fvg))

        self._events.extend(new_events)
        if len(self._events) > 50:
            self._events = self._events[-50:]

        return new_events

    def get_active(
        self, symbol: str | None = None, timeframe: int | None = None
    ) -> list[FairValueGap]:
        """Return non-terminal FVGs (not FILLED / INVALIDATED), optional filter."""
        active = [
            f for f in self._fvgs.values()
            if f.status not in (FVGState.FILLED, FVGState.INVALIDATED)
        ]
        if symbol is not None:
            active = [f for f in active if f.symbol == symbol]
        if timeframe is not None:
            active = [f for f in active if f.timeframe == timeframe]
        return active

    def get_all(self, limit: int = 50) -> list[FairValueGap]:
        return list(self._fvgs.values())[-limit:]

    def get_events(self, n: int = 10) -> list[dict]:
        return self._events[-n:]

    # ── Private ─────────────────────────────────────────────────────────

    def _detect_fvg(
        self, c1: dict, c2: dict, c3: dict, timeframe: int
    ) -> Optional[FairValueGap]:
        """
        Detect a three-candle FVG pattern.

        Bullish FVG:  c3.low  > c1.high  → gap between c1.high and c3.low
        Bearish FVG:  c3.high < c1.low   → gap between c3.high and c1.low
        """
        try:
            c1_high, c1_low = float(c1["high"]), float(c1["low"])
            c3_high, c3_low = float(c3["high"]), float(c3["low"])
        except (KeyError, TypeError, ValueError):
            return None

        symbol = c1.get("symbol") or c3.get("symbol") or ""
        formation_time = c3.get("open_time") or c3.get("close_time")
        if formation_time is None:
            return None

        direction: Optional[str] = None
        upper = lower = 0.0

        if c3_low > c1_high:  # bullish gap up
            direction = "BULLISH"
            upper = c3_low
            lower = c1_high
        elif c3_high < c1_low:  # bearish gap down
            direction = "BEARISH"
            upper = c1_low
            lower = c3_high

        if direction is None:
            return None

        size = round(upper - lower, 6)
        if size <= 0:
            return None

        fvg_id = self._make_id(symbol, timeframe, direction, formation_time)
        return FairValueGap(
            fvg_id=fvg_id,
            symbol=symbol,
            timeframe=timeframe,
            formation_time=formation_time,
            direction=direction,
            upper_boundary=upper,
            lower_boundary=lower,
            size=size,
            status=FVGState.FORMED,
            created_at=formation_time,
            origin_low=c1_low,
            origin_high=c1_high,
        )

    def _update_fill(self, fvg: FairValueGap, current: dict) -> None:
        """Update fill percentage and lifecycle state from current price."""
        try:
            cur_low = float(current["low"])
            cur_high = float(current["high"])
        except (KeyError, TypeError, ValueError):
            return

        buf = self._invalidation_buffer
        size = fvg.size if fvg.size > 0 else 1e-9

        # ── Invalidation: price breaks the origin (c1) extreme ───────────
        if fvg.direction == "BULLISH":
            if cur_low < fvg.origin_low - buf:
                self._invalidate(fvg)
                return
        else:  # BEARISH
            if cur_high > fvg.origin_high + buf:
                self._invalidate(fvg)
                return

        # ── Fill penetration (monotonic max) ─────────────────────────────
        if fvg.direction == "BULLISH":
            # Gap fills from the top (upper_boundary) downward as price retraces.
            penetration = fvg.upper_boundary - cur_low
        else:
            # Gap fills from the bottom (lower_boundary) upward as price retraces.
            penetration = cur_high - fvg.lower_boundary

        fill = max(0.0, min(1.0, penetration / size))
        fvg.filled_percentage = max(fvg.filled_percentage, fill)

        # ── State transition ─────────────────────────────────────────────
        if fvg.filled_percentage >= 1.0:
            fvg.status = FVGState.FILLED
            fvg.fully_filled = True
        elif fvg.filled_percentage > 0.0:
            fvg.status = FVGState.PARTIALLY_FILLED
        else:
            fvg.status = FVGState.OPEN

    def _invalidate(self, fvg: FairValueGap) -> None:
        fvg.status = FVGState.INVALIDATED
        fvg.invalidated = True

    def _make_id(
        self, symbol: str, timeframe: int, direction: str, formation_time: datetime
    ) -> str:
        ms = int(formation_time.timestamp() * 1000)
        return f"{symbol}_{timeframe}_{direction}_{ms}"

    def _make_event(self, event: str, fvg: FairValueGap) -> dict:
        return {
            "type": "fvg",
            "event": event,
            "fvg_id": fvg.fvg_id,
            "symbol": fvg.symbol,
            "timeframe": fvg.timeframe,
            "direction": fvg.direction,
            "upper_boundary": fvg.upper_boundary,
            "lower_boundary": fvg.lower_boundary,
            "size": fvg.size,
            "status": fvg.status.value,
            "filled_percentage": round(fvg.filled_percentage, 4),
            "fully_filled": fvg.fully_filled,
            "invalidated": fvg.invalidated,
        }

    def _prune(self) -> None:
        """Cap stored FVGs, evicting terminal (oldest) entries first."""
        if len(self._fvgs) <= self._max_fvgs:
            return
        terminal = [
            f for f in self._fvgs.values()
            if f.status in (FVGState.FILLED, FVGState.INVALIDATED)
        ]
        terminal.sort(key=lambda f: f.created_at or 0)
        for f in terminal:
            if len(self._fvgs) <= self._max_fvgs:
                break
            self._fvgs.pop(f.fvg_id, None)
        # If still over cap, evict oldest regardless of state.
        if len(self._fvgs) > self._max_fvgs:
            oldest = sorted(self._fvgs.values(), key=lambda f: f.created_at or 0)
            for f in oldest:
                self._fvgs.pop(f.fvg_id, None)
                if len(self._fvgs) <= self._max_fvgs:
                    break

    def reset(self) -> None:
        self._fvgs.clear()
        self._events.clear()
