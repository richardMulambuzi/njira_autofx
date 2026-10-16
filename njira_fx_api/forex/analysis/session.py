"""
Market Session Engine (m.md §34).

Tracks forex / indices / commodities market sessions in a timezone-aware,
UTC-only way (broker timestamps arrive as UTC).  No local-machine time and no
hardcoded offsets are ever used — every boundary decision is derived from the
UTC hour of the supplied tick timestamp.

Sessions (UTC):
    ASIAN              00:00 – 08:00
    LONDON             08:00 – 16:00
    NEW_YORK           13:00 – 21:00
    LONDON_NY_OVERLAP  13:00 – 16:00   (highest-liquidity window)
    OFF_SESSION        none of the above

Per symbol the engine maintains the running session open / high / low / range,
a tick count, and derives:
    - price_position      where price sits inside the session range (0=low,1=high)
    - session_expansion   how far the session has expanded vs its open (ratio)
    - session_liquidity_sweep  whether price took out a session extreme and
                              retraced back through the open (a liquidity grab)

Thread-safe: all per-symbol state is guarded by a single lock. Best-effort:
every public method swallows its own exceptions so a bad tick never crashes
the pipeline (mirrors volatility.py / microstructure.py behaviour).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("forex.analysis.session")


class MarketSession(Enum):
    ASIAN = "ASIAN"                      # 00:00 - 08:00 UTC
    LONDON = "LONDON"                    # 08:00 - 16:00 UTC
    NEW_YORK = "NEW_YORK"                # 13:00 - 21:00 UTC
    LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"  # 13:00 - 16:00 UTC
    OFF_SESSION = "OFF_SESSION"


# Session UTC boundaries: (start_hour, end_hour) — half-open [start, end).
SESSION_TIMES = {
    MarketSession.ASIAN: (0, 8),
    MarketSession.LONDON: (8, 16),
    MarketSession.NEW_YORK: (13, 21),
    MarketSession.LONDON_NY_OVERLAP: (13, 16),
}


@dataclass
class SessionState:
    """Mutable per-symbol session state (guarded by the engine lock)."""
    session: MarketSession
    session_open: float       # first price of the session
    session_high: float       # highest price in session
    session_low: float        # lowest price in session
    session_range: float      # high - open
    tick_count: int           # ticks in this session
    started_at: datetime
    last_price: float


@dataclass
class SessionMetrics:
    """Current session state for a symbol."""
    current_session: str      # "LONDON", "NEW_YORK", etc.
    session_open: float
    session_high: float
    session_low: float
    session_range: float
    price_position: float     # 0.0 = at low, 1.0 = at high (within session range)
    tick_count: int
    is_overlap: bool          # London/NY overlap active
    hours_into_session: float
    # ── m.md §34 "Track" extensions (derived, no extra persistent state) ──
    session_expansion: float = 0.0       # (high - low) / open  (ratio)
    session_liquidity_sweep: str = "NONE"  # NONE | HIGH | LOW | BOTH

    def to_dict(self) -> dict:
        return {
            "current_session": self.current_session,
            "session_open": self.session_open,
            "session_high": self.session_high,
            "session_low": self.session_low,
            "session_range": self.session_range,
            "price_position": self.price_position,
            "tick_count": self.tick_count,
            "is_overlap": self.is_overlap,
            "hours_into_session": self.hours_into_session,
            "session_expansion": self.session_expansion,
            "session_liquidity_sweep": self.session_liquidity_sweep,
        }


class SessionEngine:
    """
    Per-symbol market-session tracker driven by live ticks.

    Usage:
        engine = SessionEngine()
        metrics = engine.update("EURUSD", mid_price, utc_datetime)
        if engine.is_new_session("EURUSD"):
            ...  # trigger session-open analysis
        high = engine.get_session_high("EURUSD")
        pos  = engine.get_price_position("EURUSD")
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}   # symbol → SessionState
        # Transient flags (not part of the public SessionState contract):
        self._new_session: dict[str, bool] = {}        # last update crossed a boundary
        self._sweep: dict[str, dict] = {}              # internal liquidity-sweep state
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────

    def update(
        self, symbol: str, price: float, timestamp: datetime
    ) -> SessionMetrics:
        """
        Update session state with a new tick. Returns current session metrics.

        ``timestamp`` MUST be timezone-aware (UTC). A naive timestamp is treated
        as UTC defensively; a tz-aware timestamp in any zone is converted to UTC
        before the hour is read (no hardcoded offsets).
        """
        try:
            price = float(price)
            if not (price > 0) or price != price:  # reject <=0 and NaN
                logger.debug("[SESSION] %s bad price %.6f — skipped", symbol, price)
                return self.get_metrics(symbol) or self._zero_metrics()

            utc_ts = self._to_utc(timestamp)
            new_session = self._get_session(utc_ts.hour)

            with self._lock:
                prev = self._sessions.get(symbol)
                prev_session = prev.session if prev else None
                is_new = prev is not None and prev_session != new_session

                if prev is None or prev_session != new_session:
                    # New session — reset state.
                    state = SessionState(
                        session=new_session,
                        session_open=price,
                        session_high=price,
                        session_low=price,
                        session_range=0.0,
                        tick_count=1,
                        started_at=utc_ts,
                        last_price=price,
                    )
                    self._sessions[symbol] = state
                    self._sweep[symbol] = {
                        "took_high": False,
                        "took_low": False,
                        "swept_high": False,
                        "swept_low": False,
                    }
                else:
                    state = prev
                    made_new_high = price > state.session_high
                    made_new_low = price < state.session_low
                    if made_new_high:
                        state.session_high = price
                    if made_new_low:
                        state.session_low = price
                    state.last_price = price
                    state.tick_count += 1
                    state.session_range = state.session_high - state.session_open
                    self._update_sweep(symbol, state, price, made_new_high, made_new_low)

                self._new_session[symbol] = is_new
                return self._metrics_from_state(symbol, state, utc_ts)
        except Exception as exc:  # noqa: BLE001 — one bad tick must not crash stream
            logger.debug("[SESSION] update failed for %s: %s", symbol, exc)
            m = self.get_metrics(symbol)
            return m or self._zero_metrics()

    def get_metrics(self, symbol: str) -> Optional[SessionMetrics]:
        """Get current session metrics for a symbol (None if no data yet)."""
        with self._lock:
            st = self._sessions.get(symbol)
            if st is None:
                return None
            now = datetime.now(timezone.utc)
            return self._metrics_from_state(symbol, st, now)

    def get_session_high(self, symbol: str) -> Optional[float]:
        """Get the current session's high price."""
        with self._lock:
            st = self._sessions.get(symbol)
            return round(st.session_high, 8) if st else None

    def get_session_low(self, symbol: str) -> Optional[float]:
        """Get the current session's low price."""
        with self._lock:
            st = self._sessions.get(symbol)
            return round(st.session_low, 8) if st else None

    def get_price_position(self, symbol: str) -> float:
        """
        Where is price within the session range? 0.0=low, 1.0=high.

        Returns 0.5 (flat / neutral) when there is no data or the session is
        perfectly flat.
        """
        with self._lock:
            st = self._sessions.get(symbol)
            if st is None:
                return 0.5
            hi = st.session_high
            lo = st.session_low
            if hi == lo:
                return 0.5
            pos = (st.last_price - lo) / (hi - lo)
            return max(0.0, min(1.0, pos))

    def is_new_session(self, symbol: str) -> bool:
        """
        Did a new session just start? (for session-based analysis triggers).

        True only when the most recent ``update`` crossed a session boundary
        (i.e. there was a previous session and it differs from the current one).
        The flag is cleared on the next ``update`` for that symbol.
        """
        with self._lock:
            return bool(self._new_session.get(symbol, False))

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._new_session.clear()
            self._sweep.clear()

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _get_session(utc_hour: int) -> MarketSession:
        """Map a UTC hour to the active market session (half-open hours)."""
        if 13 <= utc_hour < 16:
            return MarketSession.LONDON_NY_OVERLAP
        if 8 <= utc_hour < 16:
            return MarketSession.LONDON
        if 13 <= utc_hour < 21:
            return MarketSession.NEW_YORK
        if utc_hour < 8 or utc_hour >= 21:
            return MarketSession.ASIAN
        return MarketSession.OFF_SESSION

    @staticmethod
    def _to_utc(ts: datetime) -> datetime:
        """Coerce any datetime to a UTC-aware datetime (no hardcoded offsets)."""
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    def _update_sweep(
        self,
        symbol: str,
        state: SessionState,
        price: float,
        made_new_high: bool,
        made_new_low: bool,
    ) -> None:
        """
        Liquidity-sweep detection (internal).

        A sweep = price takes out a session extreme (new high/low) and then
        retraces back through the session open.  This is the classic
        "liquidity grab" that often precedes a reversal.  State is kept in the
        private ``_sweep`` dict so the public ``SessionState`` contract is
        untouched.
        """
        sw = self._sweep.get(symbol)
        if sw is None:
            sw = {
                "took_high": False,
                "took_low": False,
                "swept_high": False,
                "swept_low": False,
            }
            self._sweep[symbol] = sw
        if made_new_high:
            sw["took_high"] = True
        if made_new_low:
            sw["took_low"] = True
        open_px = state.session_open
        if sw["took_high"] and price < open_px:
            sw["swept_high"] = True
        if sw["took_low"] and price > open_px:
            sw["swept_low"] = True

    def _metrics_from_state(
        self, symbol: str, state: SessionState, utc_ts: datetime
    ) -> SessionMetrics:
        hi = state.session_high
        lo = state.session_low
        open_px = state.session_open
        price = state.last_price

        # Price position within the session range (clamped 0..1).
        if hi == lo:
            pos = 0.5
        else:
            pos = (price - lo) / (hi - lo)
            pos = max(0.0, min(1.0, pos))

        hours = (utc_ts - state.started_at).total_seconds() / 3600.0
        expansion = (hi - lo) / open_px if open_px > 0 else 0.0

        sw = self._sweep.get(symbol, {})
        if sw.get("swept_high") and sw.get("swept_low"):
            sweep = "BOTH"
        elif sw.get("swept_high"):
            sweep = "HIGH"
        elif sw.get("swept_low"):
            sweep = "LOW"
        else:
            sweep = "NONE"

        return SessionMetrics(
            current_session=state.session.value,
            session_open=round(open_px, 8),
            session_high=round(hi, 8),
            session_low=round(lo, 8),
            session_range=round(state.session_range, 8),
            price_position=round(pos, 4),
            tick_count=state.tick_count,
            is_overlap=(state.session == MarketSession.LONDON_NY_OVERLAP),
            hours_into_session=round(hours, 3),
            session_expansion=round(expansion, 6),
            session_liquidity_sweep=sweep,
        )

    @staticmethod
    def _zero_metrics() -> SessionMetrics:
        """Neutral metrics returned when no data exists and an update fails."""
        return SessionMetrics(
            current_session=MarketSession.OFF_SESSION.value,
            session_open=0.0,
            session_high=0.0,
            session_low=0.0,
            session_range=0.0,
            price_position=0.5,
            tick_count=0,
            is_overlap=False,
            hours_into_session=0.0,
            session_expansion=0.0,
            session_liquidity_sweep="NONE",
        )
