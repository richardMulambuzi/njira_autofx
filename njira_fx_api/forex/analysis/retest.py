"""
Retest Engine (improve.md §34–§35 — audit sequencing item 7).

A level is not a signal. Price reaction to the level is the evidence. This
engine evaluates whether price actually REACTED to a zone (order block, FVG,
POC, value area) rather than merely touching it.

For every important zone it determines: approach, touch, penetration,
rejection, acceptance, displacement, retest, failure, invalidation. A retest
is only confirmed when the reaction is evaluated — not when price simply
touches the zone.

Best-effort by design: any missing zone, empty candle list, or unexpected
shape returns ``None`` and never raises, so it can be dropped into the hot
candle-close pipeline without risk.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("forex.analysis.retest")

# Look-back window (in closed candles) used to detect a touch.
_TOUCH_WINDOW = 5
# A touch within this many candles of "now" with no resolved reaction is PENDING.
_PENDING_WINDOW = 2


@dataclass
class RetestEvent:
    """Outcome of evaluating price reaction to a zone."""

    zone_type: str               # "order_block" | "fvg" | "poc" | "value_area"
    direction: str               # "BULLISH" | "BEARISH"
    zone_top: float
    zone_bottom: float
    approach: str                # "from_above" | "from_below"
    touch_ts: object = None      # open_time / close_time of the touch candle
    touch_price: float = 0.0
    penetration_depth: float = 0.0   # price units beyond the zone (0 if none)
    reaction: str = "PENDING"    # "REJECTED" | "ACCEPTED" | "PENDING" | "FAILED"
    reaction_strength: float = 0.0   # 0.0 - 1.0
    candles_since_touch: int = 0
    invalidation_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "zone_type": self.zone_type,
            "direction": self.direction,
            "zone_top": self.zone_top,
            "zone_bottom": self.zone_bottom,
            "approach": self.approach,
            "touch_ts": self.touch_ts,
            "touch_price": self.touch_price,
            "penetration_depth": self.penetration_depth,
            "reaction": self.reaction,
            "reaction_strength": round(self.reaction_strength, 4),
            "candles_since_touch": self.candles_since_touch,
            "invalidation_reason": self.invalidation_reason,
        }


def _normalize_direction(direction: str) -> str:
    d = (direction or "").upper()
    if d in ("BUY", "BULLISH_OB", "BULLISH"):
        return "BULLISH"
    if d in ("SELL", "BEARISH_OB", "BEARISH"):
        return "BEARISH"
    return d


def _zone_bounds(zone: dict) -> tuple[float, float]:
    """Extract (top, bottom) from a zone dict, tolerating OB/FVG/POC shapes."""
    top = (
        zone.get("zone_top")
        if zone.get("zone_top") is not None
        else zone.get("high", zone.get("upper_boundary", 0.0))
    )
    bottom = (
        zone.get("zone_bottom")
        if zone.get("zone_bottom") is not None
        else zone.get("low", zone.get("lower_boundary", 0.0))
    )
    top = float(top)
    bottom = float(bottom)
    # Normalize so top >= bottom (POC/value-area point zones have top == bottom).
    if bottom > top:
        top, bottom = bottom, top
    return top, bottom


class RetestEngine:
    """
    Evaluates price reaction to a zone, not just the touch.

    Usage:
        engine = RetestEngine(cache)
        event = engine.evaluate(symbol, "order_block", zone, "BULLISH", candles)
        recent = engine.get_recent(symbol)
    """

    def __init__(self, cache: object | None = None) -> None:
        self._cache = cache
        # Per-symbol ring buffer of recent retest events.
        self._recent: dict[str, deque] = {}

    # ── Public API ──────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        zone_type: str,
        zone: dict,
        direction: str,
        candles: list[dict],
    ) -> Optional[RetestEvent]:
        """
        Evaluate the reaction of ``candles`` to ``zone``.

        Returns a :class:`RetestEvent` when price touched the zone within the
        last :data:`_TOUCH_WINDOW` candles, else ``None``. Never raises.
        """
        try:
            if not zone or not candles:
                return None

            direction = _normalize_direction(direction)
            if direction not in ("BULLISH", "BEARISH"):
                return None

            zone_top, zone_bottom = _zone_bounds(zone)
            if zone_top <= 0 or zone_bottom <= 0:
                return None

            touch_idx = self._find_touch(candles, zone_top, zone_bottom)
            if touch_idx is None:
                return None

            event = self._classify(
                symbol, zone_type, zone_top, zone_bottom,
                direction, candles, touch_idx,
            )
            if event is None:
                return None

            self._record(symbol, event)
            return event
        except Exception as exc:  # noqa: BLE001 — best-effort, never break pipeline
            logger.debug("[RETEST] evaluate failed for %s: %s", symbol, exc)
            return None

    def get_recent(self, symbol: str, limit: int = 10) -> list[RetestEvent]:
        """Return the most recent retest events for ``symbol`` (ring buffer)."""
        buf = self._recent.get(symbol)
        if not buf:
            return []
        events = list(buf)
        if limit and limit > 0:
            events = events[-limit:]
        return events

    # ── Private ─────────────────────────────────────────────────────────

    def _find_touch(
        self, candles: list[dict], zone_top: float, zone_bottom: float,
    ) -> Optional[int]:
        """Index of the most recent candle (within the touch window) that
        overlapped the zone, or ``None``."""
        n = len(candles)
        start = max(0, n - _TOUCH_WINDOW)
        for i in range(n - 1, start - 1, -1):
            c = candles[i]
            try:
                low = float(c["low"])
                high = float(c["high"])
            except (KeyError, TypeError, ValueError):
                continue
            # Overlap: candle range intersects [zone_bottom, zone_top].
            if low <= zone_top and high >= zone_bottom:
                return i
        return None

    def _classify(
        self,
        symbol: str,
        zone_type: str,
        zone_top: float,
        zone_bottom: float,
        direction: str,
        candles: list[dict],
        touch_idx: int,
    ) -> Optional[RetestEvent]:
        touch = candles[touch_idx]
        last = candles[-1]
        try:
            touch_low = float(touch["low"])
            touch_high = float(touch["high"])
            last_close = float(last["close"])
            last_low = float(last["low"])
            last_high = float(last["high"])
        except (KeyError, TypeError, ValueError):
            return None

        zone_height = zone_top - zone_bottom
        mid = (zone_top + zone_bottom) / 2.0
        # Tolerance: 0.5x zone height, or a fixed fraction of price for point zones.
        tol = 0.5 * zone_height if zone_height > 0 else abs(mid) * 0.0005

        # Approach: where price came from before the touch.
        approach = self._approach(candles, touch_idx, mid, direction)

        # Touch metadata.
        touch_ts = touch.get("open_time") or touch.get("close_time")
        if direction == "BULLISH":
            touch_price = touch_low
            penetration_depth = max(0.0, zone_bottom - touch_low)
        else:
            touch_price = touch_high
            penetration_depth = max(0.0, touch_high - zone_top)

        candles_since_touch = (len(candles) - 1) - touch_idx

        # ── Reaction classification ──────────────────────────────────────
        # The "hold" side is where price recovers after testing the zone:
        #   BULLISH support  → closes back ABOVE the top.
        #   BEARISH resistance → closes back BELOW the bottom.
        # The "break" side is the opposite (support breaks down / resistance breaks up).
        if direction == "BULLISH":
            hold_side = last_close > zone_top
            break_side = last_close < zone_bottom
            penetration = max(0.0, zone_bottom - last_close)
        else:
            hold_side = last_close < zone_bottom
            break_side = last_close > zone_top
            penetration = max(0.0, last_close - zone_top)

        if not hold_side and not break_side:
            # Still inside the zone — unresolved unless it just touched.
            reaction = "PENDING"
            strength = 0.0
            invalidation = ""
        elif hold_side:
            # Closed back beyond the zone on the "hold" side → rejected.
            reaction = "REJECTED"
            strength = self._rejection_strength(
                direction, zone_top, zone_bottom, last_close, last_low, last_high
            )
            invalidation = ""
        else:
            # Closed beyond the zone on the breakout side.
            if penetration > tol:
                reaction = "FAILED"
                invalidation = "penetrated beyond tolerance — zone broken"
            elif candles_since_touch >= 1:
                # Broke through and the next candle confirmed continuation.
                reaction = "ACCEPTED"
                invalidation = ""
            else:
                # Broke through on the touch candle itself, no follow-through yet.
                reaction = "PENDING"
                invalidation = ""
            strength = self._breakout_strength(penetration, tol)

        return RetestEvent(
            zone_type=zone_type,
            direction=direction,
            zone_top=zone_top,
            zone_bottom=zone_bottom,
            approach=approach,
            touch_ts=touch_ts,
            touch_price=touch_price,
            penetration_depth=round(penetration_depth, 6),
            reaction=reaction,
            reaction_strength=round(strength, 4),
            candles_since_touch=candles_since_touch,
            invalidation_reason=invalidation,
        )

    def _approach(
        self, candles: list[dict], touch_idx: int, mid: float, direction: str
    ) -> str:
        if touch_idx > 0:
            prev = candles[touch_idx - 1]
            try:
                prev_close = float(prev["close"])
                return "from_above" if prev_close >= mid else "from_below"
            except (KeyError, TypeError, ValueError):
                pass
        # No prior candle: infer from the zone's natural side.
        return "from_above" if direction == "BULLISH" else "from_below"

    @staticmethod
    def _rejection_strength(
        direction: str,
        zone_top: float,
        zone_bottom: float,
        last_close: float,
        last_low: float,
        last_high: float,
    ) -> float:
        """Strength of a rejection from the rejection candle's wick/body ratio."""
        eps = 1e-12
        if direction == "BULLISH":
            # Recovered above the top: recovery vs how far the low pierced below.
            recovery = max(0.0, last_close - zone_top)
            pierce = max(0.0, zone_bottom - last_low)
        else:
            # Recovered below the bottom: recovery vs how far the high pierced above.
            recovery = max(0.0, zone_bottom - last_close)
            pierce = max(0.0, last_high - zone_top)
        if recovery <= 0 and pierce <= 0:
            return 0.0
        # Strong rejection = large recovery relative to pierce.
        return min(1.0, recovery / (recovery + pierce + eps))

    @staticmethod
    def _breakout_strength(penetration: float, tol: float) -> float:
        """Strength of a breakout/acceptance relative to tolerance."""
        if tol <= 0:
            return min(1.0, penetration / (abs(penetration) + 1e-12))
        return min(1.0, penetration / (tol * 2.0))

    def _record(self, symbol: str, event: RetestEvent) -> None:
        buf = self._recent.get(symbol)
        if buf is None:
            buf = deque(maxlen=50)
            self._recent[symbol] = buf
        buf.append(event)
