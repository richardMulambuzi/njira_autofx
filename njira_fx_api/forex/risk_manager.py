"""
RiskManager — pre-trade risk gate for the Pepperstone pipeline.

Every trade request passes through :meth:`RiskManager.is_trading_allowed`
before the execution engine is touched. Two independent gates are enforced:

1. **Spread filter** — reject when the current market spread exceeds
   ``RISK_MAX_SPREAD_PIPS`` (slippage / cost control).
2. **Daily drawdown** — reject when today's realized PnL (from ClickHouse
   ``trade_history`` via :func:`forex.clickhouse.schema.get_daily_pnl`) has
   breached ``RISK_MAX_DAILY_LOSS_PCT`` of the starting balance.

Configuration is environment-driven with sane defaults:

    RISK_STARTING_BALANCE      Default: "50000.0"
    RISK_MAX_DAILY_LOSS_PCT    Default: "0.02"   (2% of starting balance)
    RISK_MAX_SPREAD_PIPS       Default: "0.3"
    RISK_ACCOUNT_ID            Default: "5327660"

The class accepts explicit constructor values for testability; when omitted
they fall back to the environment (then the module defaults).
"""
from __future__ import annotations

import logging
import os

from .clickhouse.connection import get_clickhouse_client
from .clickhouse.schema import get_daily_pnl

logger = logging.getLogger("forex.risk")

# ---------------------------------------------------------------------------
# Environment-driven defaults (module level, resolved once at import)
# ---------------------------------------------------------------------------
RISK_STARTING_BALANCE = float(os.environ.get("RISK_STARTING_BALANCE", "50000.0"))
RISK_MAX_DAILY_LOSS_PCT = float(os.environ.get("RISK_MAX_DAILY_LOSS_PCT", "0.02"))
RISK_MAX_SPREAD_PIPS = float(os.environ.get("RISK_MAX_SPREAD_PIPS", "0.3"))
RISK_ACCOUNT_ID = int(os.environ.get("RISK_ACCOUNT_ID", "5327660"))


class RiskManager:
    """Evaluates whether a trade may be placed under current risk rules."""

    def __init__(
        self,
        starting_balance: float | None = None,
        max_daily_loss_pct: float | None = None,
        max_spread_pips: float | None = None,
        account_id: int | None = None,
    ) -> None:
        self.starting_balance = (
            starting_balance
            if starting_balance is not None
            else RISK_STARTING_BALANCE
        )
        self.max_daily_loss_pct = (
            max_daily_loss_pct
            if max_daily_loss_pct is not None
            else RISK_MAX_DAILY_LOSS_PCT
        )
        self.max_spread_pips = (
            max_spread_pips if max_spread_pips is not None else RISK_MAX_SPREAD_PIPS
        )
        self.account_id = account_id if account_id is not None else RISK_ACCOUNT_ID

    @property
    def max_daily_loss(self) -> float:
        """Absolute loss budget for one day (starting balance * loss pct)."""
        return self.starting_balance * self.max_daily_loss_pct

    def is_trading_allowed(self, current_spread_pips: float) -> tuple[bool, str]:
        """
        Return ``(allowed, reason)`` for a trade at ``current_spread_pips``.

        Both gates must pass:

        * ``current_spread_pips`` must be ``<= max_spread_pips``.
        * Today's realized PnL (``get_daily_pnl``) must not be at or beyond
          ``-max_daily_loss``.

        PnL reads are best-effort (schema helper returns ``0.0`` on failure),
        so a ClickHouse outage never blocks trading on a false drawdown.
        """
        if current_spread_pips > self.max_spread_pips:
            reason = (
                f"Spread {current_spread_pips:.2f} pips exceeds max "
                f"{self.max_spread_pips:.2f} pips"
            )
            logger.info("Risk gate blocked: %s", reason)
            return False, reason

        client = get_clickhouse_client()
        daily_pnl = get_daily_pnl(client, self.account_id)
        if daily_pnl <= -self.max_daily_loss:
            reason = (
                f"Daily loss limit reached: PnL {daily_pnl:.2f} is at/beyond "
                f"-{self.max_daily_loss:.2f} (account {self.account_id})"
            )
            logger.info("Risk gate blocked: %s", reason)
            return False, reason

        return True, "Trading allowed"