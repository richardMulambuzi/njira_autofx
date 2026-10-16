"""
The unified TradingProvider interface.

Every broker provider (pepperstone, mock) implements this ABC so the
rest of the application can trade against any provider through one contract.

Contract rules:
* All public methods raise :class:`BrokerError` on failure — raw SDK/library
  exceptions are caught and normalized inside each provider.
* All methods return the normalized value objects from ``forex.broker.models``.
* ``ValueError`` is reserved for programming errors (bad arguments); provider
  failures are always ``BrokerError``.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable

from forex.broker.config import TradingConfig
from forex.broker.errors import BrokerError, BrokerErrorCode
from forex.broker.health import ProviderHealth
from forex.broker.models import (
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
    TradingMode,
)

logger = logging.getLogger("forex.broker.base")

# Valid timeframe labels for get_candles().
CANDLE_TIMEFRAMES = ("1M", "5M", "15M", "30M", "1H", "4H", "1D")


class TradingProvider(ABC):
    """Abstract base class for broker providers.

    Class attributes (set by subclasses):
        name: Canonical provider key (matches ``ProviderName`` values).
        display_name: Human-readable provider name.

    Instance attributes (set in ``__init__``):
        mode: Execution mode (demo/paper/live).
        config: The :class:`TradingConfig` this provider was created with.
    """

    name: str
    display_name: str

    #: True when the provider supports windowed tick history
    #: (``get_ticks_range``). Providers that do not support it inherit the
    #: default implementation, which raises ``NotImplementedError``.
    supports_ticks_range: bool = False

    def __init__(
        self,
        config: TradingConfig | None = None,
        mode: TradingMode | None = None,
    ) -> None:
        self.config = config
        self.mode = mode or (config.mode if config is not None else TradingMode.DEMO)

    # ------------------------------------------------------------------
    # Account & balance
    # ------------------------------------------------------------------
    @abstractmethod
    def get_account(self) -> BrokerAccount:
        """Return the currently authenticated broker account."""

    @abstractmethod
    def get_balance(self) -> BrokerBalance:
        """Return the current account balance snapshot."""

    # ------------------------------------------------------------------
    # Positions & orders
    # ------------------------------------------------------------------
    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        """Return all open positions for the account."""

    @abstractmethod
    def get_orders(self) -> list[BrokerOrder]:
        """Return all working (non-terminal) orders for the account."""

    @abstractmethod
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
        """Place an order (market/limit/stop) and return the broker's record."""

    @abstractmethod
    def cancel_order(self, external_order_id: str) -> BrokerOrder:
        """Cancel a working order and return its terminal record."""

    @abstractmethod
    def modify_order(
        self,
        external_order_id: str,
        quantity: float | None = None,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> BrokerOrder:
        """Modify a working order's parameters and return the updated record."""

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    @abstractmethod
    def get_market_data(self, symbol: str) -> MarketTick:
        """Return the latest tick for a symbol."""

    @abstractmethod
    def get_ticks(self, symbol: str, limit: int = 100) -> list[MarketTick]:
        """Return recent ticks for a symbol (newest first)."""

    @abstractmethod
    def get_candles(
        self, symbol: str, timeframe: str, limit: int = 100
    ) -> list[MarketCandle]:
        """Return OHLC candles.

        Timeframe labels: 1M, 5M, 15M, 30M, 1H, 4H, 1D.
        """

    # ------------------------------------------------------------------
    # Windowed tick history (optional capability)
    # ------------------------------------------------------------------
    def get_ticks_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[MarketTick]:
        """Return the OLDEST ``limit`` ticks in the inclusive window
        ``[start, end]``, in chronological (ascending-timestamp) order.

        Window semantics (tick-sync-contracts.md §2.1):
        * Candidates are ticks ``t`` with ``start <= t.timestamp <= end``
          (inclusive on both bounds).
        * Returns at most ``limit`` ticks: the OLDEST ticks in the window.
          A short page means "no more data in window".
        * Ordering is chronological (oldest first). ``ts[i] <= ts[i+1]``.

        Caller-driven paging: when ``len(result) == limit``, advance
        ``next_start = result[-1].timestamp + 1µs`` and call again. Reverse
        walks shrink ``end`` to ``result[0].timestamp - 1µs``.

        Raises:
            NotImplementedError: when the provider does not support windowed
                tick history (``supports_ticks_range`` is False).
            ValueError: ``start > end``, naive datetimes, or ``limit < 1``
                (programming errors — base.py contract rule).
            BrokerError: provider failures, normalized to BrokerErrorCode.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support get_ticks_range "
            "(supports_ticks_range=False)"
        )

    @abstractmethod
    def get_trade_history(self, limit: int = 50) -> list[BrokerTrade]:
        """Return recent closed trades for the account."""

    @abstractmethod
    def get_symbols(self) -> list[BrokerSymbol]:
        """Return the tradable instrument list."""

    @abstractmethod
    def subscribe_to_market_data(
        self, symbols: list[str], callback: Callable[[MarketTick], None]
    ) -> str:
        """Subscribe to real-time ticks; returns a subscription id."""

    # ------------------------------------------------------------------
    # Lifecycle & health
    # ------------------------------------------------------------------
    @abstractmethod
    def disconnect(self) -> None:
        """Tear down the provider session (close sockets, cancel subscriptions)."""

    @abstractmethod
    def get_connection_status(self) -> ConnectionStatus:
        """Return the current connection status."""

    @abstractmethod
    def health(self) -> ProviderHealth:
        """Return the current health snapshot."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _normalize_side(self, side: OrderSide | str) -> OrderSide:
        """Coerce an OrderSide or its string form into an OrderSide.

        Raises:
            BrokerError(UNKNOWN_PROVIDER_ERROR): for an unrecognized side
                string (a caller/contract error surfaced through the broker
                error channel).
        """
        if isinstance(side, OrderSide):
            return side
        if isinstance(side, str):
            try:
                return OrderSide(side.strip().lower())
            except ValueError:
                pass
        raise BrokerError(
            BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            f"Invalid order side: {side!r} (expected 'buy' or 'sell')",
        )


__all__ = ["CANDLE_TIMEFRAMES", "TradingProvider"]