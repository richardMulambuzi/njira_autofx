"""
Pre-trade risk limits (trade.md §8) and account state (trade.md §1).

Pure service — deterministic, no I/O.  ``RiskLimits`` is the config dataclass,
``AccountState`` is the live account the broker mutates, and ``RiskService`` runs
the ordered pre-trade checks from trade.md §5 (steps 6–9, 17–18).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# trade.md §8 — the $500 account limit table.
@dataclass
class RiskLimits:
    max_risk_per_trade_pct: float = 0.0125   # 1.25% — rounding tolerance (§8)
    max_risk_per_symbol_pct: float = 0.02    # 2.0%  — anti-concentration
    max_portfolio_risk_pct: float = 0.04      # 4.0%  — 4 trades at 1%
    max_open_positions: int = 3              # small-account margin
    max_daily_loss_pct: float = 0.03         # 3% ($15) — stop for the day
    max_drawdown_pct: float = 0.20           # 20% ($100) — pause trading
    stop_trading_at_drawdown_pct: float = 0.20
    margin_call_level: float = 1.00          # 100%
    stop_out_level: float = 0.50             # 50%
    negative_balance_protection: bool = True


@dataclass
class AccountState:
    """Live account (trade.md §1, §2.3 — risk is 1% of CURRENT equity)."""

    initial_balance: float = 500.0
    balance: float = 500.0
    equity: float = 500.0
    peak_equity: float = 500.0
    used_margin: float = 0.0
    open_positions: dict[str, float] = field(default_factory=dict)  # symbol -> risk_usd
    day_start_equity: float = 500.0
    trade_count: int = 0
    # Latched risk halt (trade.md §8 — "Pause trading / Review").  Once the
    # drawdown limit trips, trading stops until a human reviews it; it is NOT a
    # per-signal filter that can silently resume.
    trading_halted: bool = False
    halt_reason: str = ""
    _current_day: Optional[datetime] = field(default=None, repr=False)

    def roll_day(self, event_time: datetime) -> None:
        """
        Advance the daily window when the UTC date changes (§8 daily-loss limit).

        Without this the "3% daily loss" guard is really an all-time guard that
        latches off permanently the first time equity is 3% below the day's
        opening value.  Called by the broker on every event with its timestamp.
        """
        day = event_time.date()
        if self._current_day is None:
            self._current_day = event_time
            self.day_start_equity = self.equity
            return
        if event_time.date() != self._current_day.date():
            self._current_day = event_time
            self.day_start_equity = self.equity  # new day opens at current equity

    def halt(self, reason: str) -> None:
        """Latch a risk halt (drawdown / review threshold)."""
        self.trading_halted = True
        self.halt_reason = reason

    def review_reset(self) -> None:
        """Human review clears the halt (trade.md §8 — 'Stop trading / Review')."""
        self.trading_halted = False
        self.halt_reason = ""

    @property
    def risk_amount(self) -> float:
        """1% of current equity, recalculated after every close (§1 rule)."""
        return 0.01 * self.equity

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    @property
    def portfolio_risk_pct(self) -> float:
        if self.equity <= 0:
            return float("inf")
        return sum(self.open_positions.values()) / self.equity

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    def symbol_risk_pct(self, symbol: str) -> float:
        if self.equity <= 0:
            return float("inf")
        return self.open_positions.get(symbol, 0.0) / self.equity


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    code: str = ""


class RiskService:
    """Ordered pre-trade checks (trade.md §5 steps 6–9, 17–18)."""

    def __init__(self, limits: Optional[RiskLimits] = None) -> None:
        self.limits = limits or RiskLimits()

    def pre_trade_check(
        self,
        account: AccountState,
        *,
        symbol: str,
        risk_amount: float,
    ) -> RiskDecision:
        """
        Gate a new trade.  ``risk_amount`` is the USD this trade will put at risk
        (already sized).  Checks, in order: drawdown pause, daily loss, open
        count, per-symbol concentration, portfolio risk, per-trade over-risk.
        """
        lim = self.limits

        # Latched risk halt (§8 — pause trading / review).  Once tripped it stays
        # tripped until a human reviews the account; it does not auto-resume.
        if account.trading_halted:
            return RiskDecision(
                False,
                f"trading halted: {account.halt_reason or 'risk limit'}",
                "halted",
            )

        # Drawdown pause (§8 — stop trading at 20%).  Tripping it LATCHES a halt.
        if account.drawdown_pct >= lim.stop_trading_at_drawdown_pct:
            reason = (
                f"drawdown {account.drawdown_pct:.1%} reached pause threshold "
                f"{lim.stop_trading_at_drawdown_pct:.0%}"
            )
            account.halt(reason)
            return RiskDecision(False, reason, "drawdown")

        # Daily loss limit (§8 — 3%).
        day_pnl = account.equity - account.day_start_equity
        if day_pnl < 0 and (-day_pnl) / max(account.day_start_equity, 1e-9) >= lim.max_daily_loss_pct:
            return RiskDecision(
                False,
                f"daily loss {(-day_pnl) / account.day_start_equity:.1%} reached "
                f"{lim.max_daily_loss_pct:.0%} limit",
                "daily_loss",
            )

        # Open position count (§8).
        if account.open_count >= lim.max_open_positions:
            return RiskDecision(
                False, f"already {account.open_count} open positions "
                f"(max {lim.max_open_positions})", "exposure",
            )

        # Per-symbol concentration (§8 — 2%).
        if account.symbol_risk_pct(symbol) + (risk_amount / account.equity if account.equity > 0 else 1) > lim.max_risk_per_symbol_pct:
            return RiskDecision(
                False, f"per-symbol risk would exceed {lim.max_risk_per_symbol_pct:.0%}",
                "correlation",
            )

        # Portfolio risk (§8 — 4%).
        if account.portfolio_risk_pct + (risk_amount / account.equity if account.equity > 0 else 1) > lim.max_portfolio_risk_pct:
            return RiskDecision(
                False, f"portfolio risk would exceed {lim.max_portfolio_risk_pct:.0%}",
                "exposure",
            )

        # Per-trade over-risk (§8 — 1.25% post-rounding).
        if account.equity > 0 and risk_amount / account.equity > lim.max_risk_per_trade_pct:
            return RiskDecision(
                False, f"per-trade risk {risk_amount / account.equity:.2%} exceeds "
                f"{lim.max_risk_per_trade_pct:.2%}", "risk_limit",
            )

        return RiskDecision(True)
