"""
Trade journal record (trade.md §6) and run summary statistics (trade.md §7).

Pure dataclasses + pure aggregation — no Django, no I/O.  The journal is designed
so a third party can reconstruct every trade and every cent from it alone
(trade.md §15).  The Django persistence model mirrors these fields 1:1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeJournal:
    """One closed (or rejected) trade — the full auditable record (trade.md §6)."""

    # Identity
    trade_id: str = ""
    signal_id: str = ""
    backtest_run_id: str = ""
    symbol: str = ""

    # Signal
    signal_type: str = ""            # BUY / SELL
    strategy_type: str = ""
    setup_label: str = ""            # raw analyzer tag (snr/engulfing/marriage/...)
    strength_score: float = 0.0

    # RR chain
    base_rr: float = 0.0
    volatility_mult: float = 0.0
    regime_mult: float = 0.0
    performance_mult: float = 0.0
    structural_rr: float = 0.0
    final_rr: float = 0.0
    break_even_wr: float = 0.0
    historical_wr: float = 0.0
    margin_of_safety: float = 0.0
    rr_adjustments: list[float] = field(default_factory=list)

    # Execution
    entry_time: Optional[str] = None
    entry_price: float = 0.0
    exit_time: Optional[str] = None
    exit_price: float = 0.0
    stop_price: float = 0.0
    stop_distance_pips: float = 0.0
    tp_price: float = 0.0
    tp_distance_pips: float = 0.0

    # Sizing
    ideal_size: float = 0.0
    rounded_size: float = 0.0
    size_step_used: float = 0.0
    intended_risk_usd: float = 0.0
    actual_risk_usd: float = 0.0
    actual_risk_pct: float = 0.0
    undersized_flag: bool = False
    over_risk_reject_flag: bool = False

    # Costs
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    commission_usd: float = 0.0
    swap_usd: float = 0.0

    # PnL
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    r_multiple: float = 0.0

    # Account
    balance_before: float = 0.0
    balance_after: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    next_risk_amount: float = 0.0

    # Excursions
    mae_pips: float = 0.0
    mfe_pips: float = 0.0
    mae_usd: float = 0.0
    mfe_usd: float = 0.0

    # Exit
    exit_reason: str = ""           # TP / SL / manual / margin_call / eod / user_*
    duration_minutes: float = 0.0
    duration_bars: int = 0

    # User deviations + rejection
    deviations: list[dict] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: str = ""
    rejection_detail: str = ""

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id, "signal_id": self.signal_id,
            "backtest_run_id": self.backtest_run_id, "symbol": self.symbol,
            "signal_type": self.signal_type, "strategy_type": self.strategy_type,
            "setup_label": self.setup_label, "strength_score": self.strength_score,
            "rr": {
                "base_rr": self.base_rr, "volatility_mult": self.volatility_mult,
                "regime_mult": self.regime_mult, "performance_mult": self.performance_mult,
                "structural_rr": self.structural_rr, "final_rr": self.final_rr,
                "break_even_wr": self.break_even_wr, "historical_wr": self.historical_wr,
                "margin_of_safety": self.margin_of_safety,
                "rr_adjustments": self.rr_adjustments,
            },
            "execution": {
                "entry_time": self.entry_time, "entry_price": self.entry_price,
                "exit_time": self.exit_time, "exit_price": self.exit_price,
                "stop_price": self.stop_price, "stop_distance_pips": self.stop_distance_pips,
                "tp_price": self.tp_price, "tp_distance_pips": self.tp_distance_pips,
            },
            "sizing": {
                "ideal_size": self.ideal_size, "rounded_size": self.rounded_size,
                "size_step_used": self.size_step_used,
                "intended_risk_usd": self.intended_risk_usd,
                "actual_risk_usd": self.actual_risk_usd,
                "actual_risk_pct": self.actual_risk_pct,
                "undersized_flag": self.undersized_flag,
                "over_risk_reject_flag": self.over_risk_reject_flag,
            },
            "costs": {
                "spread_cost": self.spread_cost, "slippage_cost": self.slippage_cost,
                "commission_usd": self.commission_usd, "swap_usd": self.swap_usd,
            },
            "pnl": {
                "gross_pnl": self.gross_pnl, "net_pnl": self.net_pnl,
                "r_multiple": self.r_multiple,
            },
            "account": {
                "balance_before": self.balance_before, "balance_after": self.balance_after,
                "equity_before": self.equity_before, "equity_after": self.equity_after,
                "next_risk_amount": self.next_risk_amount,
            },
            "excursions": {
                "mae_pips": self.mae_pips, "mfe_pips": self.mfe_pips,
                "mae_usd": self.mae_usd, "mfe_usd": self.mfe_usd,
            },
            "exit": {
                "exit_reason": self.exit_reason,
                "duration_minutes": self.duration_minutes, "duration_bars": self.duration_bars,
            },
            "deviations": self.deviations,
            "rejection": {
                "rejected": self.rejected, "rejection_reason": self.rejection_reason,
                "rejection_detail": self.rejection_detail,
            },
        }


@dataclass
class RunSummary:
    """Run-level statistics (trade.md §7)."""

    initial_balance: float = 0.0
    final_balance: float = 0.0
    net_profit_usd: float = 0.0
    return_pct: float = 0.0
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0

    total_signals_received: int = 0
    signals_taken: int = 0
    signals_rejected: int = 0
    rejection_breakdown: dict[str, int] = field(default_factory=dict)

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    avg_planned_rr: float = 0.0
    avg_realized_rr: float = 0.0
    profit_factor: float = 0.0
    expectancy_usd: float = 0.0
    expectancy_r: float = 0.0

    # Honesty / sanity (§15 — a stranger must be able to audit the run).
    trading_halted: bool = False
    halt_reason: str = ""
    growth_multiple: float = 0.0          # final_balance / initial_balance
    credible: bool = True                # False if growth looks like compounding artifact

    longest_win_streak: int = 0
    longest_loss_streak: int = 0

    performance_by_strategy_type: dict[str, dict] = field(default_factory=dict)
    performance_by_strength_bucket: dict[str, dict] = field(default_factory=dict)
    performance_by_session: dict[str, dict] = field(default_factory=dict)

    deviations_count_by_type: dict[str, int] = field(default_factory=dict)
    total_pnl_impact_from_deviations_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "account": {
                "initial_balance": self.initial_balance, "final_balance": self.final_balance,
                "net_profit_usd": self.net_profit_usd, "return_pct": self.return_pct,
                "peak_equity": self.peak_equity,
                "max_drawdown_pct": self.max_drawdown_pct,
                "max_drawdown_usd": self.max_drawdown_usd,
            },
            "signals": {
                "total_signals_received": self.total_signals_received,
                "signals_taken": self.signals_taken,
                "signals_rejected": self.signals_rejected,
                "rejection_breakdown": self.rejection_breakdown,
            },
            "trades": {
                "total_trades": self.total_trades, "wins": self.wins, "losses": self.losses,
                "breakeven": self.breakeven, "win_rate": self.win_rate,
                "avg_win_usd": self.avg_win_usd, "avg_loss_usd": self.avg_loss_usd,
                "avg_planned_rr": self.avg_planned_rr, "avg_realized_rr": self.avg_realized_rr,
                "profit_factor": self.profit_factor,
                "expectancy_usd": self.expectancy_usd, "expectancy_r": self.expectancy_r,
            },
            "sanity": {
                "trading_halted": self.trading_halted, "halt_reason": self.halt_reason,
                "growth_multiple": round(self.growth_multiple, 2), "credible": self.credible,
            },
            "streaks": {
                "longest_win_streak": self.longest_win_streak,
                "longest_loss_streak": self.longest_loss_streak,
            },
            "buckets": {
                "performance_by_strategy_type": self.performance_by_strategy_type,
                "performance_by_strength_bucket": self.performance_by_strength_bucket,
                "performance_by_session": self.performance_by_session,
            },
            "user_behavior": {
                "deviations_count_by_type": self.deviations_count_by_type,
                "total_pnl_impact_from_deviations_usd": self.total_pnl_impact_from_deviations_usd,
            },
        }


def summarize(
    journals: list[TradeJournal],
    initial_balance: float,
    account: Optional["object"] = None,
) -> RunSummary:
    """
    Aggregate closed trades into a :class:`RunSummary` (trade.md §7).

    ``account`` (optional) is the live :class:`forex.sim.risk.AccountState`; when
    given, the summary reports the TRUE final equity and any latched risk halt
    instead of just the last taken trade, and flags implausible compounding growth
    (1%-risk compounding across many trades can print fantasy PnL).
    """
    s = RunSummary(initial_balance=initial_balance, final_balance=initial_balance)
    taken = [j for j in journals if not j.rejected]
    rejected = [j for j in journals if j.rejected]
    s.total_signals_received = len(journals)
    s.signals_taken = len(taken)
    s.signals_rejected = len(rejected)
    s.rejection_breakdown = {}
    for j in rejected:
        key = j.rejection_reason or "unknown"
        s.rejection_breakdown[key] = s.rejection_breakdown.get(key, 0) + 1

    if not taken:
        return s

    wins = [j for j in taken if j.net_pnl > 0]
    losses = [j for j in taken if j.net_pnl < 0]
    be = [j for j in taken if j.net_pnl == 0]
    s.total_trades = len(taken)
    s.wins, s.losses, s.breakeven = len(wins), len(losses), len(be)
    s.win_rate = len(wins) / len(taken) * 100.0

    gross_win = sum(j.net_pnl for j in wins)
    gross_loss = abs(sum(j.net_pnl for j in losses))
    s.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    s.avg_win_usd = gross_win / len(wins) if wins else 0.0
    s.avg_loss_usd = gross_loss / len(losses) if losses else 0.0
    s.expectancy_usd = sum(j.net_pnl for j in taken) / len(taken)
    s.avg_realized_rr = sum(j.r_multiple for j in taken) / len(taken)
    s.avg_planned_rr = sum(j.final_rr for j in taken) / len(taken)
    s.expectancy_r = s.avg_realized_rr

    # Balance / equity / drawdown.  Prefer the LIVE account (true final equity +
    # any halt) when provided; fall back to the last taken trade otherwise.
    if account is not None:
        s.final_balance = float(getattr(account, "equity", initial_balance))
        s.trading_halted = bool(getattr(account, "trading_halted", False))
        s.halt_reason = str(getattr(account, "halt_reason", ""))
    else:
        s.final_balance = taken[-1].balance_after
    s.net_profit_usd = s.final_balance - initial_balance
    s.return_pct = (s.net_profit_usd / initial_balance * 100.0) if initial_balance else 0.0
    s.growth_multiple = (s.final_balance / initial_balance) if initial_balance else 0.0
    # 1%-risk compounding with a sustained high profit factor across many trades
    # produces numbers no real account sustains.  Flag the implausible ones.
    s.credible = s.growth_multiple < 50.0
    s.peak_equity = max(j.equity_after for j in taken)
    peak = initial_balance
    max_dd = 0.0
    for j in taken:
        peak = max(peak, j.equity_after)
        dd = peak - j.equity_after
        max_dd = max(max_dd, dd)
    s.max_drawdown_usd = max_dd
    s.max_drawdown_pct = (max_dd / s.peak_equity * 100.0) if s.peak_equity else 0.0

    # Streaks
    win_streak = loss_streak = 0
    for j in taken:
        if j.net_pnl > 0:
            win_streak += 1
            loss_streak = 0
        elif j.net_pnl < 0:
            loss_streak += 1
            win_streak = 0
        s.longest_win_streak = max(s.longest_win_streak, win_streak)
        s.longest_loss_streak = max(s.longest_loss_streak, loss_streak)

    # Buckets + deviations
    for j in taken:
        s.performance_by_strategy_type.setdefault(
            j.strategy_type, {"n": 0, "pnl": 0.0})
        b = s.performance_by_strategy_type[j.strategy_type]
        b["n"] += 1; b["pnl"] += j.net_pnl

        bucket = f"{int(j.strength_score * 5) / 5:.1f}"
        s.performance_by_strength_bucket.setdefault(bucket, {"n": 0, "pnl": 0.0})
        sb = s.performance_by_strength_bucket[bucket]
        sb["n"] += 1; sb["pnl"] += j.net_pnl

        for d in j.deviations:
            dt = d.get("deviation_type", "unknown")
            s.deviations_count_by_type[dt] = s.deviations_count_by_type.get(dt, 0) + 1
            s.total_pnl_impact_from_deviations_usd += d.get("pnl_impact_usd", 0.0)

    for b in s.performance_by_strategy_type.values():
        b["pnl"] = round(b["pnl"], 2)
    for b in s.performance_by_strength_bucket.values():
        b["pnl"] = round(b["pnl"], 2)
    return s
