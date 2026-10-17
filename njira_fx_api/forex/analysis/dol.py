"""
Draw on Liquidity (DOL) detector (ICT flagship brick — the TARGET side).

Answers one question only: *where is price being pulled towards next?*

ICT's claim: price is not wandering — it is drawn to the nearest pool of resting
orders that has not been consumed yet.  Stops cluster above old highs and below
old lows, so those untouched levels act as magnets.

Pools considered (all computed here from the candles, none imported):

    PDH / PDL   previous day high / low
    PWH / PWL   previous week high / low
    TODAY_HIGH / TODAY_LOW   the running session extremes
    EQUAL_HIGH / EQUAL_LOW   clusters of swing pivots within tolerance

A pool counts as SWEPT only once price has traded THROUGH it *after the pool was
formed* — last week's high must not mark today's high as already taken.  The draw
is the NEAREST un-swept pool; buy-side (above) wins -> direction UP, sell-side
(below) wins -> direction DOWN.  When nothing is left, NEUTRAL.

Known limits, deliberately left visible rather than hidden:
    C5  the sweep anchor is the window extreme, not the sweep candle's close
    C6  ties between two equally distant pools break toward the buy side

Standalone by design (project rule §1): imports only stdlib.  The day/week maths
is duplicated from daily_bias on purpose — importing a sibling would couple two
detectors that must stay independently tunable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.dol")

CONCEPT_NAME = "dol"
CONCEPT_SOURCES = ("ict", "litefinance")

_SECONDS_PER_DAY = 86400


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class DOLConfig:
    pool_lookback: int = 500           # bars searched for swing / equal levels
    swing_lookback: int = 5            # bars each side for a swing pivot
    equal_tolerance_pips: float = 2.0  # two highs this close = one equal-high pool
    include_weekly: bool = True        # PWH / PWL on/off
    include_equal_levels: bool = True  # equal highs / lows on/off
    include_session_extremes: bool = False  # today's running extreme — off, it is tapped by definition
    max_targets: int = 5               # how many ranked targets to report
    pip_size: float = 0.0001
    min_distance_pips: float = 0.0     # ignore pools already almost reached


DEFAULT_DOL_CONFIG = DOLConfig()


# ── Result types ────────────────────────────────────────────────────────────

class DOLDirection(Enum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class PoolKind(Enum):
    PDH = "PDH"
    PDL = "PDL"
    PWH = "PWH"
    PWL = "PWL"
    TODAY_HIGH = "TODAY_HIGH"
    TODAY_LOW = "TODAY_LOW"
    EQUAL_HIGH = "EQUAL_HIGH"
    EQUAL_LOW = "EQUAL_LOW"


class DOLEvent(Enum):
    TARGET_SET = "DOL_TARGET_SET"
    TARGET_CHANGED = "DOL_TARGET_CHANGED"
    TARGET_SWEPT = "DOL_TARGET_SWEPT"


@dataclass
class DOLRecord:
    symbol: str = ""
    timeframe: int = 0
    direction: DOLDirection = DOLDirection.UNKNOWN
    target_kind: str = ""
    target_price: float = 0.0
    distance_pips: float = 0.0
    targets: list = None        # ranked [{kind, price, side, distance_pips, swept}]
    current_price: float = 0.0
    window_high: float = 0.0
    window_low: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "target_kind": self.target_kind,
            "target_price": round(self.target_price, 6),
            "distance_pips": round(self.distance_pips, 2),
            "targets": [
                {**t, "price": round(t["price"], 6),
                 "distance_pips": round(t["distance_pips"], 2)}
                for t in (self.targets or [])
            ],
            "current_price": round(self.current_price, 6),
            "window_high": round(self.window_high, 6),
            "window_low": round(self.window_low, 6),
        }


# ── Pure helpers (no state, no I/O) ─────────────────────────────────────────

def _val(candle: dict, *keys, default=None):
    """Tolerant candle access — same tolerance as simple_snr._val."""
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]
    return default


def _time_key(candle: dict):
    ts = _val(candle, "open_time", "time", "timestamp", "date")
    if ts is None:
        return None
    ts = float(ts)
    return ts / 1000.0 if ts > 1e11 else ts


def _day_start(ts: float) -> int:
    """Epoch seconds of the UTC midnight that opened the containing day."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _week_start(ts: float) -> int:
    """Epoch seconds of Monday 00:00 UTC of the week containing ``ts``."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    return int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _extremes(candles: list[dict]) -> tuple[float, float]:
    if not candles:
        return 0.0, 0.0
    highs = [float(_val(c, "high", "h", default=0.0)) for c in candles]
    lows = [float(_val(c, "low", "l", default=0.0)) for c in candles]
    return max(highs), min(lows)


def _pivots(candles: list[dict], lookback: int, high: bool) -> list[tuple[float, float]]:
    """Swing pivots as ``(level, timestamp)`` — a pivot needs a lower/higher bar each side."""
    body, alias = ("high", "h") if high else ("low", "l")
    out: list[tuple[float, float]] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        level = float(_val(candles[i], body, alias, default=0.0))
        neighbourhood = [float(_val(candles[j], body, alias, default=0.0))
                         for j in range(i - lookback, i + lookback + 1)]
        if (high and level >= max(neighbourhood)) or (not high and level <= min(neighbourhood)):
            out.append((level, _time_key(candles[i]) or 0.0))
    return out


def _cluster(pivots: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Collapse pivots within ``tolerance``; the cluster forms when its last member does."""
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: p[0])
    clusters: list[list[tuple[float, float]]] = [[ordered[0]]]
    for level, ts in ordered[1:]:
        if abs(level - clusters[-1][-1][0]) <= tolerance:
            clusters[-1].append((level, ts))
        else:
            clusters.append([(level, ts)])
    return [(sum(l for l, _ in c) / len(c), max(t for _, t in c)) for c in clusters]


def _swept(level: float, formed_ts: float, price: float, window: list[dict]) -> bool:
    """A pool counts as swept only if price traded THROUGH it after the pool formed.

    Without the "after" test, last week's high would mark today's high as already
    swept, which is how a magnet quietly disappears from the map.
    """
    after = [c for c in window if (_time_key(c) or 0) > formed_ts]
    if not after:
        return False
    hi, lo = _extremes(after)
    return hi > level if level > price else lo < level


# ── Engine ──────────────────────────────────────────────────────────────────

class DOLEngine:
    """Per-symbol draw-on-liquidity state.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: DOLConfig = DEFAULT_DOL_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], DOLRecord] = {}
        self._events: list[dict] = []

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        record = self._build(candles, tf, symbol, last_ts)
        if record is None:
            return []

        prev = self._state.get((symbol, tf))
        events = self._diff(record, prev)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    # -- computation --------------------------------------------------------

    def _build(self, candles: list[dict], tf: int, symbol: str,
               last_ts: float) -> Optional[DOLRecord]:
        cfg = self.config
        window = candles[-cfg.pool_lookback:] if cfg.pool_lookback else list(candles)
        window_high, window_low = _extremes(window)
        price = float(_val(candles[-1], "close", "c", default=window_high))
        if window_high <= window_low:
            return None

        today_start = _day_start(last_ts)
        week_start = _week_start(last_ts)
        today = [c for c in window if (_time_key(c) or 0) >= today_start]
        this_week = [c for c in window if (_time_key(c) or 0) >= week_start]

        yesterday = [c for c in window
                     if today_start - _SECONDS_PER_DAY <= (_time_key(c) or 0) < today_start]
        last_week = [c for c in window
                     if week_start - 7 * _SECONDS_PER_DAY <= (_time_key(c) or 0) < week_start]

        levels: list[tuple[str, float, float]] = []   # (kind, price, formed_ts)

        if yesterday:
            y_high, y_low = _extremes(yesterday)
            levels.append((PoolKind.PDH.value, y_high, today_start))
            levels.append((PoolKind.PDL.value, y_low, today_start))

        if cfg.include_weekly and last_week:
            w_high, w_low = _extremes(last_week)
            levels.append((PoolKind.PWH.value, w_high, week_start))
            levels.append((PoolKind.PWL.value, w_low, week_start))

        if cfg.include_session_extremes and len(today) > 1:
            t_high, t_low = _extremes(today[:-1])
            formed = _time_key(today[-2]) or 0.0
            levels.append((PoolKind.TODAY_HIGH.value, t_high, formed))
            levels.append((PoolKind.TODAY_LOW.value, t_low, formed))

        if cfg.include_equal_levels:
            tolerance = cfg.equal_tolerance_pips * cfg.pip_size
            for level, ts in _cluster(_pivots(window, cfg.swing_lookback, True), tolerance):
                levels.append((PoolKind.EQUAL_HIGH.value, level, ts))
            for level, ts in _cluster(_pivots(window, cfg.swing_lookback, False), tolerance):
                levels.append((PoolKind.EQUAL_LOW.value, level, ts))

        min_distance = cfg.min_distance_pips * cfg.pip_size
        ranked: list[dict] = []
        for kind, level, formed_ts in levels:
            if level <= 0:
                continue
            side = "BUY_SIDE" if level > price else "SELL_SIDE"
            distance = abs(level - price)
            if distance < min_distance or _swept(level, formed_ts, price, window):
                continue
            ranked.append({
                "kind": kind,
                "price": level,
                "side": side,
                "distance_pips": distance / cfg.pip_size,
                "swept": False,
            })

        ranked.sort(key=lambda t: (t["distance_pips"], t["side"] != "BUY_SIDE"))
        targets = ranked[:cfg.max_targets]

        if not targets:
            direction = DOLDirection.NEUTRAL
            kind, target_price, distance = "", price, 0.0
        else:
            best = targets[0]
            direction = (DOLDirection.UP if best["side"] == "BUY_SIDE"
                         else DOLDirection.DOWN)
            kind, target_price, distance = best["kind"], best["price"], best["distance_pips"]

        return DOLRecord(
            symbol=symbol,
            timeframe=tf,
            direction=direction,
            target_kind=kind,
            target_price=target_price,
            distance_pips=distance,
            targets=targets,
            current_price=price,
            window_high=window_high,
            window_low=window_low,
            created_at=last_ts,
        )

    # -- events -------------------------------------------------------------

    def _diff(self, record: DOLRecord, prev: Optional[DOLRecord]) -> list[dict]:
        if prev is None:
            return [self._event(DOLEvent.TARGET_SET, record)]
        if prev.direction != record.direction:
            return [self._event(DOLEvent.TARGET_CHANGED, record,
                                previous=prev.direction.value)]
        if prev.target_kind != record.target_kind:
            return [self._event(DOLEvent.TARGET_SWEPT, record,
                                previous_target=prev.target_kind,
                                previous_price=round(prev.target_price, 6))]
        return []

    def _event(self, event: DOLEvent, record: DOLRecord, **extra) -> dict:
        payload = {"event": event.value, "timestamp": record.created_at}
        payload.update(record.to_dict())
        payload.update(extra)
        return payload

    # -- read access --------------------------------------------------------

    def get_active(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_all(self, limit: int = 20) -> list[dict]:
        return [r.to_dict() for r in list(self._state.values())[-limit:]]

    def get_events(self, n: int = 5) -> list[dict]:
        return self._events[-n:]

    def reset(self) -> None:
        self._state.clear()
        self._events.clear()
