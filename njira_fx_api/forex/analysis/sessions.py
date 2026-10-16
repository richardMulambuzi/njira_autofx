"""
Intraday market-session timing (Malaysian SNR course).

Pure, stateless helpers — no I/O, no locks, no per-symbol state. Session
boundaries are fixed UTC hours so the result never depends on the local
machine clock (mirrors session.py: "broker timestamps arrive as UTC").

Sessions (UTC, standard forex):
    sydney   21:00 – 06:00  (wraps midnight)
    tokyo    00:00 – 09:00
    london   07:00 – 16:00
    newyork  12:00 – 21:00

Sessions overlap by design: 00:00–06:00 is sydney+tokyo, 12:00–16:00 is
london+newyork (peak liquidity). 06:00–07:00 is the dead gap.

All timestamps are epoch seconds (UTC), the same convention as the
``open_time`` field of a candle dict.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Session name -> UTC hour it opens. End hour is derived (see SESSIONS).
SESSION_OPEN_HOURS: dict[str, int] = {
    "sydney": 21,
    "tokyo": 0,
    "london": 7,
    "newyork": 12,
}

# Session name -> (start_hour, end_hour) where end < start means "wraps midnight".
SESSIONS: dict[str, tuple[int, int]] = {
    "sydney": (21, 6),
    "tokyo": (0, 9),
    "london": (7, 16),
    "newyork": (12, 21),
}

# Book §H — KILLZONES and the exact session opens used for entry timing:
#   • London Killzone  — around the London open.
#   • New York Open 13:15 / 13:30 — the book times the NY confluence here.
#   • London Open 0900 / Asian High — additional liquidity windows.
# KILLZONES map a label to the half-open UTC [start, end) hour window during
# which the book's confluence setups are considered highest-probability.
KILLZONES: dict[str, tuple[int, int]] = {
    "london_kz": (7, 10),      # 07:00–10:00 UTC around the London open
    "newyork_kz": (12, 15),    # 12:00–15:00 UTC, covers the 13:15/13:30 open
    "asian_high": (0, 2),      # 00:00–02:00 UTC, the Asian range / high
}

# Instrument home sessions used by default: AUDJPY is an Asia instrument.
# XAUUSD callers pass allowed=("london", "newyork").
DEFAULT_TRADE_SESSIONS: tuple[str, ...] = ("tokyo", "sydney")

_SECONDS_PER_DAY = 86400


def _seconds_of_day(ts_utc: float) -> int:
    """Whole seconds elapsed since 00:00 UTC on the ts_utc day."""
    dt = datetime.fromtimestamp(ts_utc, tz=timezone.utc)
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _in_window(hour: int, start: int, end: int) -> bool:
    """Is ``hour`` inside the half-open [start, end) session span?"""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight (e.g. sydney 21→06)


def session_of(ts_utc: float) -> list[str]:
    """
    Sessions active at ``ts_utc`` (epoch seconds, UTC).

    Returns every overlapping session in declaration order, e.g.
    ``["tokyo", "london"]`` at 08:00 UTC. Empty list in the 06:00–07:00 gap.
    """
    hour = datetime.fromtimestamp(ts_utc, tz=timezone.utc).hour
    return [name for name, (start, end) in SESSIONS.items() if _in_window(hour, start, end)]


def in_trade_session(
    ts_utc: float, allowed: tuple[str, ...] = DEFAULT_TRADE_SESSIONS
) -> bool:
    """True if ANY active session is in ``allowed`` (default: tokyo + sydney)."""
    return any(s in allowed for s in session_of(ts_utc))


def session_open(ts_utc: float, session: str, window_seconds: int = 3600) -> bool:
    """
    True if ``ts_utc`` falls in the first ``window_seconds`` of ``session``.

    This is the "timing your trades at the market opening session" rule:
    london opens 07:00 UTC, so with the default 3600s window only
    07:00–08:00 UTC qualifies. Handles the midnight wrap (sydney).
    """
    open_hour = SESSION_OPEN_HOURS[session]
    elapsed = (_seconds_of_day(ts_utc) - open_hour * 3600) % _SECONDS_PER_DAY
    return elapsed < window_seconds


def next_session_open(ts_utc: float, session: str) -> float:
    """
    Epoch seconds of the next strictly-future occurrence of ``session``'s open.

    Handles day rollover: tokyo at 23:00 UTC on 2026-08-03 → 2026-08-04 00:00 UTC.
    """
    dt = datetime.fromtimestamp(ts_utc, tz=timezone.utc).replace(
        hour=SESSION_OPEN_HOURS[session], minute=0, second=0, microsecond=0
    )
    if dt.timestamp() <= ts_utc:
        dt = datetime.fromtimestamp(
            dt.timestamp() + _SECONDS_PER_DAY, tz=timezone.utc
        )
    return dt.timestamp()


def killzones_of(ts_utc: float) -> list[str]:
    """
    Killzones active at ``ts_utc`` (epoch seconds, UTC).

    Book §H: entries that land inside a killzone (London KZ, New York Open
    13:15/13:30, the Asian range) carry the book's highest-probability timing,
    so this is used as confluence evidence and a confidence bonus — not a hard
    gate (the base requirement stays the instrument's home session).
    """
    hour = datetime.fromtimestamp(ts_utc, tz=timezone.utc).hour
    return [name for name, (start, end) in KILLZONES.items() if start <= hour < end]


def in_killzone(ts_utc: float) -> bool:
    """True if ``ts_utc`` falls inside any book killzone window."""
    return bool(killzones_of(ts_utc))
