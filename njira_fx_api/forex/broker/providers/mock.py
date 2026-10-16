"""
Mock trading provider for the broker abstraction layer.

Simulates a broker entirely in-process so development, integration tests and
local tooling never depend on an external broker being reachable. It provides
deterministic, controllable simulation of:

* account descriptors, balances / equity / free margin
* live market ticks (random-walk prices) and synthetic OHLC candles
* positions, orders (market fill instantly, limit/stop rest pending) and
  trade history
* order execution against the simulated book
* connection failures and API errors via fail modes
  (``set_fail_mode()`` or the ``MOCK_FAIL_MODE`` environment variable)
* streaming market data subscriptions (daemon threads, one tick per symbol
  every 0.5s)
* provider health telemetry via ``ProviderHealthMonitor``

All failures surface as :class:`forex.broker.errors.BrokerError` with
normalized :class:`~forex.broker.errors.BrokerErrorCode` values, so callers
never have to know provider internals.

Control-plane methods (``disconnect`` / ``get_connection_status`` / ``health`` /
``set_fail_mode`` / ``reset``) intentionally bypass ``_check_fail_mode`` so
tests can always restore or inspect provider state even while a failure
simulation is active.
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import os
import random
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from forex.broker.base import TradingProvider
from forex.broker.config import TradingConfig
from forex.broker.errors import BrokerError, BrokerErrorCode
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
    TradingMode,
)

logger = logging.getLogger("forex.broker.mock")


class MockProvider(TradingProvider):
    """In-process broker simulator used for development and testing.

    Prices follow a seeded random walk, market orders fill immediately (a
    position and a trade record are created, balance is reduced as mock
    margin), limit/stop orders rest as ``PENDING`` until the caller manages
    them, and connection failures are simulated via fail modes.
    """

    name: str = "mock"
    display_name: str = "Mock Simulator"

    _DEFAULT_BALANCE: float = 50000.0
    _ACCOUNT_ID: str = "mock-account-001"
    _VALID_FAIL_MODES: frozenset[str] = frozenset(
        {"auth", "network", "rate_limit", "unavailable"}
    )
    _TIMEFRAME_MINUTES: dict[str, int] = {
        "1M": 1,
        "5M": 5,
        "15M": 15,
        "30M": 30,
        "1H": 60,
        "4H": 240,
        "1D": 1440,
    }
    _FAIL_MODE_ERRORS: dict[str, tuple[BrokerErrorCode, str]] = {
        "auth": (
            BrokerErrorCode.AUTHENTICATION_ERROR,
            "Mock: authentication failure simulated",
        ),
        "network": (
            BrokerErrorCode.NETWORK_ERROR,
            "Mock: network failure simulated",
        ),
        "rate_limit": (
            BrokerErrorCode.RATE_LIMITED,
            "Mock: rate limit exceeded simulated",
        ),
        "unavailable": (
            BrokerErrorCode.BROKER_UNAVAILABLE,
            "Mock: broker unavailable simulated",
        ),
    }

    def __init__(self, config: TradingConfig | None = None) -> None:
        """Initialize the mock provider simulation state.

        Args:
            config: Optional trading configuration. The active mode is taken
                from ``config.mode`` and defaults to :attr:`TradingMode.DEMO`
                when the config is missing or does not carry a mode.
        """
        self.config: TradingConfig | None = config
        self.mode: TradingMode = (
            config.mode
            if config is not None and config.mode is not None
            else TradingMode.DEMO
        )

        # ------------------------------------------------------------------
        # Account simulation state
        # ------------------------------------------------------------------
        self._balance: float = self._DEFAULT_BALANCE
        self._currency: str = "USD"

        self._symbols: list[BrokerSymbol] = [
            BrokerSymbol(
                provider="mock",
                symbol="EURUSD",
                display_name="EUR/USD",
                digits=5,
                market="forex",
                min_volume=0.01,
                max_volume=100.0,
                tick_size=0.00001,
            ),
            BrokerSymbol(
                provider="mock",
                symbol="GBPUSD",
                display_name="GBP/USD",
                digits=5,
                market="forex",
                min_volume=0.01,
                max_volume=100.0,
                tick_size=0.00001,
            ),
            BrokerSymbol(
                provider="mock",
                symbol="XAUUSD",
                display_name="XAU/USD",
                digits=2,
                market="commodity",
                min_volume=0.01,
                max_volume=100.0,
                tick_size=0.01,
            ),
            BrokerSymbol(
                provider="mock",
                symbol="USDJPY",
                display_name="USD/JPY",
                digits=3,
                market="forex",
                min_volume=0.01,
                max_volume=100.0,
                tick_size=0.001,
            ),
            BrokerSymbol(
                provider="mock",
                symbol="R_75",
                display_name="R_75",
                digits=2,
                market="synthetic_index",
                min_volume=1.0,
                max_volume=100.0,
                tick_size=0.01,
            ),
            BrokerSymbol(
                provider="mock",
                symbol="R_100",
                display_name="R_100",
                digits=2,
                market="synthetic_index",
                min_volume=1.0,
                max_volume=100.0,
                tick_size=0.01,
            ),
            BrokerSymbol(
                provider="mock",
                symbol="BTCUSD",
                display_name="BTC/USD",
                digits=2,
                market="crypto",
                min_volume=0.001,
                max_volume=10.0,
                tick_size=0.01,
            ),
        ]
        self._symbols_by_name: dict[str, BrokerSymbol] = {
            symbol.symbol: symbol for symbol in self._symbols
        }
        self._digits: dict[str, int] = {
            symbol.symbol: symbol.digits for symbol in self._symbols
        }
        self._prices: dict[str, float] = {
            "EURUSD": 1.0850,
            "GBPUSD": 1.2700,
            "XAUUSD": 2400.0,
            "USDJPY": 155.0,
            "R_75": 7500.0,
            "R_100": 10000.0,
            "BTCUSD": 67000.0,
        }
        self._ticks: dict[str, deque[MarketTick]] = {
            symbol: deque(maxlen=1000) for symbol in self._symbols_by_name
        }

        # ------------------------------------------------------------------
        # Order / position / trade simulation state
        # ------------------------------------------------------------------
        self._positions: list[BrokerPosition] = []
        self._orders: list[BrokerOrder] = []
        self._trades: list[BrokerTrade] = []
        self._id_counter: Iterator[int] = itertools.count(1)

        # ------------------------------------------------------------------
        # Connectivity / failure simulation state
        # ------------------------------------------------------------------
        self._connected: bool = True
        self._fail_mode: str | None = self._read_fail_mode()

        # ------------------------------------------------------------------
        # Streaming subscription state
        # ------------------------------------------------------------------
        self._subscriptions: dict[str, Callable[[MarketTick], None]] = {}
        self._tick_threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

        # ------------------------------------------------------------------
        # Health telemetry
        # ------------------------------------------------------------------
        self._health = ProviderHealthMonitor("mock", self.mode.value)
        self._health.set_authentication_status("authenticated")
        self._health.set_connection_status(ConnectionStatus.CONNECTED)
        self._health.set_api_available(True)
        self._health.set_account_sync_status("healthy")

        logger.info(
            "MockProvider initialized (mode=%s, fail_mode=%r)",
            self.mode.value,
            self._fail_mode,
        )

    # ------------------------------------------------------------------
    # Failure simulation
    # ------------------------------------------------------------------
    @classmethod
    def _read_fail_mode(cls) -> str | None:
        """Read and validate the ``MOCK_FAIL_MODE`` environment variable.

        Unknown values are ignored with a warning so a stale environment can
        never break provider startup.
        """
        raw = os.environ.get("MOCK_FAIL_MODE")
        if raw is None:
            return None
        normalized = raw.strip().lower()
        if normalized in cls._VALID_FAIL_MODES:
            return normalized
        logger.warning(
            "Mock: ignoring unknown MOCK_FAIL_MODE=%r (expected one of %s)",
            raw,
            ", ".join(sorted(cls._VALID_FAIL_MODES)),
        )
        return None

    def _check_fail_mode(self, operation: str) -> None:
        """Raise a simulated :class:`BrokerError` when a fail mode is active.

        Called at the start of every data-plane public method so connection
        failures and API errors behave exactly like a real broker outage.
        """
        mode = self._fail_mode
        if mode is None:
            return
        if mode not in self._FAIL_MODE_ERRORS:
            # Defensive: fail modes are validated on write, this should
            # never trigger.
            code = BrokerErrorCode.UNKNOWN_PROVIDER_ERROR
            message = f"Mock: unknown fail mode {mode!r}"
            self._health.record_error(f"{operation}: {message}")
            raise BrokerError(code, message, provider="mock")
        code, message = self._FAIL_MODE_ERRORS[mode]
        logger.debug("Mock: fail mode %r active during %s", mode, operation)
        self._health.record_error(f"{operation}: {message}")
        raise BrokerError(code, message, provider="mock")

    def set_fail_mode(self, mode: str | None) -> None:
        """Enable or disable a simulated failure for testing.

        Args:
            mode: One of ``None`` | ``"auth"`` | ``"network"`` |
                ``"rate_limit"`` | ``"unavailable"``.

        Raises:
            ValueError: When the mode is not a recognized failure mode.
        """
        if mode is None:
            self._fail_mode = None
            logger.info("Mock: fail mode cleared")
            return
        normalized = mode.strip().lower()
        if normalized not in self._VALID_FAIL_MODES:
            raise ValueError(
                f"Mock: unknown fail mode {mode!r}; "
                f"expected one of {', '.join(sorted(self._VALID_FAIL_MODES))}"
            )
        self._fail_mode = normalized
        logger.info("Mock: fail mode set to %r", self._fail_mode)

    def reset(self) -> None:
        """Factory reset of the simulation state.

        Clears positions, orders, trades and tick history, restores the
        starting balance, reconnects the provider, clears the fail mode and
        re-initializes health telemetry. Active market data subscriptions
        keep running.
        """
        self._positions.clear()
        self._orders.clear()
        self._trades.clear()
        for ticks in self._ticks.values():
            ticks.clear()
        self._balance = self._DEFAULT_BALANCE
        self._connected = True
        self._fail_mode = self._read_fail_mode()
        self._health = ProviderHealthMonitor("mock", self.mode.value)
        self._health.set_authentication_status("authenticated")
        self._health.set_connection_status(ConnectionStatus.CONNECTED)
        self._health.set_api_available(True)
        self._health.set_account_sync_status("healthy")
        logger.info("Mock provider reset: balances, orders, positions, trades and ticks cleared")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_symbol(self, symbol: str) -> None:
        """Raise ``SYMBOL_NOT_FOUND`` when the symbol is not simulated."""
        if symbol not in self._symbols_by_name:
            raise BrokerError(
                BrokerErrorCode.SYMBOL_NOT_FOUND,
                f"Mock: unknown symbol {symbol!r}",
                provider="mock",
            )

    @staticmethod
    def _normalize_side(side: OrderSide | str) -> OrderSide:
        """Coerce a side value (enum or case-insensitive string) to OrderSide."""
        if isinstance(side, OrderSide):
            return side
        try:
            normalized = str(side).lower()
            return OrderSide(normalized)
        except ValueError:
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                f"Mock: invalid order side {side!r}",
                provider="mock",
            ) from None

    def _find_order(self, external_order_id: str) -> BrokerOrder | None:
        """Return the order matching ``external_order_id`` or ``None``."""
        for order in self._orders:
            if order.external_order_id == external_order_id:
                return order
        return None

    def _latest_tick(self, symbol: str) -> MarketTick:
        """Return the most recent tick for ``symbol``, generating one lazily."""
        ticks = self._ticks[symbol]
        if not ticks:
            return self._next_tick(symbol)
        return ticks[-1]

    def _next_tick(self, symbol: str) -> MarketTick:
        """Generate the next random-walk tick for ``symbol`` and store it.

        The price moves by a small delta derived from the symbol's digit
        count, so each instrument moves at a realistic granularity. The ask
        is one spread (one pip) above the bid.
        """
        digits = self._digits[symbol]
        step = 10 ** -digits
        price = self._prices[symbol]
        price = round(max(price + random.uniform(-1.0, 1.0) * step * 2.0, step * 2.0), digits)
        self._prices[symbol] = price

        spread = step * 10
        tick = MarketTick(
            provider="mock",
            symbol=symbol,
            bid=price,
            ask=round(price + spread, digits),
            last=price,
            timestamp=datetime.now(timezone.utc),
            volume=round(random.uniform(0.1, 50.0), 2),
        )
        self._ticks[symbol].append(tick)
        return tick

    def _walk_prices(self, symbol: str, steps: int) -> list[float]:
        """Random-walk ``steps`` prices ending at the current market price.

        Returns a list ordered oldest → newest where the last element is the
        latest price, so candle close values can anchor on the live price.
        """
        digits = self._digits[symbol]
        step = 10 ** -digits
        price = self._latest_tick(symbol).last
        prices: list[float] = []
        for _ in range(steps):
            prices.append(price)
            price = round(max(price + random.uniform(-1.0, 1.0) * step * 4.0, step * 2.0), digits)
        prices.reverse()
        return prices

    def _timeframe_minutes(self, timeframe: str) -> int:
        """Map a timeframe label to its length in minutes."""
        minutes = self._TIMEFRAME_MINUTES.get((timeframe or "").upper())
        if minutes is None:
            raise BrokerError(
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
                f"Mock: unsupported timeframe {timeframe!r} "
                f"(expected one of {', '.join(self._TIMEFRAME_MINUTES)})",
                provider="mock",
            )
        return minutes

    def _tick_loop(self, subscription_id: str, symbols: list[str]) -> None:
        """Daemon loop: emit one tick per symbol every 0.5s until stopped."""
        callback = self._subscriptions.get(subscription_id)
        if callback is None:
            logger.warning("Mock: subscription %s not found, tick loop exiting", subscription_id)
            return
        while not self._stop_event.is_set():
            for symbol in symbols:
                if self._stop_event.is_set():
                    return
                try:
                    callback(self._next_tick(symbol))
                except Exception:
                    logger.exception(
                        "Mock: subscription callback failed "
                        "(subscription=%s, symbol=%s)",
                        subscription_id,
                        symbol,
                    )
            self._stop_event.wait(0.5)

    # ------------------------------------------------------------------
    # TradingProvider contract
    # ------------------------------------------------------------------
    def get_account(self) -> BrokerAccount:
        """Return the simulated broker account descriptor."""
        self._check_fail_mode("get_account")
        self._health.record_success("get_account")
        return BrokerAccount(
            provider="mock",
            account_id=self._ACCOUNT_ID,
            account_type=self.mode.value,
            currency=self._currency,
            status=AccountStatus.ACTIVE,
            name="Mock Trading Account",
            leverage=100.0,
        )

    def get_balance(self) -> BrokerBalance:
        """Return the current simulated balance snapshot."""
        self._check_fail_mode("get_balance")
        self._health.record_success("get_balance")
        return BrokerBalance(
            provider="mock",
            account_id=self._ACCOUNT_ID,
            balance=self._balance,
            equity=self._balance,
            margin=0.0,
            free_margin=self._balance,
            currency=self._currency,
            timestamp=datetime.now(timezone.utc),
        )

    def get_positions(self) -> list[BrokerPosition]:
        """Return open/closed positions with live current prices."""
        self._check_fail_mode("get_positions")
        self._health.record_success("get_positions")
        refreshed: list[BrokerPosition] = []
        for position in self._positions:
            if position.status is PositionStatus.OPEN:
                tick = self._latest_tick(position.symbol)
                position = replace(position, current_price=tick.last)
            refreshed.append(position)
        self._positions = refreshed
        return list(self._positions)

    def get_orders(self) -> list[BrokerOrder]:
        """Return all orders placed with this provider."""
        self._check_fail_mode("get_orders")
        self._health.record_success("get_orders")
        return list(self._orders)

    def place_order(
        self,
        symbol: str,
        side: OrderSide | str,
        quantity: float,
        order_type: str = "market",
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> BrokerOrder:
        """Simulate placing an order with the mock broker.

        Market orders fill immediately at the current ask (or the supplied
        price): a filled order, an open position and an open trade record are
        created, and the order value is deducted from the balance as mock
        margin. Limit/stop orders rest as ``PENDING`` at the supplied price.
        """
        self._check_fail_mode("place_order")
        self._ensure_symbol(symbol)
        side_enum = self._normalize_side(side)
        order_type = (order_type or "market").strip().lower()
        now = datetime.now(timezone.utc)
        digits = self._digits[symbol]

        if order_type in {"market", "market_order"}:
            execution_price = (
                self._latest_tick(symbol).ask if price is None else price
            )
            execution_price = round(execution_price, digits)
            # Deduct the notional from the balance as mock margin.
            self._balance -= quantity * execution_price

            order = BrokerOrder(
                provider="mock",
                external_order_id=f"mock-order-{next(self._id_counter)}",
                account_id=self._ACCOUNT_ID,
                symbol=symbol,
                side=side_enum,
                order_type="market",
                quantity=quantity,
                status=OrderStatus.FILLED,
                created_at=now,
                updated_at=now,
                filled_quantity=quantity,
                price=execution_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            position = BrokerPosition(
                provider="mock",
                external_position_id=f"mock-position-{next(self._id_counter)}",
                account_id=self._ACCOUNT_ID,
                symbol=symbol,
                side=side_enum,
                quantity=quantity,
                entry_price=execution_price,
                current_price=execution_price,
                status=PositionStatus.OPEN,
                opened_at=now,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            trade = BrokerTrade(
                provider="mock",
                external_trade_id=f"mock-trade-{next(self._id_counter)}",
                account_id=self._ACCOUNT_ID,
                symbol=symbol,
                side=side_enum,
                quantity=quantity,
                entry_price=execution_price,
                status="open",
                opened_at=now,
            )
            self._orders.append(order)
            self._positions.append(position)
            self._trades.append(trade)
        else:
            # Limit / stop orders rest as pending until managed by the caller.
            order = BrokerOrder(
                provider="mock",
                external_order_id=f"mock-order-{next(self._id_counter)}",
                account_id=self._ACCOUNT_ID,
                symbol=symbol,
                side=side_enum,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.PENDING,
                created_at=now,
                updated_at=now,
                filled_quantity=0.0,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            self._orders.append(order)

        self._health.record_success("place_order")
        logger.info(
            "Mock: order %s %s %s %s qty=%s @ %s",
            order.external_order_id,
            side_enum.value.upper(),
            order_type,
            symbol,
            quantity,
            order.price if order.price is not None else "market",
        )
        return order

    def cancel_order(self, external_order_id: str) -> BrokerOrder:
        """Cancel a working order by external id."""
        self._check_fail_mode("cancel_order")
        order = self._find_order(external_order_id)
        if order is None:
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                "Order not found",
                provider="mock",
            )
        now = datetime.now(timezone.utc)
        cancelled = replace(order, status=OrderStatus.CANCELLED, updated_at=now)
        self._orders[self._orders.index(order)] = cancelled
        self._health.record_success("cancel_order")
        logger.info("Mock: order %s cancelled", external_order_id)
        return cancelled

    def modify_order(
        self,
        external_order_id: str,
        quantity: float | None = None,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> BrokerOrder:
        """Modify the provided fields of a working order."""
        self._check_fail_mode("modify_order")
        order = self._find_order(external_order_id)
        if order is None:
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                "Order not found",
                provider="mock",
            )
        changes: dict[str, object] = {"updated_at": datetime.now(timezone.utc)}
        if quantity is not None:
            changes["quantity"] = quantity
        if price is not None:
            changes["price"] = price
        if stop_loss is not None:
            changes["stop_loss"] = stop_loss
        if take_profit is not None:
            changes["take_profit"] = take_profit
        modified = replace(order, **changes)
        self._orders[self._orders.index(order)] = modified
        self._health.record_success("modify_order")
        logger.info("Mock: order %s modified (%s)", external_order_id, list(changes))
        return modified

    def get_market_data(self, symbol: str) -> MarketTick:
        """Return a fresh simulated tick for ``symbol``."""
        self._check_fail_mode("get_market_data")
        self._ensure_symbol(symbol)
        tick = self._next_tick(symbol)
        self._health.record_success("get_market_data")
        self._health.record_market_data()
        return tick

    def get_ticks(self, symbol: str, limit: int = 100) -> list[MarketTick]:
        """Return the most recent ``limit`` ticks, generating any shortfall."""
        self._check_fail_mode("get_ticks")
        self._ensure_symbol(symbol)
        ticks = self._ticks[symbol]
        capacity = ticks.maxlen or limit
        shortfall = min(limit, capacity) - len(ticks)
        for _ in range(max(0, shortfall)):
            self._next_tick(symbol)
        self._health.record_success("get_ticks")
        return list(ticks)[-limit:]

    def get_candles(
        self, symbol: str, timeframe: str, limit: int = 100
    ) -> list[MarketCandle]:
        """Generate synthetic OHLC candles spaced by the timeframe."""
        self._check_fail_mode("get_candles")
        self._ensure_symbol(symbol)
        minutes = self._timeframe_minutes(timeframe)
        digits = self._digits[symbol]
        step = 10 ** -digits
        now = datetime.now(timezone.utc)
        # limit + 1 prices so every candle has an open (prev close) and a
        # close (next price); the newest candle closes at the live price.
        prices = self._walk_prices(symbol, limit + 1)

        candles: list[MarketCandle] = []
        for index in range(limit):
            open_price = prices[index]
            close_price = prices[index + 1]
            high = round(
                max(open_price, close_price) + random.uniform(0.1, 1.0) * step,
                digits,
            )
            low = round(
                max(
                    min(open_price, close_price) - random.uniform(0.1, 1.0) * step,
                    step * 2.0,
                ),
                digits,
            )
            timestamp = now - timedelta(minutes=minutes * (limit - 1 - index))
            candles.append(
                MarketCandle(
                    provider="mock",
                    symbol=symbol,
                    timeframe=timeframe,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close_price,
                    timestamp=timestamp,
                    volume=round(random.uniform(10.0, 1000.0), 2),
                )
            )
        self._health.record_success("get_candles")
        return candles

    def get_ticks_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[MarketTick]:
        """Return the OLDEST ``limit`` ticks in the inclusive window
        ``[start, end]``, chronological (oldest first).

        Deterministic synthetic generator: the same ``(symbol, start, end,
        limit)`` always yields the same ticks (a seeded random walk at a
        1 tick/s cadence from ``start``), so tests can assert exact values.
        Honors the active fail mode via ``_check_fail_mode``.

        Raises:
            ValueError: ``start > end``, naive datetimes, or ``limit < 1``
                (programming errors — base.py contract rule).
            BrokerError: simulated provider failures.
        """
        self._check_fail_mode("get_ticks_range")
        self._ensure_symbol(symbol)
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware UTC datetimes")
        if start > end:
            raise ValueError("start must be <= end")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        digits = self._digits[symbol]
        step = 10 ** -digits
        seed_text = f"{symbol}|{start.isoformat()}|{end.isoformat()}|{limit}"
        rng = random.Random(hashlib.sha256(seed_text.encode("utf-8")).hexdigest())
        price = self._prices[symbol]
        ticks: list[MarketTick] = []
        for index in range(min(limit, 1000)):
            ts = start + timedelta(seconds=index)
            if ts > end:
                break
            price = round(
                max(price + rng.uniform(-1.0, 1.0) * step * 2.0, step * 2.0),
                digits,
            )
            spread = step * 10
            ticks.append(
                MarketTick(
                    provider="mock",
                    symbol=symbol,
                    bid=price,
                    ask=round(price + spread, digits),
                    last=price,
                    timestamp=ts,
                    volume=round(rng.uniform(0.1, 50.0), 2),
                )
            )
        self._health.record_success("get_ticks_range")
        self._health.record_market_data()
        return ticks

    def get_trade_history(self, limit: int = 50) -> list[BrokerTrade]:
        """Return the most recent ``limit`` trade records."""
        self._check_fail_mode("get_trade_history")
        self._health.record_success("get_trade_history")
        return self._trades[-limit:]

    def get_symbols(self) -> list[BrokerSymbol]:
        """Return the simulated instrument catalog."""
        self._check_fail_mode("get_symbols")
        self._health.record_success("get_symbols")
        return list(self._symbols)

    def subscribe_to_market_data(
        self, symbols: list[str], callback: Callable[[MarketTick], None]
    ) -> str:
        """Start a streaming subscription for the given symbols.

        A daemon thread emits one tick per symbol every 0.5s and invokes
        ``callback(tick)`` until :meth:`disconnect` is called. Returns the
        subscription id.
        """
        self._check_fail_mode("subscribe_to_market_data")
        for symbol in symbols:
            self._ensure_symbol(symbol)

        subscription_id = uuid.uuid4().hex
        self._subscriptions[subscription_id] = callback
        thread = threading.Thread(
            target=self._tick_loop,
            args=(subscription_id, list(symbols)),
            name=f"mock-subscription-{subscription_id[:8]}",
            daemon=True,
        )
        thread.start()
        self._tick_threads.append(thread)
        self._health.record_success("subscribe_to_market_data")
        logger.info(
            "Mock: subscription %s started for symbols %s",
            subscription_id,
            symbols,
        )
        return subscription_id

    def disconnect(self) -> None:
        """Stop all subscriptions, join tick threads and go offline."""
        self._stop_event.set()
        for thread in self._tick_threads:
            thread.join(timeout=2.0)
        self._tick_threads.clear()
        self._subscriptions.clear()
        self._connected = False
        self._health.set_connection_status(ConnectionStatus.DISCONNECTED)
        logger.info("Mock: provider disconnected")

    def get_connection_status(self) -> ConnectionStatus:
        """Return the simulated connection status."""
        return ConnectionStatus.CONNECTED if self._connected else ConnectionStatus.DISCONNECTED

    def health(self) -> ProviderHealth:
        """Return the current provider health snapshot."""
        return self._health.snapshot()


__all__ = ["MockProvider"]
