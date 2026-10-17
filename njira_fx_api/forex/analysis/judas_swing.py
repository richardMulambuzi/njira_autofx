"""
Judas Swing detector (ICT flagship precondition — the false move at the open).

Answers one question only: *did the session open with a FAKE move that has now
been taken back?*

ICT's claim: the first thrust out of a session (London 07:00 UTC, New York 12:00
UTC) is often engineered to run the stops of traders who jumped early.  Price
punches one way, grabs the liquidity sitting there, then reverses and does the
real move in the opposite direction.  That first punch is the "Judas swing".

    thrust DOWN past the session open, then close back ABOVE it  -> BULLISH_JUDAS
    thrust UP past the session open, then close back BELOW it    -> BEARISH_JUDAS

The state is named for the REAL move that the fake move implies, not for the
direction of the punch — that is the only labelling that a trader cannot misread.

    PENDING         a watched session is open and nothing has qualified yet
    NO_JUDAS        the window closed with no reclamation (the break was real)

Session membership comes from ``forex.analysis.sessions`` (the one permitted
import); nothing is timezone-guessed here.  When two watched sessions overlap the
one that opened MOST RECENTLY wins, which is what makes the New York open
supersede the London one instead of hiding behind it.

Standalone by design (project rule §1): imports stdlib plus ``sessions`` only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from forex.analysis import sessions

logger = logging.getLogger("forex.analysis.judas_swing")

CONCEPT_NAME = "judas_swing"
CONCEPT_SOURCES = ("ict", "tradingfinder")


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class JudasSwingConfig:
    sessions_watched: tuple = ("london", "newyork")  # session names from sessions.SESSIONS
    window_bars: int = 8               # bars after the open in which the fake move must happen
    min_thrust_pips: float = 8.0       # how far past the open counts as a thrust
    reclaim_mode: str = "close"        # "close" (body back through) | "wick" (touch back)
    pip_size: float = 0.0001


DEFAULT_JUDAS_SWING_CONFIG = JudasSwingConfig()


# ── Result types ────────────────────────────────────────────────────────────

class JudasState(Enum):
    PENDING = "PENDING"
    BULLISH_JUDAS = "BULLISH_JUDAS"
    BEARISH_JUDAS = "BEARISH_JUDAS"
    NO_JUDAS = "NO_JUDAS"
    UNKNOWN = "UNKNOWN"


class JudasSwingEvent(Enum):
    JUDAS_DETECTED = "JS_JUDAS_DETECTED"
    JUDAS_PENDING = "JS_JUDAS_PENDING"


@dataclass
class JudasSwingRecord:
    symbol: str = ""
    timeframe: int = 0
    session: str = ""
    state: JudasState = JudasState.UNKNOWN
    session_open_price: float = 0.0
    session_open_time: object = None
    extreme_side: str = ""             # "UP" | "DOWN" — the direction of the fake move
    extreme_price: float = 0.0
    extreme_time: object = None
    thrust_pips: float = 0.0
    reclaimed: bool = False
    reclaim_price: float = 0.0
    reclaim_time: object = None
    bars_since_open: int = 0
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "session": self.session,
            "state": self.state.value,
            "session_open_price": round(self.session_open_price, 6),
            "session_open_time": self.session_open_time,
            "extreme_side": self.extreme_side,
            "extreme_price": round(self.extreme_price, 6),
            "extreme_time": self.extreme_time,
            "thrust_pips": round(self.thrust_pips, 2),
            "reclaimed": self.reclaimed,
            "reclaim_price": round(self.reclaim_price, 6),
            "reclaim_time": self.reclaim_time,
            "bars_since_open": self.bars_since_open,
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


def _in_session(candle: dict, name: str) -> bool:
    ts = _time_key(candle)
    if ts is None:
        return False
    return name in (sessions.session_of(ts) or [])


def _session_open_index(candles: list[dict], name: str) -> Optional[int]:
    """Index of the first bar of the most recent contiguous run of ``name``."""
    start = None
    for i, candle in enumerate(candles):
        if not _in_session(candle, name):
            continue
        if i == 0 or not _in_session(candles[i - 1], name):
            start = i
    return start


def _current_session(candles: list[dict], watched: tuple) -> Optional[tuple]:
    """(name, open_index) for the watched session that opened most recently."""
    if not candles:
        return None
    last = candles[-1]
    best = None
    for name in watched:
        if not _in_session(last, name):
            continue
        index = _session_open_index(candles, name)
        if index is None:
            continue
        if best is None or index > best[1]:
            best = (name, index)
    return best


# ── Engine ──────────────────────────────────────────────────────────────────

class JudasSwingEngine:
    """Per-symbol session-open fake-move state.  Pure: no clock, no I/O."""

    def __init__(self, config: JudasSwingConfig = DEFAULT_JUDAS_SWING_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], JudasSwingRecord] = {}
        self._events: list[dict] = []

    # -- main entry point ---------------------------------------------------

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []

        found = _current_session(candles, self.config.sessions_watched)
        if found is None:
            return []                       # no watched session is open — nothing to say
        name, index = found

        record = self._build(candles, tf, symbol, name, index, last_ts)
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

    def _build(self, candles: list[dict], tf: int, symbol: str, name: str,
               index: int, last_ts: float) -> Optional[JudasSwingRecord]:
        cfg = self.config
        opener = candles[index]
        open_price = _open(opener)
        if open_price <= 0:
            return None

        window = candles[index:index + 1 + cfg.window_bars]
        if not window:
            return None

        up_i, down_i = index, index
        for offset, candle in enumerate(window):
            if _high(candle) > _high(window[up_i - index]):
                up_i = index + offset
            if _low(candle) < _low(window[down_i - index]):
                down_i = index + offset

        up_extreme, down_extreme = _high(candles[up_i]), _low(candles[down_i])
        up_thrust = (up_extreme - open_price) / cfg.pip_size
        down_thrust = (open_price - down_extreme) / cfg.pip_size

        # a DOWN thrust is reclaimed by price getting back ABOVE the open, and vice versa
        up_reclaim = self._reclaim_index(candles, up_i, open_price, above=False)
        down_reclaim = self._reclaim_index(candles, down_i, open_price, above=True)

        up_qualified = up_thrust >= cfg.min_thrust_pips and up_reclaim is not None
        down_qualified = down_thrust >= cfg.min_thrust_pips and down_reclaim is not None

        record = JudasSwingRecord(
            symbol=symbol,
            timeframe=tf,
            session=name,
            session_open_price=open_price,
            session_open_time=_time_key(opener),
            bars_since_open=len(candles) - 1 - index,
            created_at=last_ts,
        )

        if up_qualified or down_qualified:
            if down_qualified and (not up_qualified or down_thrust >= up_thrust):
                # fake move DOWN, real move UP
                record.state = JudasState.BULLISH_JUDAS
                record.extreme_side = "DOWN"
                record.extreme_price = down_extreme
                record.extreme_time = _time_key(candles[down_i])
                record.thrust_pips = down_thrust
                reclaim = down_reclaim
            else:
                record.state = JudasState.BEARISH_JUDAS
                record.extreme_side = "UP"
                record.extreme_price = up_extreme
                record.extreme_time = _time_key(candles[up_i])
                record.thrust_pips = up_thrust
                reclaim = up_reclaim
            record.reclaimed = True
            record.reclaim_price = _close(candles[reclaim])
            record.reclaim_time = _time_key(candles[reclaim])
            return record

        # still inside the window: the fake move may simply not have happened yet
        record.state = (JudasState.PENDING if len(window) <= cfg.window_bars
                        else JudasState.NO_JUDAS)
        return record

    def _reclaim_index(self, candles: list[dict], extreme_i: int,
                       open_price: float, above: bool) -> Optional[int]:
        """First bar after the extreme that gets price back through the session open.

        ``above=True`` means the extreme was BELOW the open and price must return
        above it; ``above=False`` means the extreme was ABOVE and price must return
        below it.
        """
        for i in range(extreme_i + 1, len(candles)):
            candle = candles[i]
            if self.config.reclaim_mode == "wick":
                back = _high(candle) > open_price if above else _low(candle) < open_price
            else:
                back = _close(candle) > open_price if above else _close(candle) < open_price
            if back:
                return i
        return None

    # -- events -------------------------------------------------------------

    def _diff(self, record: JudasSwingRecord,
              prev: Optional[JudasSwingRecord]) -> list[dict]:
        same_session = prev is not None and prev.session == record.session
        if same_session and prev.state == record.state:
            return []
        if record.state in (JudasState.BULLISH_JUDAS, JudasState.BEARISH_JUDAS):
            return [self._event(JudasSwingEvent.JUDAS_DETECTED, record)]
        if record.state is JudasState.PENDING:
            return [self._event(JudasSwingEvent.JUDAS_PENDING, record)]
        return []

    def _event(self, event: JudasSwingEvent, record: JudasSwingRecord, **extra) -> dict:
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
