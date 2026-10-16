"""
Data requirements manager.

Declares what historical candle depth is needed per analysis level.
The startup preload uses this to know how many candles to load from
ClickHouse into the hot cache.

Phase 2.5 — improvements.md §21.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeframeRequirement:
    """How many closed candles are needed for one timeframe."""
    timeframe_seconds: int
    min_candles: int  # minimum needed for analysis to function
    desired_candles: int  # ideal for full structural analysis


# ── Default requirements (configurable per deployment) ──────────────────
# Only 1-minute candles are stored.  Higher timeframes are aggregated
# on-the-fly, so we only declare requirements for the 1m base.
REQUIREMENTS: list[TimeframeRequirement] = [
    TimeframeRequirement(timeframe_seconds=60, min_candles=200, desired_candles=1000),
]

# Lookup tables — unknown timeframes are conservatively treated as 200.
_MIN_CANDLES: dict[int, int] = {r.timeframe_seconds: r.min_candles for r in REQUIREMENTS}
_DESIRED_CANDLES: dict[int, int] = {r.timeframe_seconds: r.desired_candles for r in REQUIREMENTS}


def get_requirements() -> list[TimeframeRequirement]:
    """Return the current data requirements."""
    return REQUIREMENTS


def min_candles_for(tf_seconds: int) -> int:
    """Return the minimum number of candles needed for ``tf_seconds``."""
    return _MIN_CANDLES.get(tf_seconds, 200)


def desired_candles_for(tf_seconds: int) -> int:
    """Return the desired (ideal) number of candles for ``tf_seconds``."""
    return _DESIRED_CANDLES.get(tf_seconds, 200)