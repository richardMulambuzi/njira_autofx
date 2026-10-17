"""
Daily bias detector (ICT flagship brick — direction permission gate).

Answers one question only: *which direction am I allowed to trade today?*

It is a PERMISSION GATE, not a signal.  It never says "buy" or "sell" — it only
tells the models above it which side is FORBIDDEN for the rest of the day.

Five competing rules are run on every day, and they are never silently merged
into one number:

    R1 prev_day_candle  yesterday closed above its open  -> BULLISH
    R2 pdh_pdl          price above previous-day high    -> BULLISH
                        price below previous-day low     -> BEARISH
    R3 daily_open       price above the anchored midnight open
    R4 weekly_open      price above this week's open
    R5 liquidity_draw   the NEAREST un-swept pool wins (PDH above / PDL below)

The verdict is the majority of the votes.  ``agreement`` reports whether they
actually agreed (ALIGNED) or fought (MIXED); ``tradeable`` applies the
``require_alignment`` gate.  A MIXED day is the tool working correctly — it
means stand aside, not that the rules need loosening.

TIMEZONE CONFLICT C4 (never silently resolved): ICT anchors the "daily open" at
New York midnight (00:00 EST), this codebase's ``sessions.py`` speaks UTC.  Both
anchors are computed and BOTH are stored in ``reference_prices``.  The knob
``anchor`` only decides which one drives R3.

KEY DESIGN RULE: the bias is computed ONCE for the day and LOCKED.  It is not
recomputed per candle — otherwise it flip-flops all session and the models above
simply follow price, which makes the gate worthless.

Standalone by design (project rule §1): imports only stdlib.  It also does not
need ``sessions`` because day/week anchoring is calendar arithmetic, not session
membership.  Nothing here can change another detector's behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.daily_bias")

CONCEPT_NAME = "daily_bias"
CONCEPT_SOURCES = ("tradingfinder", "litefinance", "ict")

_SECONDS_PER_DAY = 86400
_SECONDS_PER_WEEK = 7 * _SECONDS_PER_DAY


# ── Config: the fine-tuning surface ─────────────────────────────────────────

@dataclass(frozen=True)
class DailyBiasConfig:
    anchor: str = "utc"                # "utc" | "ny" — which midnight drives R3
    daily_open_hour_utc: int = 0       # UTC hour the new day starts
    daily_open_hour_ny: int = 0        # NY hour the new day starts
    ny_utc_offset_hours: int = -5      # EST; DST is a known simplification (C4)
    weekly_open_enabled: bool = True   # R4 on/off
    require_alignment: bool = True     # MIXED days are not tradeable
    min_break_pips: float = 2.0        # buffer before a break "counts"
    pip_size: float = 0.0001
    require_prev_day: bool = True      # refuse to guess without a completed day


DEFAULT_DAILY_BIAS_CONFIG = DailyBiasConfig()


# ── Result types ────────────────────────────────────────────────────────────

class DailyBias(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class BiasAgreement(Enum):
    ALIGNED = "ALIGNED"
    MIXED = "MIXED"
    NONE = "NONE"


class DailyBiasEvent(Enum):
    BIAS_LOCKED = "DB_BIAS_LOCKED"
    BIAS_CHANGED = "DB_BIAS_CHANGED"


@dataclass
class DailyBiasRecord:
    symbol: str = ""
    timeframe: int = 0
    day: int = 0                        # epoch seconds of the anchored day start
    bias: DailyBias = DailyBias.UNKNOWN
    agreement: BiasAgreement = BiasAgreement.NONE
    tradeable: bool = False
    allowed_direction: str = ""         # "LONG" | "SHORT" | ""
    votes: dict = None                  # rule -> "BULLISH" | "BEARISH" | "NEUTRAL"
    prices: dict = None                 # rule -> price the vote used
    reference_prices: dict = None       # PDH/PDL/prev OHLC/daily opens/weekly open
    locked_at: object = None
    created_at: object = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "day": self.day,
            "bias": self.bias.value,
            "agreement": self.agreement.value,
            "tradeable": self.tradeable,
            "allowed_direction": self.allowed_direction,
            "votes": dict(self.votes or {}),
            "prices": {k: (round(v, 6) if isinstance(v, (int, float)) else v)
                       for k, v in (self.prices or {}).items()},
            "reference_prices": {
                k: (round(v, 6) if isinstance(v, (int, float)) else v)
                for k, v in (self.reference_prices or {}).items()},
            "locked_at": self.locked_at,
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
    # Some feeds hand milliseconds; bucket in seconds either way.
    return ts / 1000.0 if ts > 1e11 else ts


def _day_start(ts: float, hour: int, offset_hours: int) -> int:
    """Epoch seconds of the most recent ``hour``:00 in the ``offset_hours`` zone."""
    local = datetime.fromtimestamp(ts, tz=timezone.utc) - timedelta(hours=offset_hours)
    start = local.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return int((start + timedelta(hours=offset_hours)).timestamp())


def _week_start(ts: float) -> int:
    """Epoch seconds of Monday 00:00 UTC of the week containing ``ts``."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    return int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _bucket_days(candles: list[dict], hour: int, offset_hours: int) -> dict:
    """Group candles by anchored day -> {open, high, low, close, first_ts, last_ts}."""
    days: dict[int, dict] = {}
    for candle in candles:
        ts = _time_key(candle)
        if ts is None:
            continue
        key = _day_start(ts, hour, offset_hours)
        hi = float(_val(candle, "high", "h", default=0.0))
        lo = float(_val(candle, "low", "l", default=0.0))
        op = float(_val(candle, "open", "o", default=0.0))
        cl = float(_val(candle, "close", "c", default=0.0))
        bucket = days.get(key)
        if bucket is None:
            days[key] = {"open": op, "high": hi, "low": lo, "close": cl,
                         "first_ts": ts, "last_ts": ts}
            continue
        bucket["high"] = max(bucket["high"], hi)
        bucket["low"] = min(bucket["low"], lo)
        bucket["close"] = cl          # candles arrive in order
        bucket["last_ts"] = ts
    return days


def _direction(delta: float, threshold: float) -> str:
    if delta > threshold:
        return "BULLISH"
    if delta < -threshold:
        return "BEARISH"
    return "NEUTRAL"


# ── Engine ──────────────────────────────────────────────────────────────────

class DailyBiasEngine:
    """Per-symbol daily bias.  Pure: no clock, no I/O, no network."""

    def __init__(self, config: DailyBiasConfig = DEFAULT_DAILY_BIAS_CONFIG) -> None:
        self.config = config
        self._state: dict[tuple[str, int], DailyBiasRecord] = {}
        self._events: list[dict] = []

    # -- anchoring ----------------------------------------------------------

    def _anchor(self) -> tuple[int, int]:
        """(hour, utc_offset_hours) for the anchor the config selected."""
        if self.config.anchor == "ny":
            return (self.config.daily_open_hour_ny,
                    self.config.ny_utc_offset_hours)
        return (self.config.daily_open_hour_utc, 0)

    # -- main entry point ---------------------------------------------------

    def analyze(self, candles: list[dict], tf: int = 0, symbol: str = "") -> list[dict]:
        if len(candles) < 2:
            return []
        tf = tf or int(_val(candles[-1], "timeframe", default=0) or 0)

        hour, offset = self._anchor()
        last_ts = _time_key(candles[-1])
        if last_ts is None:
            return []
        day = _day_start(last_ts, hour, offset)

        key = (symbol, tf)
        prev = self._state.get(key)
        if prev is not None and prev.day == day:
            return []                       # LOCKED — the day's bias does not move

        record = self._build(candles, tf, symbol, day, hour, offset)
        if record is None:
            return []

        record.locked_at = last_ts
        record.created_at = last_ts

        events = self._diff(record, prev)
        self._state[key] = record
        self._events.extend(events)
        if len(self._events) > 50:
            self._events = self._events[-50:]
        return events

    # -- computation --------------------------------------------------------

    def _build(self, candles: list[dict], tf: int, symbol: str, day: int,
               hour: int, offset: int) -> Optional[DailyBiasRecord]:
        cfg = self.config
        utc_days = _bucket_days(candles, cfg.daily_open_hour_utc, 0)
        days = _bucket_days(candles, hour, offset)
        today = days.get(day)
        if today is None:
            return None

        past = sorted(d for d in days if d < day)
        if not past:
            if cfg.require_prev_day:
                return None                  # no completed day to lean on yet
            return None
        yesterday = days[past[-1]]

        # The UTC daily open is always computed too — conflict C4 stays visible.
        utc_today = utc_days.get(_day_start(_time_key(candles[-1]),
                                            cfg.daily_open_hour_utc, 0))

        week = _week_start(_time_key(candles[-1]))
        wick = [c for c in candles
                if (_time_key(c) is not None and _week_start(_time_key(c)) == week)]
        weekly_open = float(_val(wick[0], "open", "o", default=0.0)) if wick else None

        price = float(_val(candles[-1], "close", "c", default=0.0))
        pdh = yesterday["high"]
        pdl = yesterday["low"]
        buffer = cfg.min_break_pips * cfg.pip_size

        votes: dict[str, str] = {}
        prices: dict[str, object] = {}

        # R1 — previous day's own candle
        votes["r1_prev_day_candle"] = _direction(
            yesterday["close"] - yesterday["open"], 0.0)
        prices["r1_prev_day_candle"] = yesterday["open"]

        # R2 — previous day high / low breakout
        if price > pdh + buffer:
            votes["r2_pdh_pdl"] = "BULLISH"
        elif price < pdl - buffer:
            votes["r2_pdh_pdl"] = "BEARISH"
        else:
            votes["r2_pdh_pdl"] = "NEUTRAL"
        prices["r2_pdh_pdl"] = pdh if price > pdh else (pdl if price < pdl else price)

        # R3 — anchored midnight open
        daily_open = today["open"]
        votes["r3_daily_open"] = _direction(price - daily_open, buffer)
        prices["r3_daily_open"] = daily_open

        # R4 — weekly open
        if cfg.weekly_open_enabled and weekly_open is not None:
            votes["r4_weekly_open"] = _direction(price - weekly_open, buffer)
            prices["r4_weekly_open"] = weekly_open
        else:
            votes["r4_weekly_open"] = "NEUTRAL"
            prices["r4_weekly_open"] = weekly_open

        # R5 — nearest un-swept pool
        pdh_swept = today["high"] >= pdh
        pdl_swept = today["low"] <= pdl
        if pdh_swept and pdl_swept:
            votes["r5_liquidity_draw"] = "NEUTRAL"
            prices["r5_liquidity_draw"] = price
        elif pdh_swept:
            votes["r5_liquidity_draw"] = "BEARISH"       # only PDL left to draw
            prices["r5_liquidity_draw"] = pdl
        elif pdl_swept:
            votes["r5_liquidity_draw"] = "BULLISH"       # only PDH left to draw
            prices["r5_liquidity_draw"] = pdh
        elif abs(pdh - price) <= abs(price - pdl):
            votes["r5_liquidity_draw"] = "BULLISH"
            prices["r5_liquidity_draw"] = pdh
        else:
            votes["r5_liquidity_draw"] = "BEARISH"
            prices["r5_liquidity_draw"] = pdl

        bullish = sum(1 for v in votes.values() if v == "BULLISH")
        bearish = sum(1 for v in votes.values() if v == "BEARISH")
        decided = bullish + bearish

        if bullish > bearish:
            bias = DailyBias.BULLISH
        elif bearish > bullish:
            bias = DailyBias.BEARISH
        else:
            bias = DailyBias.NEUTRAL

        if decided == 0:
            agreement = BiasAgreement.NONE
        elif bullish == 0 or bearish == 0:
            agreement = BiasAgreement.ALIGNED
        else:
            agreement = BiasAgreement.MIXED

        tradeable = (bias is not DailyBias.NEUTRAL
                     and (agreement is BiasAgreement.ALIGNED
                          or not cfg.require_alignment))
        allowed = ("LONG" if bias is DailyBias.BULLISH
                   else "SHORT" if bias is DailyBias.BEARISH else "")

        return DailyBiasRecord(
            symbol=symbol,
            timeframe=tf,
            day=day,
            bias=bias,
            agreement=agreement,
            tradeable=tradeable,
            allowed_direction=allowed,
            votes=votes,
            prices=prices,
            reference_prices={
                "pdh": pdh,
                "pdl": pdl,
                "prev_open": yesterday["open"],
                "prev_close": yesterday["close"],
                "daily_open": daily_open,
                "daily_open_utc": utc_today["open"] if utc_today else None,
                "weekly_open": weekly_open,
                "price": price,
            },
        )

    # -- events -------------------------------------------------------------

    def _diff(self, record: DailyBiasRecord,
              prev: Optional[DailyBiasRecord]) -> list[dict]:
        if prev is None:
            return [self._event(DailyBiasEvent.BIAS_LOCKED, record)]
        if prev.bias != record.bias:
            return [self._event(DailyBiasEvent.BIAS_CHANGED, record,
                                previous=prev.bias.value, previous_day=prev.day)]
        return [self._event(DailyBiasEvent.BIAS_LOCKED, record)]

    def _event(self, event: DailyBiasEvent, record: DailyBiasRecord, **extra) -> dict:
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
