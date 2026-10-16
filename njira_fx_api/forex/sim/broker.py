"""
Simulated broker / trade lifecycle (trade.md §5, §13).

Orchestrates the ordered flow: validate → RR → size → risk gate → fill → monitor
SL/TP → close → costs → journal.  Deterministic given the seed (trade.md §10.4):
no ``datetime.now()`` — every timestamp comes from the event stream.

The broker is NOT a strategy: it takes already-decided signals and simulates a
real venue (spread/slippage/commission/swap) and a real, flawed trader (user
behaviour).  It never generates signals and never contains alpha.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .costs import BrokerCostModel, CostBreakdown
from .instruments import get_spec
from .journal import TradeJournal
from .risk import AccountState, RiskLimits, RiskService
from .rr import RRService, Regime, StrategyType
from .sizing import PositionSizingService
from .user_behavior import DeviationEvent, UserBehaviorService


@dataclass
class SimSignal:
    """A signal handed TO the engine (trade.md §1, §5 step 3–4)."""

    symbol: str
    direction: str                  # BUY / SELL
    entry: float
    stop_loss: float
    strength: float                 # 0..1
    strategy_type: StrategyType = StrategyType.BREAKOUT
    take_profit: Optional[float] = None   # structural target; RR engine may adjust
    signal_id: str = ""
    setup_label: str = ""                # raw analyzer tag for per-setup reporting
    atr_percentile: float = 0.5
    regime: Regime = Regime.WEAK_TREND
    exit_bars: int = 20             # how many bars to monitor for SL/TP


class SimulatedBroker:
    """Runs signals through the full account lifecycle and journals each one."""

    def __init__(
        self,
        *,
        initial_balance: float = 500.0,
        seed: int = 0,
        limits: Optional[RiskLimits] = None,
        costs: Optional[BrokerCostModel] = None,
        use_user_behavior: bool = True,
    ) -> None:
        self.rng = random.Random(seed)
        self.account = AccountState(initial_balance=initial_balance,
                                    balance=initial_balance,
                                    equity=initial_balance,
                                    peak_equity=initial_balance,
                                    day_start_equity=initial_balance)
        self.limits = limits or RiskLimits()
        self.costs = costs or BrokerCostModel()
        self.sizing = PositionSizingService()
        self.rr = RRService()
        self.risk = RiskService(self.limits)
        self.user = UserBehaviorService(self.rng)
        self.use_user_behavior = use_user_behavior
        self.journals: list[TradeJournal] = []
        self._win_streak = 0
        self._loss_streak = 0
        self._recent_r: list[float] = []

    # -- helpers ---------------------------------------------------------
    def _reject(self, sig: SimSignal, reason: str, detail: str = "") -> TradeJournal:
        j = TradeJournal(
            signal_id=sig.signal_id, symbol=sig.symbol,
            signal_type=sig.direction, strategy_type=sig.strategy_type.value,
            strength_score=sig.strength, rejected=True,
            rejection_reason=reason, rejection_detail=detail,
        )
        self.journals.append(j)
        return j

    def _historical_wr(self) -> Optional[float]:
        closed = [j for j in self.journals if not j.rejected]
        if not closed:
            return None
        return sum(1 for j in closed if j.net_pnl > 0) / len(closed)

    def _recent_expectancy_r(self) -> Optional[float]:
        if not self._recent_r:
            return None
        return sum(self._recent_r[-50:]) / len(self._recent_r[-50:])

    def structural_rr(self, sig: SimSignal) -> Optional[float]:
        risk = abs(sig.entry - sig.stop_loss)
        if risk <= 0 or not sig.take_profit:
            return None
        reward = abs(sig.take_profit - sig.entry)
        return reward / risk

    # -- main lifecycle ---------------------------------------------------
    def process_signal(
        self, sig: SimSignal, candles: list[dict]
    ) -> Optional[TradeJournal]:
        """
        Run one signal through the full lifecycle against ``candles`` (the bars
        AFTER the entry).  Returns the journal entry (taken or rejected).
        """
        sig.signal_id = sig.signal_id or f"sig_{uuid.uuid4().hex[:12]}"
        # §5 — advance the daily window from the event clock (no datetime.now()).
        if candles:
            self.account.roll_day(candles[0]["open_time"])

        # Step 4–5 — validate fields.
        if sig.direction not in ("BUY", "SELL"):
            return self._reject(sig, "invalid", "direction must be BUY/SELL")
        if sig.stop_loss == sig.entry or sig.entry <= 0 or sig.stop_loss <= 0:
            return self._reject(sig, "invalid", "missing/invalid SL or entry")
        if not candles:
            return self._reject(sig, "invalid", "no market data after entry")

        # --- user behaviour BEFORE order (step 3→19 of §5) ---
        deviations: list[DeviationEvent] = []
        if self.use_user_behavior:
            devs = self.user.on_signal(
                last_result=None,
                loss_streak=self._loss_streak,
                win_streak=self._win_streak,
            )
            deviations.extend(devs)
            if any(d.deviation_type == "skip_signal" for d in devs):
                return self._reject(sig, "user_skip", "trader skipped the signal")

        # --- step 10–15: RR chain ---
        closed = [j for j in self.journals if not j.rejected]
        rr = self.rr.calculate(
            strength=sig.strength,
            strategy_type=sig.strategy_type,
            atr_percentile=sig.atr_percentile,
            regime=sig.regime,
            recent_expectancy_r=self._recent_expectancy_r(),
            historical_wr=(sum(1 for j in closed if j.net_pnl > 0) / len(closed)
                           if closed else None),
            samples=len(closed),
            structural_rr=self.structural_rr(sig),
        )
        if not rr.accepted:
            return self._reject(sig, "rr_validation", rr.reason)

        # Final TP: if the signal carried a structural target, keep it; the RR
        # engine's final_rr is advisory for journaling (the book strategy owns
        # the actual level).  If no TP, derive one from final_rr.
        tp = sig.take_profit
        if tp is None:
            dist = abs(sig.entry - sig.stop_loss) * rr.final_rr
            tp = sig.entry + dist if sig.direction == "BUY" else sig.entry - dist

        # --- step 16–18: sizing ---
        sizing = self.sizing.size(
            symbol=sig.symbol, equity=self.account.equity,
            risk_amount=self.account.risk_amount,
            entry_price=sig.entry, stop_price=sig.stop_loss,
        )
        if not sizing.accepted:
            j = self._reject(sig, "sizing", sizing.reason)
            j.over_risk_reject_flag = sizing.over_risk_reject
            return j

        # --- step 6–9: risk gate ---
        decision = self.risk.pre_trade_check(
            self.account, symbol=sig.symbol, risk_amount=sizing.actual_risk
        )
        if not decision.allowed:
            return self._reject(sig, decision.code, decision.reason)

        # --- open the position (steps 19–23) ---
        balance_before = self.account.balance
        equity_before = self.account.equity
        lots = sizing.rounded_size
        entry_time = candles[0]["open_time"]
        self.account.open_positions[sig.symbol] = self.account.open_positions.get(
            sig.symbol, 0.0) + sizing.actual_risk
        self.account.trade_count += 1

        # --- steps 24–26: monitor SL/TP ---
        exit_price, exit_reason, exit_time, exit_i, mae, mfe = self._monitor(
            sig, candles, float(tp), lots
        )

        exit_candle = candles[min(exit_i, len(candles) - 1)]
        cost: CostBreakdown = self.costs.round_trip_cost(
            symbol=sig.symbol, lots=lots, mid_price=sig.entry,
            entry_time=entry_time, exit_time=exit_time,
            entry_candle=candles[0], exit_candle=exit_candle,
        )

        # --- step 27–28: PnL + account update ---
        spec = get_spec(sig.symbol)
        gross = self._pnl(sig.direction, lots, sig.entry, exit_price, spec)
        net = gross - cost.total
        self.account.balance += net
        self.account.equity = self.account.balance
        self.account.peak_equity = max(self.account.peak_equity, self.account.equity)
        # Release this trade's risk from the per-symbol exposure; drop the key
        # when the symbol is flat again.
        remaining = self.account.open_positions.get(sig.symbol, 0.0) - sizing.actual_risk
        if remaining > 1e-9:
            self.account.open_positions[sig.symbol] = remaining
        else:
            self.account.open_positions.pop(sig.symbol, None)

        r_mult = (net / sizing.actual_risk) if sizing.actual_risk > 0 else 0.0
        self._recent_r.append(r_mult)
        if net > 0:
            self._win_streak += 1; self._loss_streak = 0
        elif net < 0:
            self._loss_streak += 1; self._win_streak = 0

        # --- step 29: journal ---
        dur = (exit_time - entry_time).total_seconds() / 60.0 if (exit_time and entry_time) else 0.0
        j = TradeJournal(
            trade_id=f"t_{uuid.uuid4().hex[:12]}",
            signal_id=sig.signal_id, symbol=sig.symbol,
            signal_type=sig.direction, strategy_type=sig.strategy_type.value,
            setup_label=sig.setup_label, strength_score=sig.strength,
            base_rr=rr.base_rr, volatility_mult=rr.volatility_mult,
            regime_mult=rr.regime_mult, performance_mult=rr.performance_mult,
            structural_rr=rr.structural_rr, final_rr=rr.final_rr,
            break_even_wr=rr.break_even_wr, historical_wr=rr.historical_wr,
            margin_of_safety=rr.margin_of_safety, rr_adjustments=rr.rr_reductions,
            entry_time=entry_time.isoformat() if entry_time else None,
            entry_price=sig.entry, exit_time=exit_time.isoformat() if exit_time else None,
            exit_price=exit_price, stop_price=sig.stop_loss,
            stop_distance_pips=spec.pips(abs(sig.entry - sig.stop_loss)),
            tp_price=float(tp), tp_distance_pips=spec.pips(abs(float(tp) - sig.entry)),
            ideal_size=sizing.ideal_size, rounded_size=sizing.rounded_size,
            size_step_used=sizing.size_step_used,
            intended_risk_usd=sizing.intended_risk, actual_risk_usd=sizing.actual_risk,
            actual_risk_pct=sizing.actual_risk_pct,
            undersized_flag=sizing.undersized, over_risk_reject_flag=False,
            spread_cost=cost.spread_cost, slippage_cost=cost.slippage_cost,
            commission_usd=cost.commission_usd, swap_usd=cost.swap_usd,
            gross_pnl=gross, net_pnl=net, r_multiple=r_mult,
            balance_before=balance_before, balance_after=self.account.balance,
            equity_before=equity_before, equity_after=self.account.equity,
            next_risk_amount=self.account.risk_amount,
            mae_pips=spec.pips(mae), mfe_pips=spec.pips(mfe),
            mae_usd=spec.pip_value(lots) * spec.pips(mae) * -1,
            mfe_usd=spec.pip_value(lots) * spec.pips(mfe),
            exit_reason=exit_reason, duration_minutes=dur, duration_bars=exit_i + 1,
            deviations=[d.to_dict() for d in deviations],
        )
        self.journals.append(j)
        return j

    # -- internals --------------------------------------------------------
    @staticmethod
    def _pnl(direction: str, lots: float, entry: float, exit_price: float, spec) -> float:
        diff = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
        return diff * (spec.pip_value_per_lot / spec.pip_size) * lots

    def _monitor(self, sig: SimSignal, candles: list[dict], tp: float, lots: float):
        """
        Walk bars checking SL then TP (§14 — SL first on the same bar).  Tracks
        MAE/MFE.  Returns (exit_price, reason, exit_time, index, mae, mfe).
        """
        spec = get_spec(sig.symbol)
        is_buy = sig.direction == "BUY"
        mae = 0.0
        mfe = 0.0
        for i, c in enumerate(candles[: sig.exit_bars]):
            hi, lo = float(c["high"]), float(c["low"])
            if is_buy:
                mae = max(mae, sig.entry - lo)
                mfe = max(mfe, hi - sig.entry)
                # SL first (§14)
                if lo <= sig.stop_loss:
                    return sig.stop_loss, "SL", c["open_time"], i, mae, mfe
                if hi >= tp:
                    return tp, "TP", c["open_time"], i, mae, mfe
            else:
                mae = max(mae, hi - sig.entry)
                mfe = max(mfe, sig.entry - lo)
                if hi >= sig.stop_loss:
                    return sig.stop_loss, "SL", c["open_time"], i, mae, mfe
                if lo <= tp:
                    return tp, "TP", c["open_time"], i, mae, mfe
        last = candles[min(sig.exit_bars, len(candles)) - 1]
        return float(last["close"]), "eod", last["open_time"], min(sig.exit_bars, len(candles)) - 1, mae, mfe
