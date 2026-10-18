"""
Signal Lifecycle Manager (Phase 4 — improvements.md §32, m.md §37–38).

Manages signal states and transitions:
CANDIDATE → WATCHING → TRIGGERED → CONFIRMED → INVALIDATED / EXPIRED / COMPLETED
WAITING is a first-class intermediate state (m.md §38): a setup whose
evidence is insufficient/contradicted is held in WAITING instead of being
forced into BUY/SELL.

Every transition records timestamp, reason, evidence, and score.
Never silently changes a signal.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from forex.sim.instruments import get_spec

logger = logging.getLogger("forex.analysis.signal_lifecycle")

SCHEMA_VERSION = "1.0"

# v1 envelope mappings (flat Signal → nested versioned envelope).
_SIGNAL_TYPE_BY_STRATEGY: dict[str, str] = {
    "malysian": "price_breakout",
    "moving_averages": "trend_continuation",
}


# ── v1 envelope defaults (pure helpers, no DB/network/clock) ─────────────
# SL/TP SOURCE: forex/analysis/signal_engine.py — SignalDraft.entry /
#   stop_loss / take_profit (built by _mtf_candidate_setup, _snr_take_profit,
#   _apply_strength_rr) and stored on Signal.entry / stop_loss / take_profit.
#   These helpers only READ those values, never recompute structure.
# RR THRESHOLDS: mirror signal_engine.MIN_RR_RATIO (0.5) and
#   MIN_CONFIDENCE_TO_CONFIRM (60.0), enforced by forex/analysis/polisher.py
#   (min_rr / min_confidence filters). Kept as local constants to avoid a
#   circular import (signal_engine imports this module).
V1_MIN_RR_RATIO = 0.5
V1_MIN_CONFIDENCE = 60.0
V1_DEFAULT_LEVERAGE = 2
V1_DEFAULT_POSITION_SIZE = 2.5
V1_DEFAULT_MAX_SLIPPAGE_BPS = 10
V1_ORDER_TYPE = "LIMIT"
V1_TIME_IN_FORCE = "GTD"
V1_EXPIRY_SECONDS = 3600  # expiresAt = created_at + 1h

# Precision per symbol (quote digits). Fallback 5 (FX) / 2 (crypto USDT).
V1_PRECISION_BY_SYMBOL: dict[str, int] = {
    "XAUUSD": 2,
    "EURUSD": 5,
    "BTCUSDT": 2,
}


def instrument_for_symbol(symbol: str) -> dict[str, object]:
    """Map a trading symbol to the v1 instrument block (pure).

    FX (e.g. XAUUSD/EURUSD): symbol as-is, exchange "pepperstone",
    coin = base currency (XAUUSD → XAU, EURUSD → EUR).
    Crypto (e.g. BTCUSDT): coin = base (BTC), exchange "binance".
    """
    sym = (symbol or "").upper()
    if sym.endswith("USDT"):
        base = sym[:-len("USDT")] or sym
        exchange = "binance"
    elif sym.endswith("USD"):
        base = sym[:-len("USD")] or sym
        exchange = "pepperstone"
    else:
        base, exchange = sym, "pepperstone"
    precision = V1_PRECISION_BY_SYMBOL.get(sym, 2 if exchange == "binance" else 5)
    return {"coin": base, "symbol": sym, "exchange": exchange, "precision": precision}


def execution_defaults(created_at: datetime) -> dict[str, object]:
    """v1 execution block (pure): LIMIT / GTD / created+1h / postOnly."""
    # ponytail: naive datetime assumed UTC; no clock read, caller passes created_at.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    expires = created_at + timedelta(seconds=V1_EXPIRY_SECONDS)
    return {
        "orderType": V1_ORDER_TYPE,
        "timeInForce": V1_TIME_IN_FORCE,
        "expiresAt": expires.isoformat(),
        "postOnly": True,
    }


def risk_defaults() -> dict[str, object]:
    """v1 risk block (pure constants)."""
    return {
        "leverage": V1_DEFAULT_LEVERAGE,
        "positionSize": V1_DEFAULT_POSITION_SIZE,
        "positionSizeUnit": "PERCENT_EQUITY",
        "maxSlippageBps": V1_DEFAULT_MAX_SLIPPAGE_BPS,
    }


def compute_rr(entry: float, stop_loss: float, take_profit: float) -> float:
    """Structural RR from prices — same formula as signal_engine drafts.

    ``abs(tp - entry) / risk``; 0.0 when risk <= 0. Reused for the
    rrThresholdMet fallback when the stored risk_reward_ratio is missing.
    """
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return 0.0
    return abs(take_profit - entry) / risk


def validation_for_signal(
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    risk_reward: float = 0.0,
) -> dict[str, object]:
    """v1 validation block (pure).

    priceSanity: entry > 0 and SL/TP on the correct side
      (BUY: sl < entry < tp; SELL: tp < entry < sl).
    rrThresholdMet: RR >= V1_MIN_RR_RATIO (0.5, mirrors MIN_RR_RATIO).
    spreadCheck: True placeholder (no spread feed in this scope).
    """
    is_buy = (direction or "").upper() == "BUY"
    if is_buy:
        sane = entry > 0 and stop_loss > 0 and take_profit > 0 \
            and stop_loss < entry < take_profit
    else:
        sane = entry > 0 and stop_loss > 0 and take_profit > 0 \
            and take_profit < entry < stop_loss
    rr = risk_reward if risk_reward > 0 else compute_rr(entry, stop_loss, take_profit)
    return {
        "priceSanity": bool(sane),
        "rrThresholdMet": bool(rr >= V1_MIN_RR_RATIO),
        "spreadCheck": True,  # placeholder — no live spread feed here
    }


class SignalState(Enum):
    CANDIDATE = "CANDIDATE"
    WATCHING = "WATCHING"
    WAITING = "WAITING"        # NEW (m.md §38) — evidence insufficient for trade
    TRIGGERED = "TRIGGERED"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


# Valid transitions: from_state → set of allowed to_states
_VALID_TRANSITIONS: dict[SignalState, set[SignalState]] = {
    SignalState.CANDIDATE: {SignalState.WATCHING, SignalState.WAITING, SignalState.CONFIRMED, SignalState.INVALIDATED, SignalState.EXPIRED},
    SignalState.WATCHING: {SignalState.TRIGGERED, SignalState.WAITING, SignalState.INVALIDATED, SignalState.EXPIRED},
    SignalState.WAITING: {SignalState.TRIGGERED, SignalState.CONFIRMED, SignalState.INVALIDATED, SignalState.EXPIRED},
    SignalState.TRIGGERED: {SignalState.CONFIRMED, SignalState.INVALIDATED, SignalState.EXPIRED},
    SignalState.CONFIRMED: {SignalState.COMPLETED, SignalState.INVALIDATED, SignalState.EXPIRED},
    SignalState.INVALIDATED: set(),  # terminal
    SignalState.EXPIRED: set(),      # terminal
    SignalState.COMPLETED: set(),    # terminal
}

# Terminal states: a setup in one of these is considered closed/dead and is
# NOT a candidate for dedup or re-evaluation (improve.md §29, §32).
_TERMINAL_STATES: frozenset[SignalState] = frozenset({
    SignalState.INVALIDATED,
    SignalState.EXPIRED,
    SignalState.COMPLETED,
})

# v1 envelope status: every open lifecycle state is an unfilled intent.
_STATUS_V1_BY_STATE: dict[SignalState, str] = {
    SignalState.CANDIDATE: "PENDING",
    SignalState.WATCHING: "PENDING",
    SignalState.WAITING: "PENDING",
    SignalState.TRIGGERED: "PENDING",
    SignalState.CONFIRMED: "PENDING",
    SignalState.COMPLETED: "FILLED",
    SignalState.INVALIDATED: "CANCELLED",
    SignalState.EXPIRED: "CANCELLED",
}


@dataclass
class Transition:
    from_state: SignalState
    to_state: SignalState
    timestamp: datetime
    reason: str
    evidence: dict[str, Any]
    score: float


@dataclass
class Signal:
    """A trading signal with full lifecycle tracking."""
    signal_id: str
    symbol: str
    direction: str  # "BUY" or "SELL"
    state: SignalState = SignalState.CANDIDATE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    confidence_score: float = 0.0
    confidence_evidence: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""
    strategy: str = ""
    timeframe: str = ""
    market_regime: str = ""
    risk_reward_ratio: float = 0.0
    strength: str = ""  # "RECOMMENDED", "STRONG", "NORMAL", "WEAK", "RISKY" — derived from confidence
    transitions: list[Transition] = field(default_factory=list)
    combined: bool = False
    source_strategies: list[str] = field(default_factory=list)

    @staticmethod
    def classify_strength(confidence: float) -> str:
        """Map confidence score to one of five signal strength tiers.

        - RECOMMENDED >= 80  highest conviction (multi-strategy agreement + confirmed)
        - STRONG     >= 65
        - NORMAL     >= 50
        - WEAK       >= 35  a real signal but with low conviction
        - RISKY      <  35  counter-trend / speculative — still a valid signal
        """
        if confidence >= 80:
            return "RECOMMENDED"
        if confidence >= 65:
            return "STRONG"
        if confidence >= 50:
            return "NORMAL"
        if confidence >= 35:
            return "WEAK"
        return "RISKY"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for ClickHouse / Pusher."""
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
            "confidence_score": self.confidence_score,
            "confidence_evidence": self.confidence_evidence,
            "explanation": self.explanation,
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "market_regime": self.market_regime,
            "risk_reward_ratio": self.risk_reward_ratio,
            "strength": self.strength or Signal.classify_strength(self.confidence_score),
            "combined": self.combined,
            "source_strategies": list(self.source_strategies),
        }

    def to_dict_v1(self) -> dict[str, Any]:
        """Versioned nested envelope (v1) — additive, ``to_dict()`` untouched.

        Pure — reads only self fields + module-level helpers. Reuses
        ``classify_strength`` (bias), ``instrument_for_symbol``,
        ``execution_defaults`` / ``risk_defaults`` / ``validation_for_signal``.
        """
        # ponytail: naive datetime assumed UTC; Z-suffix for wire consistency.
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_z = created.isoformat().replace("+00:00", "Z")
        unix_ts = int(created.timestamp())
        norm_strategy = (self.strategy or "").strip().lower()
        # Idempotent key: same setup + timeframe + same bar timestamp → same
        # key on retry. Timeframe is part of the key so the same setup on two
        # timeframes in the same second cannot collide (default 'multi').
        setup = SignalLifecycleManager.setup_key(self.symbol, self.direction, self.strategy)
        timeframe = (self.timeframe or "").strip() or "multi"
        inst = instrument_for_symbol(self.symbol)
        precision = int(inst.get("precision", 5))  # type: ignore[arg-type]
        ex = execution_defaults(self.created_at)
        rk = risk_defaults()
        rr = self.risk_reward_ratio or compute_rr(self.entry, self.stop_loss, self.take_profit)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "id": self.signal_id,
            "idempotencyKey": f"{setup}:{timeframe}:{unix_ts}",
            "createdAt": created_z,
            "status": _STATUS_V1_BY_STATE.get(self.state, "PENDING"),
            "source": {
                "platform": "njira_fx",
                "signalType": _SIGNAL_TYPE_BY_STRATEGY.get(norm_strategy, norm_strategy or "price_breakout"),
                "tags": list(dict.fromkeys(
                    t for t in [self.strategy, self.timeframe, self.market_regime, *self.source_strategies] if t
                )),
            },
            "target": {
                "platform": "njira_fx",
                "account": "default",
                "clientOrderId": self.signal_id,
            },
            "instrument": {
                "coin": inst.get("coin"),
                "symbol": inst.get("symbol"),
                "exchange": inst.get("exchange"),
                "pricePrecision": precision,
                # ponytail: fixed qty precision; widen per-venue only if fills reject.
                "quantityPrecision": 6 if inst.get("exchange") == "binance" else 2,
            },
            "bias": {
                "direction": self.direction,
                "side": self.direction,
                "confidence": self.confidence_score,
                "strength": self.strength or Signal.classify_strength(self.confidence_score),
            },
            "execution": {
                "orderType": ex.get("orderType"),
                "limitPrice": self.entry,
                "estimatedEntryTime": created_z,
                "timeInForce": ex.get("timeInForce"),
                "expiresAt": str(ex.get("expiresAt")).replace("+00:00", "Z"),
                "postOnly": ex.get("postOnly"),
                "reduceOnly": False,
            },
            "risk": {
                "stopLoss": self.stop_loss,
                "takeProfit": self.take_profit,
                "riskRewardRatio": rr,
                "leverage": rk.get("leverage"),
                "positionSize": rk.get("positionSize"),
                "positionSizeType": rk.get("positionSizeUnit"),
                "maxSlippageBps": rk.get("maxSlippageBps"),
            },
            "validation": validation_for_signal(
                self.direction, self.entry, self.stop_loss,
                self.take_profit, rr,
            ),
        }


class SignalLifecycleManager:
    """
    Manages signal state machines.

    Usage:
        mgr = SignalLifecycleManager()
        signal = mgr.create(symbol="EURUSD", direction="BUY", ...)
        mgr.transition(signal, SignalState.WATCHING, reason="OB detected", evidence={...})
    """

    def __init__(self, max_active: int = 20) -> None:
        self._max_active = max_active
        self._signals: dict[str, Signal] = {}  # signal_id → Signal
        self._recent_events: list[dict] = []

    def create(
        self,
        symbol: str,
        direction: str,
        entry: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        confidence: float = 0.0,
        evidence: dict | None = None,
        explanation: str = "",
        strategy: str = "",
        timeframe: str = "",
        market_regime: str = "",
    ) -> Signal:
        """Create a new signal in CANDIDATE state."""
        # Dedup: don't create if we already have an active signal for this symbol
        active = [s for s in self._signals.values()
                  if s.symbol == symbol and s.state not in (
                      SignalState.INVALIDATED, SignalState.EXPIRED, SignalState.COMPLETED)]
        if len(active) >= self._max_active:
            logger.warning("Max active signals reached for %s — dropping oldest", symbol)

        sig_id = f"sig_{uuid.uuid4().hex[:12]}"
        signal = Signal(
            signal_id=sig_id,
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence_score=confidence,
            confidence_evidence=evidence or {},
            explanation=explanation,
            strategy=strategy,
            timeframe=timeframe,
            market_regime=market_regime,
        )
        self._signals[sig_id] = signal
        self._recent_events.append({
            "event": "SIGNAL_CREATED",
            "signal_id": sig_id,
            "symbol": symbol,
            "direction": direction,
        })
        return signal

    @staticmethod
    def setup_key(symbol: str, direction: str, strategy: str) -> str:
        """
        Canonical identity for a setup (improve.md §32 — cooldown by identity).

        Two setups with the same symbol + direction + strategy are the SAME
        setup; a new direction or structure (different strategy) is a NEW key
        and therefore allowed to produce a fresh signal.
        """
        norm = (strategy or "").strip().lower() or "unknown"
        return f"{symbol}:{direction}:{norm}"

    def find_active_setup(
        self, symbol: str, direction: str, strategy: str
    ) -> Optional["Signal"]:
        """
        Return the first non-terminal signal matching the setup key, else None.

        This is the dedup lookup: callers should update the returned signal
        in place rather than creating a duplicate (improve.md §29, §32).
        """
        key = self.setup_key(symbol, direction, strategy)
        for sig in self._signals.values():
            if sig.state in _TERMINAL_STATES:
                continue
            if self.setup_key(sig.symbol, sig.direction, sig.strategy) == key:
                return sig
        return None

    def transition(
        self,
        signal: Signal,
        to_state: SignalState,
        reason: str,
        evidence: dict | None = None,
        score: float = 0.0,
    ) -> bool:
        """
        Attempt a state transition.  Returns True if valid.

        Logs the transition and records it in the signal's history.
        """
        from_state = signal.state

        # Validate transition
        allowed = _VALID_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            logger.warning(
                "Invalid transition: %s → %s for signal %s (allowed: %s)",
                from_state.value, to_state.value, signal.signal_id,
                [s.value for s in allowed],
            )
            return False

        # Apply transition
        signal.state = to_state
        transition = Transition(
            from_state=from_state,
            to_state=to_state,
            timestamp=datetime.now(timezone.utc),
            reason=reason,
            evidence=evidence or {},
            score=score,
        )
        signal.transitions.append(transition)

        event = {
            "event": "SIGNAL_TRANSITION",
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "from": from_state.value,
            "to": to_state.value,
            "reason": reason,
            "score": score,
        }
        self._recent_events.append(event)
        if len(self._recent_events) > 100:
            self._recent_events = self._recent_events[-100:]

        logger.info(
            "[SIGNAL] %s %s %s → %s: %s (score=%.1f)",
            signal.symbol, signal.direction, from_state.value,
            to_state.value, reason, score,
        )
        return True

    def get_active(self, symbol: str | None = None) -> list[Signal]:
        """Return all non-terminal signals, optionally filtered by symbol."""
        active = [
            s for s in self._signals.values()
            if s.state not in (SignalState.INVALIDATED, SignalState.EXPIRED, SignalState.COMPLETED)
        ]
        if symbol:
            active = [s for s in active if s.symbol == symbol]
        return sorted(active, key=lambda s: s.confidence_score, reverse=True)

    def get_by_id(self, signal_id: str) -> Optional[Signal]:
        return self._signals.get(signal_id)

    def get_recent_events(self, n: int = 10) -> list[dict]:
        return self._recent_events[-n:]

    def expire_stale(self, max_age_candles: int = 50, current_candle_index: int = 0) -> list[Signal]:
        """Expire signals that are too old (no confirmation within window)."""
        expired = []
        for sig in list(self._signals.values()):
            if sig.state in (SignalState.CANDIDATE, SignalState.WATCHING, SignalState.WAITING):
                # Use created_at age approximation
                self.transition(sig, SignalState.EXPIRED, reason="Stale — no confirmation within window")
                expired.append(sig)
        return expired

    def update_trailing_stop(
        self,
        signal: Signal,
        current_price: float,
        atr_pips: float | None = None,
        trailing_pips: float = 20.0,
        partial_tp_r: float = 1.0,
        partial_tp_fraction: float = 0.5,
    ) -> dict[str, Any]:
        """
        Update trailing stop and partial TP for a CONFIRMED signal.

        Args:
            signal: The signal to update (must be CONFIRMED).
            current_price: Current market price.
            atr_pips: ATR in pips for volatility-based trailing. If None, uses trailing_pips.
            trailing_pips: Fixed pip distance for trailing stop (default 20 pips = 0.0020 for EURUSD).
            partial_tp_r: R-multiple at which to take partial profit (default 1R).
            partial_tp_fraction: Fraction of position to close at partial TP (default 0.5).

        Returns:
            Dict with actions taken: {"trailing_activated": bool, "sl_moved": bool,
            "partial_tp_taken": bool, "new_sl": float, "new_tp": float, "reason": str}
        """
        if signal.state != SignalState.CONFIRMED:
            return {"trailing_activated": False, "sl_moved": False, "partial_tp_taken": False,
                    "new_sl": signal.stop_loss, "new_tp": signal.take_profit,
                    "reason": f"Signal not CONFIRMED (state={signal.state.value})"}

        if signal.risk_reward_ratio <= 0 or signal.entry <= 0 or signal.stop_loss <= 0:
            return {"trailing_activated": False, "sl_moved": False, "partial_tp_taken": False,
                    "new_sl": signal.stop_loss, "new_tp": signal.take_profit,
                    "reason": "Invalid signal prices/RR"}

        is_buy = signal.direction == "BUY"
        
        # Use original risk for R calculations (stored at activation), fallback to current
        evidence = signal.confidence_evidence
        original_risk = evidence.get("original_risk")
        current_risk = abs(signal.entry - signal.stop_loss)
        risk = original_risk if original_risk is not None else current_risk
        if risk <= 0:
            return {"trailing_activated": False, "sl_moved": False, "partial_tp_taken": False,
                    "new_sl": signal.stop_loss, "new_tp": signal.take_profit,
                    "reason": "Zero risk distance"}

        # Store original risk on first call
        if original_risk is None:
            evidence["original_risk"] = current_risk

        # Calculate current profit in R (using original risk)
        if is_buy:
            profit_pips = current_price - signal.entry
        else:
            profit_pips = signal.entry - current_price
        current_r = profit_pips / risk if risk > 0 else 0.0
        # Small epsilon to handle float precision at exact R boundaries
        current_r = round(current_r, 6)

        trailing_active = evidence.get("trailing_active", False)
        partial_tp_taken = evidence.get("partial_tp_taken", False)

        result = {
            "trailing_activated": False,
            "sl_moved": False,
            "partial_tp_taken": False,
            "new_sl": signal.stop_loss,
            "new_tp": signal.take_profit,
            "reason": "",
        }

        # 1) Partial TP at 1R: close fraction, move SL to breakeven
        if not partial_tp_taken and current_r >= partial_tp_r:
            evidence["partial_tp_taken"] = True
            evidence["partial_tp_price"] = current_price
            evidence["partial_tp_r"] = round(current_r, 2)
            # Move SL to breakeven (entry price)
            new_sl = signal.entry
            if is_buy and new_sl > signal.stop_loss:
                signal.stop_loss = new_sl
                result["sl_moved"] = True
                result["new_sl"] = new_sl
            elif not is_buy and new_sl < signal.stop_loss:
                signal.stop_loss = new_sl
                result["sl_moved"] = True
                result["new_sl"] = new_sl
            result["partial_tp_taken"] = True
            result["reason"] = f"Partial TP at {current_r:.2f}R ({partial_tp_fraction*100:.0f}% closed, SL→BE)"

        # 2) Activate trailing stop at 1R (or after partial TP)
        symbol = signal.symbol.upper()
        pip_size = get_spec(symbol).pip_size
        trail_distance_price = (atr_pips if atr_pips is not None else trailing_pips) * pip_size
        
        if not trailing_active and current_r >= partial_tp_r:
            evidence["trailing_active"] = True
            evidence["trail_distance_price"] = trail_distance_price
            result["trailing_activated"] = True
            if not result["reason"]:
                result["reason"] = f"Trailing stop activated at {current_r:.2f}R (trail={(trail_distance_price/pip_size):.0f} pips)"

        # 3) Trail the stop if active
        if trailing_active:
            trail_distance_price = evidence.get("trail_distance_price", trail_distance_price)
            if is_buy:
                # For BUY: trail stop up as price rises
                proposed_sl = current_price - trail_distance_price
                if proposed_sl > signal.stop_loss:
                    signal.stop_loss = proposed_sl
                    result["sl_moved"] = True
                    result["new_sl"] = proposed_sl
                    result["reason"] = f"Trailing stop moved to {proposed_sl:.5f} (price {current_price:.5f} - {trail_distance_price/pip_size:.0f} pips)"
            else:
                # For SELL: trail stop down as price falls
                proposed_sl = current_price + trail_distance_price
                if proposed_sl < signal.stop_loss:
                    signal.stop_loss = proposed_sl
                    result["sl_moved"] = True
                    result["new_sl"] = proposed_sl
                    result["reason"] = f"Trailing stop moved to {proposed_sl:.5f} (price {current_price:.5f} + {trail_distance_price/pip_size:.0f} pips)"

        # Update signal's evidence
        signal.confidence_evidence = evidence
        return result

    def reset(self) -> None:
        self._signals.clear()
        self._recent_events.clear()


# Global lifecycle manager registry — populated by SignalEngine at startup, read by API views.
_global_lifecycle: Optional["SignalLifecycleManager"] = None


def set_global_lifecycle(lifecycle: "SignalLifecycleManager") -> None:
    """Register the running lifecycle manager so API views can read live signals."""
    global _global_lifecycle
    _global_lifecycle = lifecycle


def get_global_lifecycle() -> Optional["SignalLifecycleManager"]:
    """Return the registered lifecycle manager (None when not running / feature disabled)."""
    return _global_lifecycle
