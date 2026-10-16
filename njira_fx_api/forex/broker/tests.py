"""
Unit & integration tests for the broker/provider abstraction layer.

Covers, in order:

* ``forex.broker.models``     — frozen value-object dataclasses and enums.
* ``forex.broker.errors``     — :class:`BrokerError` wire format and codes.
* ``forex.broker.config``     — :class:`TradingConfig` env resolution,
  credential validation, safety checks and secret-free serialization.
* ``forex.broker.factory``    — provider registry, cached singleton, switching.
* ``forex.broker.providers.mock`` — behavioral tests of the in-process
  simulator (balances, ticks, candles, orders, positions, trades, fail modes).
* ``forex.broker.providers.pepperstone`` — graceful AUTHENTICATION_ERROR path
  when ``PEPPERSTONE_*`` env vars are unset (no real subprocess spawned).
* ``forex.broker.health``     — :class:`ProviderHealthMonitor` tracking,
  heuristic status degradation and ISO-8601 serialization.

All tests are pure-unit: they use :class:`django.test.SimpleTestCase` and never
touch the database, live broker connections, the cTrader SDK reactor, or
ClickHouse. External transports are intercepted with ``unittest.mock``:
Pepperstone's ``subprocess.run`` is patched — never a real worker process.

Environment handling: ``setUp`` strips every trading-related env var so each
test starts from a deterministic baseline; ``tearDown`` restores the original
values. ``TradingProviderFactory.reset()`` runs in both so the cached active
provider never leaks between tests.

Run: ``python manage.py test forex.broker``
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from forex.broker.base import CANDLE_TIMEFRAMES, TradingProvider
from forex.broker.config import (
    PEPPERSTONE_REQUIRED_ENV,
    ProviderConfig,
    TradingConfig,
)
from forex.broker.errors import BrokerError, BrokerErrorCode
from forex.broker.factory import TradingProviderFactory
from forex.broker.health import ProviderHealth, ProviderHealthMonitor
from forex.broker.models import (
    AccountStatus,
    BrokerAccount,
    BrokerBalance,
    BrokerOrder,
    BrokerPosition,
    BrokerSymbol,
    BrokerTrade,
    ConnectionStatus,
    MarketCandle,
    MarketTick,
    OrderSide,
    OrderStatus,
    PositionStatus,
    ProviderName,
    TradingMode,
)
from forex.broker.providers.mock import MockProvider
from forex.broker.providers.pepperstone import PepperstoneProvider

# ---------------------------------------------------------------------------
# Test environment helpers
# ---------------------------------------------------------------------------

# Every env var the broker layer reads. setUp removes them for a deterministic
# baseline; tearDown restores whatever was present before the test.
_ENV_KEYS = (
    "TRADING_PROVIDER",
    "TRADING_MODE",
    "PEPPERSTONE_CLIENT_ID",
    "PEPPERSTONE_CLIENT_SECRET",
    "PEPPERSTONE_ACCESS_TOKEN",
    "PEPPERSTONE_REFRESH_TOKEN",
    "PEPPERSTONE_ACCOUNT_ID",
    "PEPPERSTONE_ENV_PATH",
    "PEPPERSTONE_TOKEN_URL",
    "MOCK_FAIL_MODE",
)

# Explicit "credentials unset" environment for the Pepperstone provider tests.
_EMPTY_PEPPERSTONE_ENV = {
    "PEPPERSTONE_CLIENT_ID": "",
    "PEPPERSTONE_CLIENT_SECRET": "",
    "PEPPERSTONE_ACCESS_TOKEN": "",
    "PEPPERSTONE_REFRESH_TOKEN": "",
}


class _BrokerTestCase(SimpleTestCase):
    """Base test case: deterministic env baseline + fresh factory state."""

    def setUp(self) -> None:
        super().setUp()
        self._saved_env = {
            key: os.environ[key] for key in _ENV_KEYS if key in os.environ
        }
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        # Sandbox any token persist: refresh_access_token(persist=True)
        # resolves PEPPERSTONE_ENV_PATH, so point it at a throwaway dir —
        # a test run can never overwrite the real project .env.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        os.environ["PEPPERSTONE_ENV_PATH"] = str(
            Path(self._tmpdir.name) / "test.env"
        )
        TradingProviderFactory.reset()

    def tearDown(self) -> None:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(self._saved_env)
        TradingProviderFactory.reset()
        super().tearDown()


class _StubProvider(TradingProvider):
    """Minimal TradingProvider subclass used to test factory registration."""

    name = "stub"
    display_name = "Stub Provider"

    def get_account(self) -> BrokerAccount:
        raise NotImplementedError

    def get_balance(self) -> BrokerBalance:
        raise NotImplementedError

    def get_positions(self) -> list[BrokerPosition]:
        raise NotImplementedError

    def get_orders(self) -> list[BrokerOrder]:
        raise NotImplementedError

    def place_order(self, *args, **kwargs) -> BrokerOrder:
        raise NotImplementedError

    def cancel_order(self, external_order_id: str) -> BrokerOrder:
        raise NotImplementedError

    def modify_order(self, *args, **kwargs) -> BrokerOrder:
        raise NotImplementedError

    def get_market_data(self, symbol: str) -> MarketTick:
        raise NotImplementedError

    def get_ticks(self, symbol: str, limit: int = 100) -> list[MarketTick]:
        raise NotImplementedError

    def get_candles(
        self, symbol: str, timeframe: str, limit: int = 100
    ) -> list[MarketCandle]:
        raise NotImplementedError

    def get_trade_history(self, limit: int = 50) -> list[BrokerTrade]:
        raise NotImplementedError

    def get_symbols(self) -> list[BrokerSymbol]:
        raise NotImplementedError

    def subscribe_to_market_data(self, symbols: list[str], callback) -> str:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def get_connection_status(self) -> ConnectionStatus:
        raise NotImplementedError

    def health(self) -> ProviderHealth:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Normalized models
# ---------------------------------------------------------------------------
class ModelsTests(_BrokerTestCase):
    """Frozen value-object dataclasses and their enums."""

    NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    def test_enums_have_expected_values(self) -> None:
        self.assertEqual(ProviderName.PEPPERSTONE.value, "pepperstone")
        self.assertEqual(ProviderName.MOCK.value, "mock")
        # Exact membership: the provider registry is closed — no retired
        # provider may reappear without an explicit contract change.
        self.assertEqual(
            [name.value for name in ProviderName], ["pepperstone", "mock"]
        )

        self.assertEqual(TradingMode.DEMO.value, "demo")
        self.assertEqual(TradingMode.PAPER.value, "paper")
        self.assertEqual(TradingMode.LIVE.value, "live")

        self.assertEqual(OrderSide.BUY.value, "buy")
        self.assertEqual(OrderSide.SELL.value, "sell")

        self.assertEqual(
            [status.value for status in OrderStatus],
            ["pending", "accepted", "filled", "rejected", "cancelled", "expired"],
        )
        self.assertEqual(
            [status.value for status in PositionStatus], ["open", "closed"]
        )
        self.assertEqual(
            [status.value for status in ConnectionStatus],
            [
                "connected",
                "disconnected",
                "connecting",
                "authenticating",
                "authentication_failed",
                "error",
            ],
        )
        self.assertEqual(
            [status.value for status in AccountStatus],
            ["active", "unavailable", "not_authenticated", "suspended"],
        )

    def test_broker_account_fields_and_frozen(self) -> None:
        account = BrokerAccount(
            provider="mock",
            account_id="acc-1",
            account_type="demo",
            currency="USD",
            status=AccountStatus.ACTIVE,
            name="Demo Account",
            leverage=100.0,
        )
        self.assertEqual(account.provider, "mock")
        self.assertEqual(account.account_id, "acc-1")
        self.assertEqual(account.account_type, "demo")
        self.assertEqual(account.currency, "USD")
        self.assertIs(account.status, AccountStatus.ACTIVE)
        self.assertEqual(account.name, "Demo Account")
        self.assertEqual(account.leverage, 100.0)
        with self.assertRaises(FrozenInstanceError):
            account.account_id = "changed"

    def test_broker_balance_fields_and_frozen(self) -> None:
        balance = BrokerBalance(
            provider="mock",
            account_id="acc-1",
            balance=50000.0,
            equity=50100.0,
            margin=100.0,
            free_margin=49900.0,
            currency="USD",
            timestamp=self.NOW,
        )
        self.assertEqual(balance.balance, 50000.0)
        self.assertEqual(balance.equity, 50100.0)
        self.assertEqual(balance.margin, 100.0)
        self.assertEqual(balance.free_margin, 49900.0)
        self.assertEqual(balance.timestamp, self.NOW)
        with self.assertRaises(FrozenInstanceError):
            balance.balance = 0.0

    def test_broker_position_fields_and_frozen(self) -> None:
        position = BrokerPosition(
            provider="mock",
            external_position_id="pos-1",
            account_id="acc-1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            quantity=1000.0,
            entry_price=1.0850,
            current_price=1.0860,
            status=PositionStatus.OPEN,
            opened_at=self.NOW,
            stop_loss=1.0800,
            take_profit=1.0900,
        )
        self.assertEqual(position.external_position_id, "pos-1")
        self.assertIs(position.side, OrderSide.BUY)
        self.assertEqual(position.quantity, 1000.0)
        self.assertEqual(position.entry_price, 1.0850)
        self.assertEqual(position.stop_loss, 1.0800)
        self.assertEqual(position.take_profit, 1.0900)
        self.assertIsNone(position.closed_at)
        self.assertEqual(position.realized_pnl, 0.0)
        with self.assertRaises(FrozenInstanceError):
            position.status = PositionStatus.CLOSED

    def test_broker_order_fields_and_frozen(self) -> None:
        order = BrokerOrder(
            provider="mock",
            external_order_id="order-1",
            account_id="acc-1",
            symbol="EURUSD",
            side=OrderSide.SELL,
            order_type="limit",
            quantity=100.0,
            status=OrderStatus.PENDING,
            created_at=self.NOW,
            updated_at=self.NOW,
            filled_quantity=0.0,
            price=1.1000,
        )
        self.assertEqual(order.external_order_id, "order-1")
        self.assertIs(order.side, OrderSide.SELL)
        self.assertEqual(order.order_type, "limit")
        self.assertIs(order.status, OrderStatus.PENDING)
        self.assertEqual(order.price, 1.1000)
        self.assertEqual(order.filled_quantity, 0.0)
        with self.assertRaises(FrozenInstanceError):
            order.status = OrderStatus.CANCELLED

    def test_broker_trade_fields_and_frozen(self) -> None:
        trade = BrokerTrade(
            provider="mock",
            external_trade_id="trade-1",
            account_id="acc-1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            quantity=1000.0,
            entry_price=1.0850,
            status="open",
            opened_at=self.NOW,
            exit_price=1.0890,
            realized_pnl=4.0,
        )
        self.assertEqual(trade.external_trade_id, "trade-1")
        self.assertEqual(trade.entry_price, 1.0850)
        self.assertEqual(trade.exit_price, 1.0890)
        self.assertEqual(trade.realized_pnl, 4.0)
        self.assertIsNone(trade.closed_at)
        with self.assertRaises(FrozenInstanceError):
            trade.quantity = 0.0

    def test_broker_symbol_fields_and_frozen(self) -> None:
        symbol = BrokerSymbol(
            provider="mock",
            symbol="EURUSD",
            display_name="EUR/USD",
            digits=5,
            market="forex",
            min_volume=0.01,
            max_volume=100.0,
            tick_size=0.00001,
        )
        self.assertEqual(symbol.symbol, "EURUSD")
        self.assertEqual(symbol.display_name, "EUR/USD")
        self.assertEqual(symbol.digits, 5)
        self.assertEqual(symbol.market, "forex")
        self.assertEqual(symbol.min_volume, 0.01)
        self.assertEqual(symbol.max_volume, 100.0)
        with self.assertRaises(FrozenInstanceError):
            symbol.symbol = "GBPUSD"

    def test_market_tick_fields_and_frozen(self) -> None:
        tick = MarketTick(
            provider="mock",
            symbol="EURUSD",
            bid=1.0850,
            ask=1.0851,
            last=1.0850,
            timestamp=self.NOW,
            volume=12.5,
        )
        self.assertEqual(tick.bid, 1.0850)
        self.assertEqual(tick.ask, 1.0851)
        self.assertEqual(tick.last, 1.0850)
        self.assertEqual(tick.volume, 12.5)
        with self.assertRaises(FrozenInstanceError):
            tick.bid = 0.0

    def test_market_candle_fields_and_frozen(self) -> None:
        candle = MarketCandle(
            provider="mock",
            symbol="EURUSD",
            timeframe="1H",
            open=1.0850,
            high=1.0860,
            low=1.0840,
            close=1.0855,
            timestamp=self.NOW,
            volume=500.0,
        )
        self.assertEqual(candle.timeframe, "1H")
        self.assertEqual(candle.open, 1.0850)
        self.assertEqual(candle.high, 1.0860)
        self.assertEqual(candle.low, 1.0840)
        self.assertEqual(candle.close, 1.0855)
        with self.assertRaises(FrozenInstanceError):
            candle.close = 0.0

    def test_equality_and_hash(self) -> None:
        first = BrokerAccount(
            provider="mock",
            account_id="acc-1",
            account_type="demo",
            currency="USD",
            status=AccountStatus.ACTIVE,
        )
        second = BrokerAccount(
            provider="mock",
            account_id="acc-1",
            account_type="demo",
            currency="USD",
            status=AccountStatus.ACTIVE,
        )
        other = BrokerAccount(
            provider="pepperstone",
            account_id="acc-1",
            account_type="demo",
            currency="USD",
            status=AccountStatus.ACTIVE,
        )
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertNotEqual(first, other)


# ---------------------------------------------------------------------------
# 2. BrokerError
# ---------------------------------------------------------------------------
class BrokerErrorTests(_BrokerTestCase):
    """Normalized error type: wire format, string form and code enum."""

    def test_to_dict_shape(self) -> None:
        error = BrokerError(
            BrokerErrorCode.AUTHENTICATION_ERROR,
            "Pepperstone API token not configured",
            provider="pepperstone",
            details={"missing": ["PEPPERSTONE_ACCESS_TOKEN"]},
        )
        self.assertEqual(
            error.to_dict(),
            {
                "error": "authentication_error",
                "message": "Pepperstone API token not configured",
                "provider": "pepperstone",
                "details": {"missing": ["PEPPERSTONE_ACCESS_TOKEN"]},
            },
        )

    def test_str_format(self) -> None:
        error = BrokerError(BrokerErrorCode.NETWORK_ERROR, "connection lost")
        self.assertEqual(str(error), "[network_error] connection lost")

    def test_default_provider_and_details_are_none(self) -> None:
        error = BrokerError(BrokerErrorCode.RATE_LIMITED, "slow down")
        self.assertIsNone(error.provider)
        self.assertIsNone(error.details)
        self.assertEqual(error.message, "slow down")

    def test_all_error_codes_exist_and_constructible(self) -> None:
        expected_values = {
            "authentication_error",
            "token_expired",
            "invalid_credentials",
            "rate_limited",
            "broker_unavailable",
            "account_not_found",
            "symbol_not_found",
            "order_rejected",
            "insufficient_margin",
            "market_closed",
            "network_error",
            "unknown_provider_error",
        }
        self.assertEqual({code.value for code in BrokerErrorCode}, expected_values)
        for code in BrokerErrorCode:
            error = BrokerError(code, "boom")
            self.assertIs(error.code, code)
            self.assertEqual(error.to_dict()["error"], code.value)


# ---------------------------------------------------------------------------
# 3. TradingConfig / credential validation
# ---------------------------------------------------------------------------
class TradingConfigTests(_BrokerTestCase):
    """Environment resolution, credential validation, safety and secrets."""

    def test_from_env_defaults_to_mock_demo(self) -> None:
        # setUp removed TRADING_PROVIDER / TRADING_MODE entirely.
        config = TradingConfig.from_env()
        self.assertEqual(config.active_provider, "mock")
        self.assertIs(config.mode, TradingMode.DEMO)
        self.assertFalse(config.is_live())

    def test_from_env_mock_demo_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {"TRADING_PROVIDER": "mock", "TRADING_MODE": "demo"},
            clear=False,
        ):
            config = TradingConfig.from_env()
        self.assertEqual(config.active_provider, "mock")
        self.assertIs(config.mode, TradingMode.DEMO)

    def test_from_env_live_mode_is_live(self) -> None:
        with patch.dict(
            os.environ,
            {"TRADING_PROVIDER": "mock", "TRADING_MODE": "live"},
            clear=False,
        ):
            config = TradingConfig.from_env()
        self.assertIs(config.mode, TradingMode.LIVE)
        self.assertTrue(config.is_live())

    def test_from_env_invalid_mode_falls_back_to_demo(self) -> None:
        with patch.dict(
            os.environ,
            {"TRADING_PROVIDER": "mock", "TRADING_MODE": "bogus"},
            clear=False,
        ):
            config = TradingConfig.from_env()
        self.assertIs(config.mode, TradingMode.DEMO)
        self.assertFalse(config.is_live())

    def test_validate_credentials_mock(self) -> None:
        config = TradingConfig.from_env()
        self.assertEqual(config.validate_credentials("mock"), (True, []))

    def test_validate_credentials_pepperstone_missing_env_lists_names(self) -> None:
        config = TradingConfig.from_env()  # PEPPERSTONE_* removed by setUp
        valid, errors = config.validate_credentials("pepperstone")
        self.assertFalse(valid)
        self.assertEqual(len(errors), len(PEPPERSTONE_REQUIRED_ENV))
        joined = " ".join(errors)
        for name in PEPPERSTONE_REQUIRED_ENV:
            self.assertIn(name, joined)

    def test_validate_credentials_pepperstone_placeholders(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "your_client_id",
                "PEPPERSTONE_CLIENT_SECRET": "your_client_secret",
                "PEPPERSTONE_ACCESS_TOKEN": "your_access_token",
            },
            clear=False,
        ):
            config = TradingConfig.from_env()
            valid, errors = config.validate_credentials("pepperstone")
        self.assertFalse(valid)
        self.assertTrue(any("placeholder" in error for error in errors))
        # Errors reference env var NAMES, never the placeholder values.
        joined = " ".join(errors)
        for name in PEPPERSTONE_REQUIRED_ENV:
            self.assertIn(name, joined)
        self.assertNotIn("your_access_token", joined)

    def test_validate_credentials_unknown_provider(self) -> None:
        config = TradingConfig.from_env()
        valid, errors = config.validate_credentials("nonexistent")
        self.assertFalse(valid)
        self.assertEqual(errors, ["Unknown provider 'nonexistent'"])

    def test_get_provider(self) -> None:
        config = TradingConfig.from_env()
        mock_cfg = config.get_provider("mock")
        self.assertIsNotNone(mock_cfg)
        self.assertEqual(mock_cfg.name, "mock")
        self.assertTrue(mock_cfg.credentials_valid)
        pepperstone_cfg = config.get_provider("pepperstone")
        self.assertEqual(pepperstone_cfg.requires, list(PEPPERSTONE_REQUIRED_ENV))
        self.assertIsNone(config.get_provider("not-a-provider"))

    def test_provider_config_defaults(self) -> None:
        cfg = ProviderConfig(name="x", enabled=True, mode=TradingMode.DEMO)
        self.assertEqual(cfg.requires, [])
        self.assertFalse(cfg.credentials_valid)
        self.assertEqual(cfg.validation_errors, [])

    def test_safety_check_live_mock_warning(self) -> None:
        config = TradingConfig(active_provider="mock", mode=TradingMode.LIVE)
        warnings = config.safety_check()
        self.assertTrue(
            any(
                "TRADING_MODE=live but TRADING_PROVIDER=mock" in warning
                for warning in warnings
            )
        )

    def test_safety_check_demo_mode_note(self) -> None:
        config = TradingConfig(active_provider="mock", mode=TradingMode.DEMO)
        warnings = config.safety_check()
        self.assertEqual(
            warnings, ["Trading is in demo mode — live trading is disabled."]
        )

    def test_safety_check_live_invalid_active_provider_credentials(self) -> None:
        # PEPPERSTONE_* env vars are removed by setUp -> credentials invalid.
        config = TradingConfig(active_provider="pepperstone", mode=TradingMode.LIVE)
        warnings = config.safety_check()
        self.assertTrue(
            any("real orders may be placed" in warning for warning in warnings)
        )
        self.assertTrue(
            any(
                "credentials are invalid" in warning
                and "PEPPERSTONE_CLIENT_ID" in warning
                and "PEPPERSTONE_ACCESS_TOKEN" in warning
                for warning in warnings
            )
        )

    def test_to_dict_structure(self) -> None:
        with patch.dict(
            os.environ,
            {"TRADING_PROVIDER": "mock", "TRADING_MODE": "demo"},
            clear=False,
        ):
            config = TradingConfig.from_env()
            data = config.to_dict()
        self.assertEqual(
            set(data), {"active_provider", "mode", "is_live", "providers", "safety_warnings"}
        )
        self.assertEqual(data["active_provider"], "mock")
        self.assertEqual(data["mode"], "demo")
        self.assertFalse(data["is_live"])
        self.assertTrue(data["providers"]["mock"]["credentials_valid"])
        # Exact membership: the provider summary is closed — no retired
        # provider may reappear without an explicit contract change.
        self.assertEqual(set(data["providers"]), {"mock", "pepperstone"})
        self.assertEqual(
            data["providers"]["pepperstone"]["requires"],
            list(PEPPERSTONE_REQUIRED_ENV),
        )

    def test_to_dict_never_exposes_secret_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TRADING_PROVIDER": "pepperstone",
                "TRADING_MODE": "demo",
                "PEPPERSTONE_CLIENT_ID": "99887",
                "PEPPERSTONE_ACCESS_TOKEN": "super-secret-token-abc123",
            },
            clear=False,
        ):
            config = TradingConfig.from_env()
            text = json.dumps(config.to_dict())
        self.assertNotIn("super-secret-token-abc123", text)
        self.assertNotIn("99887", text)
        # The env var NAME may appear; its VALUE never does.
        self.assertIn("PEPPERSTONE_ACCESS_TOKEN", text)

    def test_placeholder_values_never_serialized(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PEPPERSTONE_CLIENT_ID": "your_client_id",
                "PEPPERSTONE_CLIENT_SECRET": "your_client_secret",
                "PEPPERSTONE_ACCESS_TOKEN": "your_access_token",
            },
            clear=False,
        ):
            config = TradingConfig.from_env()
            text = json.dumps(config.to_dict())
        self.assertNotIn("your_access_token", text)
        self.assertIn("PEPPERSTONE_ACCESS_TOKEN", text)

    def test_repr(self) -> None:
        config = TradingConfig(active_provider="mock", mode=TradingMode.DEMO)
        self.assertEqual(repr(config), "TradingConfig(active_provider='mock', mode='demo')")


# ---------------------------------------------------------------------------
# 4. TradingProviderFactory
# ---------------------------------------------------------------------------
class TradingProviderFactoryTests(_BrokerTestCase):
    """Registry, creation, cached singleton, reset and switching."""

    def test_create_mock_returns_mock_provider(self) -> None:
        provider = TradingProviderFactory.create("mock")
        self.assertIsInstance(provider, MockProvider)
        self.assertEqual(provider.name, "mock")

    def test_create_unknown_provider_raises(self) -> None:
        with self.assertRaises(BrokerError) as ctx:
            TradingProviderFactory.create("unknown-provider")
        self.assertIs(ctx.exception.code, BrokerErrorCode.UNKNOWN_PROVIDER_ERROR)
        self.assertEqual(ctx.exception.provider, "unknown-provider")

    def test_get_active_returns_same_cached_instance(self) -> None:
        TradingProviderFactory.reset()
        first = TradingProviderFactory.get_active()
        second = TradingProviderFactory.get_active()
        self.assertIsInstance(first, MockProvider)
        self.assertIs(first, second)

    def test_reset_clears_cached_instance(self) -> None:
        first = TradingProviderFactory.get_active()
        TradingProviderFactory.reset()
        third = TradingProviderFactory.get_active()
        self.assertIsNot(first, third)

    def test_switch_returns_and_caches_provider(self) -> None:
        provider = TradingProviderFactory.switch("mock")
        self.assertIsInstance(provider, MockProvider)
        self.assertIs(provider, TradingProviderFactory.get_active())

    def test_switch_between_mock_providers_of_different_configs(self) -> None:
        live_config = TradingConfig(active_provider="mock", mode=TradingMode.LIVE)
        first = TradingProviderFactory.switch("mock", config=live_config)
        self.assertIsInstance(first, MockProvider)
        self.assertIs(first.mode, TradingMode.LIVE)
        self.assertIs(first, TradingProviderFactory.get_active())

        demo_config = TradingConfig(active_provider="mock", mode=TradingMode.DEMO)
        second = TradingProviderFactory.switch("mock", config=demo_config)
        self.assertIs(second, TradingProviderFactory.get_active())
        self.assertIs(second.mode, TradingMode.DEMO)
        self.assertIsNot(first, second)
        # The previous active provider was disconnected by the switch.
        self.assertEqual(
            first.get_connection_status(), ConnectionStatus.DISCONNECTED
        )

    def test_register_custom_provider(self) -> None:
        TradingProviderFactory.register("stub", _StubProvider)
        self.assertIn("stub", TradingProviderFactory.registered())
        provider = TradingProviderFactory.create("stub")
        self.assertIsInstance(provider, _StubProvider)

    def test_register_rejects_non_provider_class(self) -> None:
        with self.assertRaises(TypeError):
            TradingProviderFactory.register("not-a-provider", dict)


# ---------------------------------------------------------------------------
# 5. MockProvider behavior
# ---------------------------------------------------------------------------
class MockProviderTests(_BrokerTestCase):
    """Behavioral tests of the in-process broker simulator."""

    def setUp(self) -> None:
        super().setUp()
        self.provider = MockProvider(
            config=TradingConfig(active_provider="mock", mode=TradingMode.DEMO)
        )
        self.provider.reset()

    def test_get_account(self) -> None:
        account = self.provider.get_account()
        self.assertIsInstance(account, BrokerAccount)
        self.assertEqual(account.provider, "mock")
        self.assertEqual(account.account_id, "mock-account-001")
        self.assertEqual(account.account_type, "demo")
        self.assertEqual(account.currency, "USD")
        self.assertIs(account.status, AccountStatus.ACTIVE)

    def test_get_balance_default(self) -> None:
        balance = self.provider.get_balance()
        self.assertIsInstance(balance, BrokerBalance)
        self.assertEqual(balance.provider, "mock")
        self.assertEqual(balance.balance, 50000.0)
        self.assertEqual(balance.equity, 50000.0)
        self.assertEqual(balance.margin, 0.0)
        self.assertEqual(balance.free_margin, 50000.0)
        self.assertEqual(balance.currency, "USD")

    def test_get_symbols_at_least_five_including_eurusd(self) -> None:
        symbols = self.provider.get_symbols()
        self.assertGreaterEqual(len(symbols), 5)
        names = {symbol.symbol for symbol in symbols}
        self.assertIn("EURUSD", names)
        eurusd = next(symbol for symbol in symbols if symbol.symbol == "EURUSD")
        self.assertEqual(eurusd.provider, "mock")
        self.assertEqual(eurusd.digits, 5)

    def test_get_market_data_returns_positive_bid(self) -> None:
        tick = self.provider.get_market_data("EURUSD")
        self.assertIsInstance(tick, MarketTick)
        self.assertEqual(tick.provider, "mock")
        self.assertEqual(tick.symbol, "EURUSD")
        self.assertGreater(tick.bid, 0.0)
        self.assertGreaterEqual(tick.ask, tick.bid)
        self.assertIsNotNone(tick.timestamp)

    def test_get_ticks_returns_requested_count(self) -> None:
        ticks = self.provider.get_ticks("EURUSD", 10)
        self.assertEqual(len(ticks), 10)
        for tick in ticks:
            self.assertEqual(tick.provider, "mock")
            self.assertEqual(tick.symbol, "EURUSD")
            self.assertGreater(tick.bid, 0.0)
        # Ticks are generated in time order (oldest -> newest in the window).
        timestamps = [tick.timestamp for tick in ticks]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_get_candles_ohlc_shape_and_ordering(self) -> None:
        candles = self.provider.get_candles("EURUSD", "1H", 5)
        self.assertEqual(len(candles), 5)
        for candle in candles:
            self.assertEqual(candle.provider, "mock")
            self.assertEqual(candle.symbol, "EURUSD")
            self.assertEqual(candle.timeframe, "1H")
            self.assertGreater(candle.open, 0.0)
            self.assertGreater(candle.high, 0.0)
            self.assertGreater(candle.low, 0.0)
            self.assertGreater(candle.close, 0.0)
            self.assertGreaterEqual(candle.high, candle.open)
            self.assertGreaterEqual(candle.high, candle.close)
            self.assertLessEqual(candle.low, candle.open)
            self.assertLessEqual(candle.low, candle.close)
        timestamps = [candle.timestamp for candle in candles]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_place_market_order_fills_and_creates_position_and_trade(self) -> None:
        order = self.provider.place_order("EURUSD", "buy", 1000)
        self.assertIsInstance(order, BrokerOrder)
        self.assertEqual(order.provider, "mock")
        self.assertEqual(order.symbol, "EURUSD")
        self.assertIs(order.side, OrderSide.BUY)
        self.assertEqual(order.quantity, 1000.0)
        self.assertEqual(order.filled_quantity, 1000.0)
        self.assertEqual(order.order_type, "market")
        self.assertIs(order.status, OrderStatus.FILLED)
        self.assertIsNotNone(order.price)

        positions = self.provider.get_positions()
        self.assertEqual(len(positions), 1)
        position = positions[0]
        self.assertIs(position.status, PositionStatus.OPEN)
        self.assertEqual(position.symbol, "EURUSD")
        self.assertEqual(position.quantity, 1000.0)

        trades = self.provider.get_trade_history()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].symbol, "EURUSD")
        self.assertEqual(trades[0].quantity, 1000.0)
        self.assertIs(trades[0].side, OrderSide.BUY)

        # Mock margin is deducted from the balance.
        self.assertLess(self.provider.get_balance().balance, 50000.0)

    def test_place_order_unknown_symbol_raises(self) -> None:
        with self.assertRaises(BrokerError) as ctx:
            self.provider.place_order("NOT-A-SYMBOL", "buy", 100)
        self.assertIs(ctx.exception.code, BrokerErrorCode.SYMBOL_NOT_FOUND)
        self.assertEqual(ctx.exception.provider, "mock")

    def test_place_order_invalid_side_raises(self) -> None:
        with self.assertRaises(BrokerError) as ctx:
            self.provider.place_order("EURUSD", "diagonal", 100)
        self.assertIs(ctx.exception.code, BrokerErrorCode.ORDER_REJECTED)

    def test_place_limit_order_then_cancel(self) -> None:
        order = self.provider.place_order(
            "EURUSD", "buy", 100, order_type="limit", price=1.1000
        )
        self.assertIs(order.status, OrderStatus.PENDING)
        self.assertEqual(order.price, 1.1000)
        self.assertEqual(order.filled_quantity, 0.0)

        cancelled = self.provider.cancel_order(order.external_order_id)
        self.assertIs(cancelled.status, OrderStatus.CANCELLED)
        self.assertGreaterEqual(cancelled.updated_at, cancelled.created_at)
        # A cancelled pending order never opened a position.
        self.assertEqual(len(self.provider.get_positions()), 0)

        with self.assertRaises(BrokerError) as ctx:
            self.provider.cancel_order("no-such-order")
        self.assertIs(ctx.exception.code, BrokerErrorCode.ORDER_REJECTED)

    def test_fail_mode_network_raises_network_error(self) -> None:
        self.provider.set_fail_mode("network")
        with self.assertRaises(BrokerError) as ctx:
            self.provider.get_balance()
        self.assertIs(ctx.exception.code, BrokerErrorCode.NETWORK_ERROR)
        self.assertEqual(ctx.exception.provider, "mock")

    def test_fail_mode_auth_raises_authentication_error(self) -> None:
        self.provider.set_fail_mode("auth")
        with self.assertRaises(BrokerError) as ctx:
            self.provider.get_balance()
        self.assertIs(ctx.exception.code, BrokerErrorCode.AUTHENTICATION_ERROR)

    def test_fail_mode_clear_restores_operation(self) -> None:
        self.provider.set_fail_mode("network")
        with self.assertRaises(BrokerError):
            self.provider.get_balance()
        self.provider.set_fail_mode(None)
        self.assertEqual(self.provider.get_balance().balance, 50000.0)

    def test_fail_mode_rate_limit_and_unavailable(self) -> None:
        self.provider.set_fail_mode("rate_limit")
        with self.assertRaises(BrokerError) as ctx:
            self.provider.get_balance()
        self.assertIs(ctx.exception.code, BrokerErrorCode.RATE_LIMITED)

        self.provider.set_fail_mode("unavailable")
        with self.assertRaises(BrokerError) as ctx:
            self.provider.get_balance()
        self.assertIs(ctx.exception.code, BrokerErrorCode.BROKER_UNAVAILABLE)

    def test_fail_mode_invalid_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.set_fail_mode("bogus-mode")

    def test_invalid_timeframe_raises(self) -> None:
        with self.assertRaises(BrokerError) as ctx:
            self.provider.get_candles("EURUSD", "2H", 5)
        self.assertIs(ctx.exception.code, BrokerErrorCode.UNKNOWN_PROVIDER_ERROR)

    def test_health_snapshot(self) -> None:
        health = self.provider.health()
        self.assertIsInstance(health, ProviderHealth)
        self.assertEqual(health.provider, "mock")
        self.assertEqual(health.environment, "demo")
        self.assertEqual(health.authentication_status, "authenticated")
        self.assertEqual(health.connection_status, "connected")
        self.assertTrue(health.api_available)
        self.assertEqual(health.account_sync_status, "healthy")

    def test_disconnect_sets_disconnected(self) -> None:
        self.assertEqual(
            self.provider.get_connection_status(), ConnectionStatus.CONNECTED
        )
        self.provider.disconnect()
        self.assertEqual(
            self.provider.get_connection_status(), ConnectionStatus.DISCONNECTED
        )

    def test_mode_propagates_from_config(self) -> None:
        provider = MockProvider(
            config=TradingConfig(active_provider="mock", mode=TradingMode.LIVE)
        )
        self.assertIs(provider.mode, TradingMode.LIVE)
        self.assertEqual(provider.get_account().account_type, "live")


# ---------------------------------------------------------------------------
# 6. PepperstoneProvider graceful path
# ---------------------------------------------------------------------------
class PepperstoneProviderGracefulPathTests(_BrokerTestCase):
    """Pepperstone with unset credentials degrades without a real subprocess."""

    def test_get_balance_missing_credentials_maps_worker_auth_error(self) -> None:
        # This is the query worker's real output contract for missing
        # credentials (see forex/pepperstone/query_worker.main()): a single
        # JSON line with ok:false + code "authentication_error". The worker
        # never starts the cTrader reactor for this path.
        worker_stdout = json.dumps(
            {
                "ok": False,
                "error": (
                    "Pepperstone credentials missing: PEPPERSTONE_CLIENT_ID, "
                    "PEPPERSTONE_CLIENT_SECRET, PEPPERSTONE_ACCESS_TOKEN. Set "
                    "them in the environment (or the project .env file) and "
                    "re-run."
                ),
                "code": BrokerErrorCode.AUTHENTICATION_ERROR.value,
            }
        )
        completed = subprocess.CompletedProcess(
            args=["python", "-m", "forex.pepperstone.query_worker"],
            returncode=0,
            stdout=worker_stdout,
            stderr="",
        )
        with patch.dict(os.environ, _EMPTY_PEPPERSTONE_ENV, clear=False):
            provider = PepperstoneProvider(
                config=TradingConfig(
                    active_provider="pepperstone", mode=TradingMode.DEMO
                )
            )
            with patch(
                "forex.broker.providers.pepperstone.subprocess.run",
                return_value=completed,
            ) as mock_run:
                with self.assertRaises(BrokerError) as ctx:
                    provider.get_balance()

        error = ctx.exception
        self.assertIs(error.code, BrokerErrorCode.AUTHENTICATION_ERROR)
        self.assertIn("Pepperstone credentials invalid", error.message)
        self.assertIn("PEPPERSTONE_CLIENT_ID", error.message)
        self.assertEqual(error.provider, "pepperstone")

        # The credential gate fired BEFORE subprocess.run — no real worker
        # process (and therefore no cTrader SDK handshake) was spawned.
        mock_run.assert_not_called()

    def test_place_order_missing_credentials_no_subprocess_waiting_auth(self) -> None:
        # place_order() hits the credential gate (_credentials) BEFORE any
        # subprocess/executor work, so the graceful path spawns nothing.
        with patch.dict(os.environ, _EMPTY_PEPPERSTONE_ENV, clear=False):
            provider = PepperstoneProvider(
                config=TradingConfig(
                    active_provider="pepperstone", mode=TradingMode.DEMO
                )
            )
            with patch(
                "forex.broker.providers.pepperstone.subprocess.run",
                side_effect=AssertionError("a subprocess must not be spawned"),
            ) as mock_run:
                with self.assertRaises(BrokerError) as ctx:
                    provider.place_order("EURUSD", "buy", 100)

        error = ctx.exception
        self.assertIs(error.code, BrokerErrorCode.AUTHENTICATION_ERROR)
        self.assertIn("Pepperstone credentials invalid", error.message)
        self.assertIn("PEPPERSTONE_CLIENT_ID", error.message)
        self.assertEqual(error.provider, "pepperstone")
        mock_run.assert_not_called()

        # "Waiting for authentication" semantics recorded on the health monitor.
        health = provider.health()
        self.assertEqual(health.authentication_status, "waiting_for_authentication")
        self.assertEqual(
            health.connection_status, ConnectionStatus.AUTHENTICATION_FAILED
        )


class PepperstoneRefreshTokenTests(_BrokerTestCase):
    """The provider forwards refresh_token to the session + fails fast without it."""

    _VALID_ENV = {
        "PEPPERSTONE_CLIENT_ID": "cid",
        "PEPPERSTONE_CLIENT_SECRET": "csecret",
        "PEPPERSTONE_ACCESS_TOKEN": "atoken",
        "PEPPERSTONE_REFRESH_TOKEN": "rtoken",
    }

    def _provider(self) -> PepperstoneProvider:
        return PepperstoneProvider(
            config=TradingConfig(active_provider="pepperstone", mode=TradingMode.DEMO)
        )

    def test_refresh_token_is_required_and_forwarded(self) -> None:
        with patch.dict(os.environ, self._VALID_ENV, clear=False):
            creds = self._provider()._credentials()
        self.assertEqual(creds["refresh_token"], "rtoken")

    def test_missing_refresh_token_fails_fast_naming_key(self) -> None:
        env = dict(self._VALID_ENV, PEPPERSTONE_REFRESH_TOKEN="")
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(BrokerError) as ctx:
                self._provider()._credentials()
        message = ctx.exception.message
        self.assertIs(ctx.exception.code, BrokerErrorCode.AUTHENTICATION_ERROR)
        self.assertIn("PEPPERSTONE_REFRESH_TOKEN", message)
        # Names only — never the other secret VALUES.
        self.assertNotIn("csecret", message)
        self.assertNotIn("atoken", message)

    def test_placeholder_refresh_token_rejected(self) -> None:
        env = dict(self._VALID_ENV, PEPPERSTONE_REFRESH_TOKEN="your_refresh_token")
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(BrokerError) as ctx:
                self._provider()._credentials()
        self.assertIn("PEPPERSTONE_REFRESH_TOKEN", ctx.exception.message)

    def test_required_env_declares_refresh_token(self) -> None:
        # config.py and the provider gate must agree on the required key set.
        self.assertIn("PEPPERSTONE_REFRESH_TOKEN", PEPPERSTONE_REQUIRED_ENV)


# ---------------------------------------------------------------------------
# 7b. MockProvider windowed tick history (get_ticks_range)
# ---------------------------------------------------------------------------
class MockProviderTicksRangeTests(_BrokerTestCase):
    """Deterministic windowed tick history of the in-process simulator."""

    def setUp(self) -> None:
        super().setUp()
        self.provider = MockProvider(
            config=TradingConfig(active_provider="mock", mode=TradingMode.DEMO)
        )
        self.provider.reset()

    def _window(self, seconds: int = 30, limit: int = 1000) -> list[MarketTick]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=seconds)
        return self.provider.get_ticks_range("EURUSD", start, end, limit=limit)

    def test_deterministic_same_window_same_ticks(self) -> None:
        # Fixed window: two calls with different `now` could straddle a tick
        # boundary and flake. Freeze the end timestamp for both calls.
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=30)
        first = self.provider.get_ticks_range("EURUSD", start, end, limit=1000)
        second = self.provider.get_ticks_range("EURUSD", start, end, limit=1000)
        self.assertEqual(len(first), len(second))
        for a, b in zip(first, second):
            self.assertEqual(a.timestamp, b.timestamp)
            self.assertEqual(a.last, b.last)
            self.assertEqual(a.bid, b.bid)

    def test_chronological_and_window_bounds(self) -> None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=30)
        ticks = self.provider.get_ticks_range("EURUSD", start, end, limit=1000)
        self.assertGreaterEqual(len(ticks), 1)
        ts = [t.timestamp for t in ticks]
        self.assertEqual(ts, sorted(ts))
        self.assertGreaterEqual(ts[0], start)
        self.assertLessEqual(ts[-1], end)
        self.assertTrue(all(t.symbol == "EURUSD" for t in ticks))
        self.assertTrue(all(t.provider == "mock" for t in ticks))

    def test_limit_capped_at_1000(self) -> None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=5000)
        ticks = self.provider.get_ticks_range("EURUSD", start, end, limit=5000)
        self.assertLessEqual(len(ticks), 1000)

    def test_fail_mode_network_raises(self) -> None:
        self.provider.set_fail_mode("network")
        with self.assertRaises(BrokerError) as ctx:
            self._window()
        self.assertIs(ctx.exception.code, BrokerErrorCode.NETWORK_ERROR)
        self.assertEqual(ctx.exception.provider, "mock")

    def test_validation_errors(self) -> None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=30)
        with self.assertRaises(ValueError):
            self.provider.get_ticks_range("EURUSD", datetime(2026, 1, 1), end)
        with self.assertRaises(ValueError):
            self.provider.get_ticks_range("EURUSD", end, start)
        with self.assertRaises(ValueError):
            self.provider.get_ticks_range("EURUSD", start, end, limit=0)

    def test_unknown_symbol_raises(self) -> None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=30)
        with self.assertRaises(BrokerError) as ctx:
            self.provider.get_ticks_range("NOT-A-SYMBOL", start, end)
        self.assertIs(ctx.exception.code, BrokerErrorCode.SYMBOL_NOT_FOUND)


# ---------------------------------------------------------------------------
# 8. ProviderHealthMonitor
# ---------------------------------------------------------------------------
class ProviderHealthMonitorTests(_BrokerTestCase):
    """Health tracking, heuristic degradation and serialization."""

    def test_initial_snapshot_defaults(self) -> None:
        monitor = ProviderHealthMonitor("mock", "demo")
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.provider, "mock")
        self.assertEqual(snapshot.environment, "demo")
        self.assertEqual(snapshot.authentication_status, "not_required")
        self.assertEqual(snapshot.connection_status, "disconnected")
        self.assertFalse(snapshot.api_available)
        self.assertEqual(snapshot.account_sync_status, "never_synced")
        self.assertIsNone(snapshot.last_successful_request)
        self.assertIsNone(snapshot.last_market_data_update)
        self.assertIsNone(snapshot.last_error)

    def test_record_success_and_market_data_update_timestamps(self) -> None:
        monitor = ProviderHealthMonitor("mock", "demo")
        monitor.record_success("get_balance")
        monitor.record_market_data()
        snapshot = monitor.snapshot()
        self.assertIsNotNone(snapshot.last_successful_request)
        self.assertIsNotNone(snapshot.last_market_data_update)
        self.assertIsNone(snapshot.last_error)

    def test_record_error_degrades_status_heuristically(self) -> None:
        monitor = ProviderHealthMonitor("mock", "demo")
        monitor.record_error("network timeout while fetching ticks")
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.last_error, "network timeout while fetching ticks")
        self.assertEqual(snapshot.connection_status, "error")
        # No auth hint in the message -> authentication status untouched.
        self.assertEqual(snapshot.authentication_status, "not_required")

        monitor.record_error("authentication failed: invalid token")
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.authentication_status, "authentication_failed")

    def test_explicit_setters_roundtrip_through_snapshot(self) -> None:
        monitor = ProviderHealthMonitor("pepperstone", "live")
        monitor.set_authentication_status("authenticated")
        monitor.set_connection_status(ConnectionStatus.CONNECTED)
        monitor.set_api_available(True)
        monitor.set_account_sync_status("healthy")
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.authentication_status, "authenticated")
        self.assertEqual(snapshot.connection_status, "connected")
        self.assertTrue(snapshot.api_available)
        self.assertEqual(snapshot.account_sync_status, "healthy")

    def test_to_dict_has_iso_strings(self) -> None:
        monitor = ProviderHealthMonitor("mock", "demo")
        monitor.record_success("get_balance")
        monitor.record_error("network unavailable")
        snapshot = monitor.snapshot()
        data = snapshot.to_dict()
        self.assertEqual(data["provider"], "mock")
        self.assertEqual(data["environment"], "demo")
        self.assertEqual(data["last_error"], "network unavailable")
        # Datetime fields render as ISO-8601 strings (UTC), None-safe.
        self.assertIsInstance(data["last_successful_request"], str)
        self.assertEqual(
            data["last_successful_request"],
            snapshot.last_successful_request.isoformat(),
        )
        self.assertTrue(data["last_successful_request"].endswith("+00:00"))
        self.assertIsNone(data["last_market_data_update"])

    def test_snapshot_is_frozen(self) -> None:
        monitor = ProviderHealthMonitor("mock", "demo")
        snapshot = monitor.snapshot()
        with self.assertRaises(FrozenInstanceError):
            snapshot.provider = "pepperstone"


# ---------------------------------------------------------------------------
# 9. Contract constants
# ---------------------------------------------------------------------------
class BrokerContractTests(_BrokerTestCase):
    """Provider-neutral contract constants."""

    def test_candle_timeframes(self) -> None:
        self.assertEqual(CANDLE_TIMEFRAMES, ("1M", "5M", "15M", "30M", "1H", "4H", "1D"))
