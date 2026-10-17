"""
Fractal Inversion detector (ICT — a higher-timeframe level that flips its role).

Answers one question only: *did price break a confirmed higher-timeframe fractal,
then come back and hold that level from the other side?*

ICT's claim: a higher-timeframe fractal is a swing point that the higher timeframe
itself respects.  When price closes through one with displacement, the level stops
being resistance and becomes support (or the reverse).  The trade is the RETEST —
you are not chasing the break, you are waiting for price to come back to the level
and prove it holds.

    close ABOVE a confirmed HTF fractal high, then retest it and hold -> CONFIRMED_LONG
    close BELOW a confirmed HTF fractal low,  then retest it and hold -> CONFIRMED_SHORT

A close back THROUGH the level during the retest window ends the idea
(`INVALIDATED`); if the window closes with no retest the level has been consumed
and the detector drops back to `WAITING` for the next fractal.

The higher timeframe is built inside this module from the candles it is given
(`htf_minutes`, default 240 = H4 out of M15 input), so it needs nothing injected
and nothing else from the repo.  Fractals are confirmed by `fractal_lookback` bars
on EACH side, which means the newest HTF bars can never form one — deliberate, so
an in-progress bar cannot invent a level.

Standalone by design (project rule §1): imports stdlib only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.fractal_inversion")

CONCEPT_NAME = "fractal_inversion"
CONCEPT_SOURCES = ("ict", "tradingfinder")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class FractalInversionConfig:
    htf_minutes: int = 240            # the higher timeframe built from the input candles
    fractal_lookback: int = 2         # bars on EACH side needed to confirm a fractal
    break_buffer_pips: float = 2.0    # a close this far through the level is a break
    retest_tolerance_pips: float = 5.0   # how close price must come back to the level
    stop_buffer_pips: float = 2.0     # stop sits this far beyond the retest extreme
    target_mode: str = "measured_move"   # "measured_move" | "next_fractal"
    retest_window_bars: int = 100     # bars after the break in which the retest must arrive
    pip_size: float = 0.0001


DEFAULT_FRACTAL_INVERSION_CONFIG = FractalInversionConfig()


# ── Result types ────────────────────────────────────────────────────────────

class FractalInversionState(Enum):
    NO_FRACTAL = "NO_FRACTAL"         # not enough HTF history to confirm a level
    WAITING = "WAITING"               # a level exists, nothing has broken yet
    BROKEN_UP = "BROKEN_UP"           # closed above a fractal high, no retest yet
    BROKEN_DOWN = "BROKEN_DOWN"       # closed below a fractal low, no retest yet
    CONFIRMED_LONG = "CONFIRMED_LONG"   # the flipped level held — this is the signal
    CONFIRMED_SHORT = "CONFIRMED_SHORT"
    INVALIDATED = "INVALIDATED"       # closed back through the level
    UNKNOWN = "UNKNOWN"


class FractalInversionEvent(Enum):
    BROKEN_UP = "FI_BROKEN_UP"
    BROKEN_DOWN = "FI_BROKEN_DOWN"
    CONFIRMED_LONG = "FI_CONFIRMED_LONG"
    CONFIRMED_SHORT = "FI_CONFIRMED_SHORT"
    INVALIDATED = "FI_INVALIDATED"


@dataclass
class FractalInversionRecord:
    symbol: str = ""
    timeframe: int = 0
    state: FractalInversionState = FractalInversionState.UNKNOWN
    htf_minutes: int = 0
    level_side: str = ""              # "HIGH" | "LOW" — which fractal flipped
    level_price: float = 0.0
    level_time: object = None
    break_price: float = 0.0
    break_time: object = None
    direction: str = ""               # "LONG" | "SHORT"
    retest_price: float = 0.0         # the close of the bar that retested
    retest_extreme: float = 0.0       # that bar's low (longs) / high (shorts)
    retest_time: object = None
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    rr: float = 0.0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state.value,
            "htf_minutes": self.htf_minutes,
            "level_side": self.level_side,
            "level_price": round(self.level_price, 6),
            "level_time": self.level_time,
            "break_price": round(self.break_price, 6),
            "break_time": self.break_time,
            "direction": self.direction,
            "retest_price": round(self.retest_price, 6),
            "retest_extreme": round(self.retest_extreme, 6),
            "retest_time": self.retest_time,
            "entry_price": round(self.entry_price, 6),
            "stop_price": round(self.stop_price, 6),
            "target_price": round(self.target_price, 6),
            "risk_pips": round(self.risk_pips, 2),
            "reward_pips": round(self.reward_pips, 2),
            "rr": round(self.rr, 2),
        }


# ── Pure helpers (no state, no I/O) ─────────────────────────────────────────

def _val(candle: dict, *keys, default=None):
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


def _open(candle: dict) -> float:
    return float(_val(candle, "open", "o", default=0.0))


def _high(candle: dict) -> float:
    return float(_val(candle, "high", "h", default=0.0))


def _low(candle: dict) -> float:
    return float(_val(candle, "low", "l", default=0.0))


def _close(candle: dict) -> float:
    return float(_val(candle, "close", "c", default=0.0))


def _htf_bars(candles: list[dict], htf_seconds: int) -> list[dict]:
    """Fold the input candles into higher-timeframe bars, oldest first."""
    buckets: dict[int, list[dict]] = {}
    for candle in candles:
        ts = _time_key(candle)
        if ts is None:
            continue
        buckets.setdefault(int(ts // htf_seconds), []).append(candle)
    bars = []
    for bucket in sorted(buckets):
        group = buckets[bucket]
        bars.append({
            "open_time": bucket * htf_seconds,
            "end_time": (bucket + 1) * htf_seconds,
            "open": _open(group[0]),
            "high": max(_high(c) for c in group),
            "low": min(_low(c) for c in group),
            "close": _close(group[-1]),
        })
    return bars


def _fractal_indices(bars: list[dict], lookback: int) -> tuple[list[int], list[int]]:
    """Confirmed fractal highs and lows.  Strict comparisons, and the bar itself is
    excluded from its own window — otherwise every bar would qualify."""
    highs: list[int] = []
    lows: list[int] = []
    for i in range(lookback, len(bars) - lookback):
        others = bars[i - lookback:i] + bars[i + 1:i + lookback + 1]
        if _high(bars[i]) > max(_high(b) for b in others):
            highs.append(i)
        if _low(bars[i]) < min(_low(b) for b in others):
            lows.append(i)
    return highs, lows


def _first_close_through(candles: list[dict], level: float, start_ts: float,
                         above: bool, threshold: float) -> Optional[tuple]:
    """First bar at/after `start_ts` that CLOSES through `level` by `threshold`."""
    for i, candle in enumerate(candles):
        ts = _time_key(candle)
        if ts is None or ts < start_ts:
            continue
        close = _close(candle)
        if above and close > level + threshold:
            return i, close, ts
        if not above and close < level - threshold:
            return i, close, ts
    return None


# ── Engine ──────────────────────────────────────────────────────────────────

class FractalInversionEngine:
    """Per-symbol HTF-level state.  Pure: no clock, no I/O."""

    def __init__(self,
                 config: FractalInversionConfig = DEFAULT_FRACTAL_INVERSION_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], FractalInversionRecord] = {}
        self._events: list[dict] = []

    # -- main entry point ---------------------------------------------------

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        record = FractalInversionRecord(
            symbol=symbol,
            timeframe=tf,
            htf_minutes=self.config.htf_minutes,
            created_at=last_ts,
        )
        record = self._build(candles, record)
        prev = self._state.get((symbol, tf))
        events = self._diff(record, prev)
        self._state[(symbol, tf)] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    # -- computation --------------------------------------------------------

    def _build(self, candles: list[dict],
               record: FractalInversionRecord) -> FractalInversionRecord:
        cfg = self.config
        htf_seconds = cfg.htf_minutes * 60
        bars = _htf_bars(candles, htf_seconds)
        highs, lows = _fractal_indices(bars, cfg.fractal_lookback)
        if not highs and not lows:
            record.state = FractalInversionState.NO_FRACTAL
            return record

        threshold = cfg.break_buffer_pips * cfg.pip_size
        candidates = []
        if highs:
            bar = bars[highs[-1]]
            found = _first_close_through(candles, bar["high"], bar["end_time"],
                                         True, threshold)
            if found:
                candidates.append((found[0], "HIGH", bar, found))
        if lows:
            bar = bars[lows[-1]]
            found = _first_close_through(candles, bar["low"], bar["end_time"],
                                         False, threshold)
            if found:
                candidates.append((found[0], "LOW", bar, found))

        if not candidates:
            record.state = FractalInversionState.WAITING
            return record

        # whichever level broke FIRST is the one in play
        index, side, bar, (break_i, break_price, break_ts) = min(candidates,
                                                                 key=lambda c: c[0])
        record.level_side = side
        record.level_price = bar["high"] if side == "HIGH" else bar["low"]
        record.level_time = bar["open_time"]
        record.break_price = break_price
        record.break_time = break_ts

        up = side == "HIGH"
        record.direction = "LONG" if up else "SHORT"
        record.state = (FractalInversionState.BROKEN_UP if up
                        else FractalInversionState.BROKEN_DOWN)

        tol = cfg.retest_tolerance_pips * cfg.pip_size
        for offset, candle in enumerate(candles[break_i + 1:]):
            if offset >= cfg.retest_window_bars:
                # the level has been consumed without a retest; wait for the next one
                record.state = FractalInversionState.WAITING
                return record
            close = _close(candle)
            if (up and close < record.level_price - tol) or \
               (not up and close > record.level_price + tol):
                record.state = FractalInversionState.INVALIDATED
                record.retest_price = close
                record.retest_time = _time_key(candle)
                return record
            touched = (_low(candle) <= record.level_price + tol if up
                       else _high(candle) >= record.level_price - tol)
            if touched:
                record.state = (FractalInversionState.CONFIRMED_LONG if up
                                else FractalInversionState.CONFIRMED_SHORT)
                record.retest_price = close
                record.retest_extreme = _low(candle) if up else _high(candle)
                record.retest_time = _time_key(candle)
                self._trade_levels(record, bars, highs, lows)
                return record
        return record

    def _trade_levels(self, record: FractalInversionRecord, bars: list[dict],
                      highs: list[int], lows: list[int]) -> None:
        """Enter on the retest close; stop beyond the retest extreme; target per mode."""
        cfg = self.config
        long_side = record.direction == "LONG"
        buffer = cfg.stop_buffer_pips * cfg.pip_size

        record.entry_price = record.retest_price
        record.stop_price = (record.retest_extreme - buffer if long_side
                             else record.retest_extreme + buffer)

        if cfg.target_mode == "next_fractal":
            if long_side:
                beyond = [bars[i]["high"] for i in highs
                          if bars[i]["high"] > record.level_price]
                record.target_price = min(beyond) if beyond else 0.0
            else:
                beyond = [bars[i]["low"] for i in lows
                          if bars[i]["low"] < record.level_price]
                record.target_price = max(beyond) if beyond else 0.0
        else:
            # measured move: the level projects its own swing height past itself
            if long_side and lows:
                anchor = bars[lows[-1]]["low"]
                record.target_price = record.level_price + (
                    record.level_price - anchor)
            elif not long_side and highs:
                anchor = bars[highs[-1]]["high"]
                record.target_price = record.level_price - (
                    anchor - record.level_price)
            else:
                record.target_price = 0.0

        if record.stop_price and record.target_price:
            record.risk_pips = abs(record.entry_price - record.stop_price) / cfg.pip_size
            record.reward_pips = abs(record.target_price - record.entry_price) / cfg.pip_size
            record.rr = (record.reward_pips / record.risk_pips
                         if record.risk_pips > 0 else 0.0)

    # -- events -------------------------------------------------------------

    def _diff(self, record: FractalInversionRecord,
              prev: Optional[FractalInversionRecord]) -> list[dict]:
        if prev is not None and prev.state is record.state and \
                prev.level_price == record.level_price:
            return []
        table = {
            FractalInversionState.BROKEN_UP: FractalInversionEvent.BROKEN_UP,
            FractalInversionState.BROKEN_DOWN: FractalInversionEvent.BROKEN_DOWN,
            FractalInversionState.CONFIRMED_LONG: FractalInversionEvent.CONFIRMED_LONG,
            FractalInversionState.CONFIRMED_SHORT: FractalInversionEvent.CONFIRMED_SHORT,
            FractalInversionState.INVALIDATED: FractalInversionEvent.INVALIDATED,
        }
        event = table.get(record.state)
        if event is None:
            return []
        return [self._event(event, record)]

    def _event(self, event: FractalInversionEvent,
               record: FractalInversionRecord, **extra) -> dict:
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
