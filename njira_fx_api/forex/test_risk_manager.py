"""
QA Gate 2 — RiskManager unit tests (spread gate + daily drawdown gate).

Covers forex/risk_manager.py:

* Spread filter — reject when current spread exceeds RISK_MAX_SPREAD_PIPS.
* Daily drawdown — reject when today's realized PnL breaches the loss budget.
* Best-effort PnL reads — a ClickHouse failure (get_daily_pnl -> 0.0) must
  never block trading on a false drawdown.
* Constructor defaults / env fallbacks / max_daily_loss math.

All ClickHouse access is mocked — no live server is ever contacted.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from forex.risk_manager import RiskManager


class RiskManagerSpreadGateTests(SimpleTestCase):
    """Gate 1 — spread must be <= max_spread_pips."""

    def setUp(self):
        self.manager = RiskManager(
            starting_balance=50000.0,
            max_daily_loss_pct=0.02,
            max_spread_pips=0.3,
            account_id=5327660,
        )

    def test_spread_within_limit_allows_trade(self):
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
        ):
            allowed, reason = self.manager.is_trading_allowed(0.1)
        self.assertTrue(allowed)
        self.assertEqual(reason, "Trading allowed")

    def test_spread_at_exact_limit_allows_trade(self):
        # Boundary: <= max is allowed (inclusive).
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
        ):
            allowed, _ = self.manager.is_trading_allowed(0.3)
        self.assertTrue(allowed)

    def test_spread_above_limit_blocks_trade(self):
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
        ):
            allowed, reason = self.manager.is_trading_allowed(0.31)
        self.assertFalse(allowed)
        self.assertIn("Spread 0.31 pips exceeds max 0.30 pips", reason)

    def test_spread_gate_short_circuits_before_pnl_read(self):
        # When the spread gate fails, get_daily_pnl must NOT be called.
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client") as m_client,
            mock.patch("forex.risk_manager.get_daily_pnl") as m_pnl,
        ):
            allowed, _ = self.manager.is_trading_allowed(5.0)
        self.assertFalse(allowed)
        m_client.assert_not_called()
        m_pnl.assert_not_called()


class RiskManagerDrawdownGateTests(SimpleTestCase):
    """Gate 2 — daily realized PnL must stay above -max_daily_loss."""

    def setUp(self):
        self.manager = RiskManager(
            starting_balance=50000.0,
            max_daily_loss_pct=0.02,  # budget = 1000.0
            max_spread_pips=0.3,
            account_id=5327660,
        )

    def test_pnl_at_loss_budget_blocks_trade(self):
        # PnL == -1000.0 is at the budget -> blocked (>= comparison).
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=-1000.0),
        ):
            allowed, reason = self.manager.is_trading_allowed(0.1)
        self.assertFalse(allowed)
        self.assertIn("Daily loss limit reached", reason)
        self.assertIn("account 5327660", reason)

    def test_pnl_beyond_loss_budget_blocks_trade(self):
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=-1500.0),
        ):
            allowed, _ = self.manager.is_trading_allowed(0.1)
        self.assertFalse(allowed)

    def test_pnl_within_budget_allows_trade(self):
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=-999.99),
        ):
            allowed, _ = self.manager.is_trading_allowed(0.1)
        self.assertTrue(allowed)

    def test_positive_pnl_allows_trade(self):
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=250.0),
        ):
            allowed, _ = self.manager.is_trading_allowed(0.1)
        self.assertTrue(allowed)

    def test_pnl_read_failure_never_blocks_trading(self):
        # get_daily_pnl returns 0.0 on ClickHouse failure (best-effort) —
        # an outage must not freeze the firm out of the market.
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client"),
            mock.patch("forex.risk_manager.get_daily_pnl", return_value=0.0),
        ):
            allowed, reason = self.manager.is_trading_allowed(0.1)
        self.assertTrue(allowed)
        self.assertEqual(reason, "Trading allowed")

    def test_account_id_forwarded_to_pnl_query(self):
        with (
            mock.patch("forex.risk_manager.get_clickhouse_client") as m_client,
            mock.patch("forex.risk_manager.get_daily_pnl") as m_pnl,
        ):
            m_pnl.return_value = 0.0
            self.manager.is_trading_allowed(0.1)
        m_pnl.assert_called_once_with(m_client.return_value, 5327660)


class RiskManagerConfigTests(SimpleTestCase):
    """Constructor defaults, env fallbacks and budget math."""

    def test_max_daily_loss_is_balance_times_pct(self):
        manager = RiskManager(
            starting_balance=50000.0, max_daily_loss_pct=0.02
        )
        self.assertEqual(manager.max_daily_loss, 1000.0)

    def test_constructor_defaults_fall_back_to_module_constants(self):
        # Module constants are resolved from the environment at import time;
        # the constructor falls back to them when no explicit value is passed.
        with (
            mock.patch("forex.risk_manager.RISK_STARTING_BALANCE", 100000.0),
            mock.patch("forex.risk_manager.RISK_MAX_DAILY_LOSS_PCT", 0.01),
            mock.patch("forex.risk_manager.RISK_MAX_SPREAD_PIPS", 0.5),
            mock.patch("forex.risk_manager.RISK_ACCOUNT_ID", 42),
        ):
            manager = RiskManager()
        self.assertEqual(manager.starting_balance, 100000.0)
        self.assertEqual(manager.max_daily_loss_pct, 0.01)
        self.assertEqual(manager.max_spread_pips, 0.5)
        self.assertEqual(manager.account_id, 42)
        self.assertEqual(manager.max_daily_loss, 1000.0)

    def test_explicit_constructor_values_win_over_module_defaults(self):
        manager = RiskManager(starting_balance=20000.0, max_daily_loss_pct=0.05)
        self.assertEqual(manager.starting_balance, 20000.0)
        self.assertEqual(manager.max_daily_loss_pct, 0.05)