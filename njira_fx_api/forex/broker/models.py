"""
Normalized broker domain models (dataclasses, NOT Django models).

These are provider-agnostic value objects that every provider implementation
(pepperstone, mock) converts its native SDK types into. They are plain
frozen dataclasses so they can be passed across process boundaries (queues,
caching, API serialization) without ORM overhead.

Every record carries a ``provider`` field as its FIRST field so records are
provider-aware and can be traced back to their origin.

NOTE on field ordering: dataclass rules require all fields with defaults to
appear AFTER fields without defaults. Where the requested field list mixed
defaulted and non-defaulted fields, the non-defaulted fields were moved first
(types and names are unchanged).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

logger = logging.getLogger("forex.broker.models")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class ProviderName(str, Enum):
    """Supported trading providers."""

    PEPPERSTONE = "pepperstone"
    MOCK = "mock"


class TradingMode(str, Enum):
    """Execution environment for a provider session."""

    DEMO = "demo"
    PAPER = "paper"
    LIVE = "live"


class OrderSide(str, Enum):
    """Direction of an order or position."""

    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """Lifecycle state of an order."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PositionStatus(str, Enum):
    """Lifecycle state of a position."""

    OPEN = "open"
    CLOSED = "closed"


class ConnectionStatus(str, Enum):
    """Provider connection lifecycle states."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    AUTHENTICATION_FAILED = "authentication_failed"
    ERROR = "error"


class AccountStatus(str, Enum):
    """Availability of the broker account backing the session."""

    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    NOT_AUTHENTICATED = "not_authenticated"
    SUSPENDED = "suspended"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BrokerAccount:
    """A broker account descriptor."""

    provider: str
    account_id: str
    account_type: str  # "demo" | "live" | "paper"
    currency: str
    status: AccountStatus
    name: str = ""
    leverage: float = 0.0


@dataclass(frozen=True)
class BrokerBalance:
    """A point-in-time balance snapshot."""

    provider: str
    account_id: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    currency: str
    timestamp: datetime


@dataclass(frozen=True)
class BrokerPosition:
    """An open or closed position."""

    provider: str
    external_position_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    current_price: float
    status: PositionStatus
    opened_at: datetime
    closed_at: datetime | None = None
    realized_pnl: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True)
class BrokerOrder:
    """A working or historical order.

    NOTE: ``status`` / ``created_at`` / ``updated_at`` (non-defaulted) were
    moved ahead of ``filled_quantity`` / ``price`` (defaulted) to satisfy
    dataclass field-ordering rules.
    """

    provider: str
    external_order_id: str
    account_id: str
    symbol: str
    side: OrderSide
    order_type: str  # "market" | "limit" | "stop"
    quantity: float
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    filled_quantity: float = 0.0
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True)
class BrokerTrade:
    """A closed (or closing) trade — the profit/loss record.

    NOTE: ``status`` / ``opened_at`` (non-defaulted) were moved ahead of the
    defaulted fields to satisfy dataclass field-ordering rules.
    """

    provider: str
    external_trade_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    status: str
    opened_at: datetime
    exit_price: float | None = None
    realized_pnl: float = 0.0
    closed_at: datetime | None = None


@dataclass(frozen=True)
class BrokerSymbol:
    """Instrument metadata."""

    provider: str
    symbol: str
    display_name: str
    digits: int
    market: str = ""
    min_volume: float = 0.0
    max_volume: float = 0.0
    tick_size: float = 0.0


@dataclass(frozen=True)
class MarketTick:
    """A single real-time price tick.

    NOTE: ``timestamp`` (non-defaulted) was moved ahead of ``volume``
    (defaulted) to satisfy dataclass field-ordering rules.
    """

    provider: str
    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: datetime
    volume: float = 0.0


@dataclass(frozen=True)
class MarketCandle:
    """A single OHLC candle.

    NOTE: ``timestamp`` (non-defaulted) was moved ahead of ``volume``
    (defaulted) to satisfy dataclass field-ordering rules.
    """

    provider: str
    symbol: str
    timeframe: str  # "1M" | "5M" | "15M" | "30M" | "1H" | "4H" | "1D"
    open: float
    high: float
    low: float
    close: float
    timestamp: datetime
    volume: float = 0.0


__all__ = [
    "AccountStatus",
    "BrokerAccount",
    "BrokerBalance",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerSymbol",
    "BrokerTrade",
    "ConnectionStatus",
    "MarketCandle",
    "MarketTick",
    "OrderSide",
    "OrderStatus",
    "PositionStatus",
    "ProviderName",
    "TradingMode",
]
