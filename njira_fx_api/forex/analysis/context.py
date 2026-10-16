"""
Market Context State (improve.md §17, Phase 4).

A single, incrementally-updated snapshot of market context that all strategies
consume instead of rebuilding per-evaluation.  Every engine access is wrapped in
try/except so a missing or raising engine never breaks the candle-close pipeline.

The snapshot exposes the full §17 field set with safe defaults, so consumers can
read any field without a KeyError.  ``MarketContextEngine`` reuses the exact same
engine calls ``SignalEngine._build_market_state`` already makes (structure bias,
spread-regime mapping EXPANDED→WIDE, volatility regime, FVG status) plus session,
daily range, liquidity sweeps, volume profile and a per-timeframe structure bias
map for multi-timeframe alignment.
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("forex.analysis.context")

# Timeframes (seconds) used for the multi-timeframe structure-bias map.
MTF_TIMEFRAMES: tuple[int, ...] = (60, 300, 900, 3600, 14400, 86400)


@dataclass
class MarketContextState:
    """Coherent snapshot of market context for one symbol (improve.md §17)."""

    symbol: str = ""
    current_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    timestamp: Optional[datetime] = None
    data_freshness: float = 0.0  # seconds since last update

    daily_high: float = 0.0
    daily_low: float = 0.0
    previous_day_high: float = 0.0
    previous_day_low: float = 0.0

    session: str = ""
    session_high: float = 0.0
    session_low: float = 0.0

    trend: str = ""
    trend_strength: float = 0.0
    market_structure: str = ""
    structure_state: str = ""

    liquidity_levels: list = field(default_factory=list)
    liquidity_sweeps: list = field(default_factory=list)

    order_blocks: list = field(default_factory=list)
    major_order_blocks: list = field(default_factory=list)

    fvgs: list = field(default_factory=list)
    imbalances: list = field(default_factory=list)

    volume_profile: Optional[dict] = None
    poc: float = 0.0
    vah: float = 0.0
    val: float = 0.0
    hvn: list = field(default_factory=list)
    lvn: list = field(default_factory=list)

    order_flow: dict = field(default_factory=dict)

    # Extra fields needed to reproduce the contradiction-engine vocabulary
    # (improve.md §37) without re-querying engines.
    spread_regime: str = "NORMAL"
    volatility_regime: str = "NORMAL"

    multi_timeframe_context: dict = field(default_factory=dict)

    active_strategy_states: dict = field(default_factory=dict)
    candidate_setups: list = field(default_factory=list)

    signal_state: str = ""
    signal_strength: float = 0.0
    signal_confidence: float = 0.0
    signal_invalidation: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize for API / Pusher payloads (never raises)."""
        return {
            "symbol": self.symbol,
            "current_price": self.current_price,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "data_freshness": self.data_freshness,
            "daily_high": self.daily_high,
            "daily_low": self.daily_low,
            "previous_day_high": self.previous_day_high,
            "previous_day_low": self.previous_day_low,
            "session": self.session,
            "session_high": self.session_high,
            "session_low": self.session_low,
            "trend": self.trend,
            "trend_strength": self.trend_strength,
            "market_structure": self.market_structure,
            "structure_state": self.structure_state,
            "liquidity_levels": self.liquidity_levels,
            "liquidity_sweeps": self.liquidity_sweeps,
            "order_blocks": self.order_blocks,
            "major_order_blocks": self.major_order_blocks,
            "fvgs": self.fvgs,
            "imbalances": self.imbalances,
            "volume_profile": self.volume_profile,
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "hvn": self.hvn,
            "lvn": self.lvn,
            "order_flow": self.order_flow,
            "spread_regime": self.spread_regime,
            "volatility_regime": self.volatility_regime,
            "multi_timeframe_context": self.multi_timeframe_context,
            "active_strategy_states": self.active_strategy_states,
            "candidate_setups": self.candidate_setups,
            "signal_state": self.signal_state,
            "signal_strength": self.signal_strength,
            "signal_confidence": self.signal_confidence,
            "signal_invalidation": self.signal_invalidation,
        }


class MarketContextEngine:
    """
    Incrementally maintains a :class:`MarketContextState` per symbol.

    Constructed with the same engine set ``SignalEngine`` receives.  ``update``
    is best-effort: any engine/method that is missing or raises is skipped and
    the corresponding field keeps its neutral default.
    """

    def __init__(
        self,
        structure_engines: dict[int, Any] | None = None,
        liquidity_engines: dict[int, Any] | None = None,
        ob_engines: dict[int, Any] | None = None,
        fvg_engines: dict[int, Any] | None = None,
        poc_engines: dict[int, Any] | None = None,
        microstructure_engine: Any | None = None,
        order_flow_engines: dict[int, Any] | None = None,
        volatility_engine: Any | None = None,
        session_engine: Any | None = None,
        spread_engine: Any | None = None,
        volume_profile_engine: Any | None = None,
        cache: Any | None = None,
    ) -> None:
        self._structure = structure_engines or {}
        self._liquidity = liquidity_engines or {}
        self._obs = ob_engines or {}
        self._fvgs = fvg_engines or {}
        self._poc = poc_engines or {}
        self._micro = microstructure_engine
        self._order_flow = order_flow_engines or {}
        self._vol = volatility_engine
        self._session = session_engine
        self._spread = spread_engine
        self._volume_profile = volume_profile_engine
        self._cache = cache
        self._states: dict[str, MarketContextState] = {}
        self._updated_at: dict[str, datetime] = {}
        # Dirty tracking: per symbol, per timeframe, last candle open_time processed.
        # Only re-query engines for timeframes that have new candles.
        self._last_candle_time: dict[str, dict[int, datetime]] = {}
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────

    def update(self, symbol: str, candle: Optional[dict] = None) -> None:
        """Incrementally refresh the context for ``symbol`` (best-effort).

        Uses dirty tracking: only re-queries engines for the timeframe of the
        candle that just closed. Other timeframes' cached values are reused.
        """
        try:
            with self._lock:
                st = self._states.get(symbol)
                if st is None:
                    st = MarketContextState(symbol=symbol)
                    self._states[symbol] = st
                # Extract timeframe from candle for dirty tracking
                tf = int(candle["timeframe"]) if candle and candle.get("timeframe") else None
                self._apply(symbol, st, candle, tf)
        except Exception as exc:  # noqa: BLE001 — never break the pipeline
            logger.debug("[CONTEXT] update failed for %s: %s", symbol, exc)

    def get(self, symbol: str) -> MarketContextState:
        """Return the current state (create-on-demand with defaults if unknown)."""
        with self._lock:
            st = self._states.get(symbol)
            if st is None:
                return MarketContextState(symbol=symbol)
            return st

    def invalidate(self, symbol: str) -> None:
        """Drop cached state for a symbol (e.g. on symbol change)."""
        with self._lock:
            self._states.pop(symbol, None)
            self._updated_at.pop(symbol, None)
            self._last_candle_time.pop(symbol, None)

    # ── Internal ────────────────────────────────────────────────────────

    def _apply(self, symbol: str, st: MarketContextState, candle: Optional[dict], tf: Optional[int]) -> None:
        now = datetime.now(timezone.utc)
        st.timestamp = (
            candle.get("open_time") if candle and candle.get("open_time") else now
        )
        if candle and candle.get("close") is not None:
            try:
                st.current_price = float(candle["close"])
            except (TypeError, ValueError):
                pass

        # ── Dirty tracking: check if this timeframe has a new candle ───────
        # Only re-query structure engines for timeframes with new candles.
        # Other engines (FVG, OB, liquidity, etc.) are also per-timeframe.
        symbol_times = self._last_candle_time.setdefault(symbol, {})
        is_new_candle = False
        if tf is not None and candle and candle.get("open_time"):
            candle_time = candle["open_time"]
            last_time = symbol_times.get(tf)
            if last_time is None or candle_time > last_time:
                is_new_candle = True
                symbol_times[tf] = candle_time

        # ── Spread regime (map micro vocabulary → contradiction vocabulary) ──
        # Microstructure is tick-driven, not candle-driven — always refresh.
        if self._micro is not None:
            try:
                m = self._micro.get_metrics(symbol)
                if m is not None:
                    st.spread = float(getattr(m, "spread_pips", 0.0) or 0.0)
                    st.spread_regime = (
                        "WIDE" if getattr(m, "spread_regime", "NORMAL") == "EXPANDED"
                        else "NORMAL"
                    )
            except Exception:  # noqa: BLE001
                pass

        # ── Order flow (ICT/SMC real order flow) ───────────────────────────
        # Candle-driven: update the engine for the closed candle's timeframe and
        # expose its current state. Best-effort.
        if self._order_flow and tf is not None and candle is not None:
            try:
                eng = self._order_flow.get(tf)
                if eng is not None:
                    eng.update(symbol, candle, tf)
                    st.order_flow = eng.get_state(symbol, tf)
            except Exception:  # noqa: BLE001
                pass

        # ── Volatility regime ─────────────────────────────────────────────
        # Volatility is candle-driven but uses ATR which accumulates — refresh on new candle.
        if self._vol is not None and (is_new_candle or tf is None):
            try:
                vm = self._vol.get_metrics(symbol)
                if vm is not None:
                    st.volatility_regime = getattr(vm, "regime", "NORMAL") or "NORMAL"
            except Exception:  # noqa: BLE001
                pass

        # ── Multi-timeframe structure bias ───────────────────────────────
        # Only refresh bias for timeframes with new candles (or all if tf unknown).
        mtf: dict[int, str] = dict(st.multi_timeframe_context)  # start with cached
        if is_new_candle or tf is None:
            for check_tf in MTF_TIMEFRAMES:
                # Skip timeframes that haven't received new candles
                if tf is not None and check_tf != tf:
                    continue
                eng = self._structure.get(check_tf)
                if eng is not None:
                    try:
                        b = eng.get_bias(check_tf)
                        if b is not None:
                            mtf[check_tf] = b.value.lower()
                    except Exception:  # noqa: BLE001
                        pass
        st.multi_timeframe_context = mtf

        htf_tf = (
            14400 if 14400 in self._structure
            else (86400 if 86400 in self._structure else None)
        )
        ltf_tf = (
            900 if 900 in self._structure
            else (300 if 300 in self._structure
                  else (60 if 60 in self._structure else None))
        )
        st.trend = mtf.get(htf_tf, "")
        st.structure_state = mtf.get(ltf_tf, "")
        if htf_tf is not None and htf_tf in self._structure:
            try:
                st.market_structure = self._structure[htf_tf].get_regime(htf_tf).value.lower()
            except Exception:  # noqa: BLE001
                pass
        biases = [v for v in mtf.values() if v]
        if biases:
            mode, count = Counter(biases).most_common(1)[0]
            st.trend_strength = round(count / len(biases), 3)

        # ── Session ──────────────────────────────────────────────────────
        # Session is time-based (UTC), not candle-driven — always refresh.
        if self._session is not None:
            try:
                sm = self._session.get_metrics(symbol)
                if sm is not None:
                    st.session = sm.current_session
                    st.session_high = sm.session_high
                    st.session_low = sm.session_low
            except Exception:  # noqa: BLE001
                pass

        # ── FVGs / imbalances ────────────────────────────────────────────
        # Only refresh for the timeframe that has a new candle.
        if is_new_candle or tf is None:
            fvgs: list[dict] = []
            target_tfs = [tf] if tf is not None else list(self._fvgs.keys())
            for check_tf in target_tfs:
                eng = self._fvgs.get(check_tf)
                if eng is not None:
                    try:
                        for f in eng.get_active():
                            fvgs.append(f.to_dict())
                    except Exception:  # noqa: BLE001
                        continue
            st.fvgs = fvgs
            st.imbalances = [
                f for f in fvgs
                if f.get("status") in ("FORMED", "OPEN", "PARTIALLY_FILLED")
            ]

        # ── Order blocks / major order blocks ────────────────────────────
        # Only refresh for the timeframe that has a new candle.
        if is_new_candle or tf is None:
            obs: list[dict] = []
            target_tfs = [tf] if tf is not None else list(self._obs.keys())
            for check_tf in target_tfs:
                eng = self._obs.get(check_tf)
                if eng is not None:
                    try:
                        for o in eng.get_active(limit=20):
                            obs.append(o.to_dict())
                    except Exception:  # noqa: BLE001
                        continue
            st.order_blocks = obs
            st.major_order_blocks = [
                o for o in obs if o.get("state") in ("VALID", "PARTIALLY_MITIGATED")
            ]

        # ── Liquidity levels / sweeps ────────────────────────────────────
        # Only refresh for the timeframe that has a new candle.
        if is_new_candle or tf is None:
            levels: list[dict] = []
            sweeps: list[dict] = []
            target_tfs = [tf] if tf is not None else list(self._liquidity.keys())
            for check_tf in target_tfs:
                eng = self._liquidity.get(check_tf)
                if eng is not None:
                    try:
                        for p in eng.get_pools():
                            levels.append(
                                {"pool_type": p.pool_type.value, "price": p.price,
                                 "state": p.state.value}
                            )
                        for p in eng.get_pools(state="SWEPT"):
                            sweeps.append(
                                {"pool_type": p.pool_type.value, "price": p.price,
                                 "state": p.state.value}
                            )
                    except Exception:  # noqa: BLE001
                        continue
            st.liquidity_levels = levels
            st.liquidity_sweeps = sweeps

        # ── Volume profile (POC / VAH / VAL / HVN / LVN) ─────────────────
        if self._volume_profile is not None:
            try:
                vp = self._volume_profile.get_profile(symbol)
                if vp is not None:
                    st.volume_profile = vp.to_dict()
                    st.poc = vp.poc
                    st.vah = vp.vah
                    st.val = vp.val
                    st.hvn = list(vp.hvn_levels)
                    st.lvn = list(vp.lvn_levels)
            except Exception:  # noqa: BLE001
                pass

        # ── POC fallback (rolling POC engine) if profile unavailable ─────
        if st.poc == 0.0 and self._poc:
            try:
                for tf in (86400, 14400, 3600):
                    pe = self._poc.get(tf)
                    if pe is not None:
                        poc = pe.get_poc(tf)
                        if poc is not None:
                            st.poc = poc
                            break
            except Exception:  # noqa: BLE001
                pass

        # ── Daily range ──────────────────────────────────────────────────
        dh, dl, pdh, pdl = self._daily_range(symbol)
        st.daily_high, st.daily_low = dh, dl
        st.previous_day_high, st.previous_day_low = pdh, pdl

        # ── Freshness (Phase 3 §48) ──────────────────────────────────────
        # Reflect the age of the underlying market data: time since the last
        # candle's open_time. The quality gate (quality_gate.py) treats
        # >300s as stale and suppresses signals, so a live gap makes freshness
        # grow instead of staying pinned at "just updated".
        if candle and candle.get("open_time") is not None:
            ot = candle["open_time"]
            if ot.tzinfo is None:
                ot = ot.replace(tzinfo=timezone.utc)
            st.data_freshness = round((now - ot).total_seconds(), 3)
        else:
            prev = self._updated_at.get(symbol, now)
            st.data_freshness = round((now - prev).total_seconds(), 3)
        self._updated_at[symbol] = now

    def _daily_range(self, symbol: str) -> tuple[float, float, float, float]:
        """Return (daily_high, daily_low, prev_day_high, prev_day_low)."""
        if self._cache is None:
            return (0.0, 0.0, 0.0, 0.0)
        try:
            daily = self._cache.get(symbol, 86400)
            if daily:
                highs = [c["high"] for c in daily]
                lows = [c["low"] for c in daily]
                dh, dl = max(highs), min(lows)
                if len(daily) >= 2:
                    return dh, dl, daily[-2]["high"], daily[-2]["low"]
                return dh, dl, 0.0, 0.0
            # Fallback: finest available timeframe, current UTC day.
            tfs = sorted(self._cache.timeframes(symbol))
            if not tfs:
                return (0.0, 0.0, 0.0, 0.0)
            candles = self._cache.get(symbol, tfs[0])
            today = datetime.now(timezone.utc).date()
            day = [c for c in candles if self._candle_date(c) == today]
            if not day:
                day = candles[-1:] if candles else []
            if day:
                return max(c["high"] for c in day), min(c["low"] for c in day), 0.0, 0.0
        except Exception:  # noqa: BLE001
            pass
        return (0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def _candle_date(candle: dict) -> Optional[object]:
        ot = candle.get("open_time")
        return getattr(ot, "date", lambda: None)() if ot is not None else None
