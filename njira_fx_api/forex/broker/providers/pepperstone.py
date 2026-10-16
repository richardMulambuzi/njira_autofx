"""
Pepperstone provider adapter for the broker/provider abstraction layer.

Implements :class:`TradingProvider` against the Pepperstone / cTrader Open API
through two transports:

* **Query plane** — every read/control operation (account, balance, positions,
  orders, trade history, symbols, market data, cancel/modify) runs in a FRESH
  subprocess via ``forex.pepperstone.query_worker``. This is required because
  the Twisted reactor is one-shot per process (see
  ``forex.pepperstone.execution_engine``); a fresh process may freely own the
  reactor for the duration of a single query.
* **Execution plane** — market orders reuse the existing
  ``TradeExecutor.execute_order`` (also one-shot per process; repeated
  executions require a fresh worker process, matching existing behaviour).

Every provider-facing failure surfaces as :class:`BrokerError` with a
normalized :class:`BrokerErrorCode`; raw SDK/library exceptions are never
leaked to callers.

Credentials come exclusively from the environment (``get_credentials()``).
When they are missing, the provider degrades gracefully: health records
``authentication_status="waiting_for_authentication"`` and the caller receives
``BrokerError(AUTHENTICATION_ERROR, ...)`` — the application never crashes.

Long-running tick streaming is intentionally NOT implemented here: it is
handled by the ``ticks_engine`` management command
(``forex.pepperstone.ticks_engine``). ``subscribe_to_market_data`` only
registers an in-process callback and logs a warning — no reactor is spawned
(one-shot limitation).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
from forex.pepperstone.client import compute_real_price, get_credentials
from forex.pepperstone.execution_engine import TradeExecutor

logger = logging.getLogger("forex.broker.pepperstone")

# Backend root: .../njira_fx_api (parents: providers -> broker -> forex -> root).
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

# Subprocess query timeout (seconds). The worker itself defaults to 15s; this
# is the outer bound including interpreter boot and the cTrader handshake.
# Bumped 90 -> 150 to cover sustained sequential backfill queries that exceed
# 90s (worker reactor timeout is 120s; keep outer bound above it).
_QUERY_TIMEOUT_SECONDS = 150.0

# Timeframe label -> length in minutes (ClickHouse bar aggregation).
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1M": 1,
    "5M": 5,
    "15M": 15,
    "30M": 30,
    "1H": 60,
    "4H": 240,
    "1D": 1440,
}

_ORDER_TYPE_MAP: dict[str, str] = {
    "MARKET": "market",
    "LIMIT": "limit",
    "STOP": "stop",
}

_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "ORDER_STATUS_ACCEPTED": OrderStatus.ACCEPTED,
    "ORDER_STATUS_FILLED": OrderStatus.FILLED,
    "ORDER_STATUS_REJECTED": OrderStatus.REJECTED,
    "ORDER_STATUS_CANCELLED": OrderStatus.CANCELLED,
    "ORDER_STATUS_EXPIRED": OrderStatus.EXPIRED,
}


def _ms_to_dt(value: Any) -> datetime | None:
    """Convert an epoch-millisecond value (int, str, or None) to UTC datetime."""
    if value is None or value == "":
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms == 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _real_price_or_none(raw: Any, digits: int) -> float | None:
    """Convert a raw fixed-point price to a real price, or ``None`` when unset."""
    if raw is None or raw == "":
        return None
    try:
        raw_int = int(raw)
    except (TypeError, ValueError):
        return None
    if raw_int == 0:
        return None
    return compute_real_price(raw_int, digits)


def _map_order_type(raw: str) -> str:
    """Map a cTrader order-type name to the normalized broker label."""
    return _ORDER_TYPE_MAP.get(str(raw or "").upper(), "market")


def _map_order_status(raw: str) -> OrderStatus | None:
    """Map a cTrader order-status name to :class:`OrderStatus`, or ``None``."""
    return _ORDER_STATUS_MAP.get(str(raw or "").upper())


class PepperstoneProvider(TradingProvider):
    """Pepperstone (cTrader) implementation of the unified provider contract.

    Connection is lazy: no socket or reactor is opened at construction time.
    Each query spins up a fresh ``forex.pepperstone.query_worker`` subprocess
    and parses its single JSON line of output.
    """

    name: str = "pepperstone"
    display_name: str = "Pepperstone (cTrader)"

    #: Windowed tick history is implemented (cTrader ``ProtoOAGetTrendbarsReq``
    #: via the ``ticks_range`` query worker) and verified end-to-end against the
    #: live Pepperstone credentials (a real EURUSD history request returned
    #: data). ``True`` lets the BackfillPlanner use this instead of the
    #: ``get_ticks(limit)`` ClickHouse fallback (tick-sync-contracts.md §2.4/§8.2).
    supports_ticks_range: bool = True

    def __init__(
        self,
        config: TradingConfig | None = None,
        mode: TradingMode | None = None,
    ) -> None:
        super().__init__(config=config, mode=mode)
        self._health = ProviderHealthMonitor("pepperstone", self.mode.value)
        self._health.set_connection_status(ConnectionStatus.DISCONNECTED)
        self._subscriptions: dict[str, Callable[[MarketTick], None]] = {}
        self._cached_account_id: str | None = None
        logger.info(
            "PepperstoneProvider initialized (mode=%s) — connection is lazy",
            self.mode.value,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _credentials(self) -> dict:
        """Read Pepperstone credentials from the environment.

        Raises:
            BrokerError(AUTHENTICATION_ERROR): when required env vars are
                missing OR the configured credentials fail the trading config
                validation (e.g. a placeholder access token). Health is updated
                to the "waiting for authentication" state so the API layer can
                degrade gracefully instead of crashing.
        """
        # Config-level validation catches placeholder / missing credentials
        # (e.g. "YOUR_ACCESS_TOKEN") BEFORE any network I/O — the Pepperstone
        # verification delay must never block or hang the rest of the app.
        if self.config is not None:
            valid, errors = self.config.validate_credentials("pepperstone")
            if not valid:
                message = "Pepperstone credentials invalid: " + "; ".join(errors)
                self._health.record_error(message)
                self._health.set_authentication_status("waiting_for_authentication")
                self._health.set_connection_status(
                    ConnectionStatus.AUTHENTICATION_FAILED
                )
                raise BrokerError(
                    BrokerErrorCode.AUTHENTICATION_ERROR,
                    message,
                    provider="pepperstone",
                )
        try:
            creds = get_credentials()
        except RuntimeError as exc:
            message = str(exc)
            self._health.record_error(message)
            self._health.set_authentication_status("waiting_for_authentication")
            self._health.set_connection_status(ConnectionStatus.AUTHENTICATION_FAILED)
            raise BrokerError(
                BrokerErrorCode.AUTHENTICATION_ERROR,
                message,
                provider="pepperstone",
            ) from exc
        # client.py (auth-engineer owned) does not yet know REFRESH_TOKEN, so
        # enforce + forward it here: fail fast naming the key (never the value),
        # attach to the session-builder dict for when client.py learns it.
        # Subprocess workers inherit env, so they see the var automatically.
        refresh = os.environ.get("PEPPERSTONE_REFRESH_TOKEN", "").strip()
        if not refresh or refresh.lower().startswith("your_"):
            message = (
                "Pepperstone credentials invalid: "
                "PEPPERSTONE_REFRESH_TOKEN is missing or empty. "
                "Set it in the environment (or the project .env file) and re-run."
            )
            self._health.record_error(message)
            self._health.set_authentication_status("waiting_for_authentication")
            self._health.set_connection_status(ConnectionStatus.AUTHENTICATION_FAILED)
            raise BrokerError(
                BrokerErrorCode.AUTHENTICATION_ERROR,
                message,
                provider="pepperstone",
            )
        creds["refresh_token"] = refresh
        return creds

    def _account_id(self) -> str:
        """Return the configured account id (env-driven, cached per instance)."""
        if self._cached_account_id is None:
            self._cached_account_id = str(self._credentials()["account_id"])
        return self._cached_account_id

    def _run_query(self, query_type: str, params: dict | None = None) -> dict:
        """Run a query in a fresh subprocess and return its ``data`` payload.

        The worker prints a single JSON line to stdout:
        ``{"ok": true, "data": ...}`` or ``{"ok": false, "error", "code"}``.
        The ``data`` payload is a dict or a list depending on the query type.

        Raises:
            BrokerError: with the worker's normalized code on ``ok: false``,
                or ``NETWORK_ERROR`` when the subprocess times out, fails to
                launch, or returns malformed output.

        Credentials are validated BEFORE spawning the subprocess so a missing
        / placeholder access token fails fast with ``AUTHENTICATION_ERROR``
        ("waiting for authentication") instead of hanging on a dead socket.
        """
        self._credentials()  # graceful auth error when env is unset
        payload = json.dumps(params or {})
        command = [
            sys.executable,
            "-m",
            "forex.pepperstone.query_worker",
            query_type,
            payload,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_QUERY_TIMEOUT_SECONDS,
                cwd=str(_BACKEND_ROOT),
            )
        except subprocess.TimeoutExpired as exc:
            message = (
                f"Pepperstone query '{query_type}' timed out after "
                f"{_QUERY_TIMEOUT_SECONDS:.0f}s"
            )
            self._health.record_error(message)
            raise BrokerError(
                BrokerErrorCode.NETWORK_ERROR, message, provider="pepperstone"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — normalize launch failures
            message = f"Pepperstone query '{query_type}' failed to launch: {exc}"
            self._health.record_error(message)
            raise BrokerError(
                BrokerErrorCode.NETWORK_ERROR, message, provider="pepperstone"
            ) from exc

        stdout = (completed.stdout or "").strip()
        if not stdout:
            stderr = (completed.stderr or "").strip()
            message = f"Pepperstone query '{query_type}' returned no output"
            self._health.record_error(message)
            raise BrokerError(
                BrokerErrorCode.NETWORK_ERROR,
                message,
                provider="pepperstone",
                details={"stderr": stderr[-500:] or None},
            )

        # The worker emits exactly one JSON line; other stdout noise (e.g. the
        # forex package boot banner) precedes it, so take the LAST line.
        try:
            result = json.loads(stdout.splitlines()[-1])
        except (ValueError, IndexError) as exc:
            message = f"Pepperstone query '{query_type}' returned malformed output"
            self._health.record_error(message)
            raise BrokerError(
                BrokerErrorCode.NETWORK_ERROR,
                message,
                provider="pepperstone",
                details={"stdout": stdout[-500:]},
            ) from exc

        if not result.get("ok"):
            code_value = (
                result.get("code")
                or BrokerErrorCode.UNKNOWN_PROVIDER_ERROR.value
            )
            try:
                code = BrokerErrorCode(code_value)
            except ValueError:
                code = BrokerErrorCode.UNKNOWN_PROVIDER_ERROR
            message = result.get("error") or "Unknown Pepperstone error"
            self._health.record_error(f"{query_type}: {message}")
            raise BrokerError(code, message, provider="pepperstone")

        self._health.record_success(query_type)
        self._health.set_api_available(True)
        self._health.set_connection_status(ConnectionStatus.CONNECTED)
        return result.get("data")

    # ------------------------------------------------------------------
    # Account & balance
    # ------------------------------------------------------------------
    def get_account(self) -> BrokerAccount:
        """Return the broker account matching the configured account id."""
        data = self._run_query("account")
        return BrokerAccount(
            provider="pepperstone",
            account_id=str(data.get("ctidTraderAccountId", "")),
            account_type="live" if data.get("isLive") else "demo",
            currency="USD",
            status=AccountStatus.ACTIVE,
            name=f"trader {data.get('traderLogin', '')}".strip(),
        )

    def get_balance(self) -> BrokerBalance:
        """Return the current balance snapshot (equity mirrors balance)."""
        data = self._run_query("balance")
        balance = float(data.get("balance") or 0.0)
        return BrokerBalance(
            provider="pepperstone",
            account_id=str(data.get("ctidTraderAccountId") or self._account_id()),
            balance=balance,
            equity=balance,
            margin=0.0,
            free_margin=balance,
            currency="USD",
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Positions & orders
    # ------------------------------------------------------------------
    def get_positions(self) -> list[BrokerPosition]:
        """Return all positions for the account (raw prices converted to real)."""
        data = self._run_query("positions")
        account_id = self._account_id()
        positions: list[BrokerPosition] = []
        for item in data:
            trade_data = item.get("tradeData") or {}
            symbol = str(item.get("symbol") or trade_data.get("symbolId") or "")
            digits = int(item.get("digits") or 0)
            side_raw = str(trade_data.get("tradeSide") or "")
            side = OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL
            quantity = float(trade_data.get("volume") or 0)
            entry_price = compute_real_price(int(item.get("price") or 0), digits)
            status_raw = str(item.get("positionStatus") or "")
            status = (
                PositionStatus.OPEN
                if status_raw == "POSITION_STATUS_OPEN"
                else PositionStatus.CLOSED
            )
            opened_at = _ms_to_dt(trade_data.get("openTimestamp")) or datetime.now(
                timezone.utc
            )
            closed_at = None
            if status is PositionStatus.CLOSED:
                closed_at = _ms_to_dt(item.get("utcLastUpdateTimestamp"))
            positions.append(
                BrokerPosition(
                    provider="pepperstone",
                    external_position_id=str(item.get("positionId", "")),
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    current_price=entry_price,
                    status=status,
                    opened_at=opened_at,
                    closed_at=closed_at,
                    stop_loss=_real_price_or_none(item.get("stopLoss"), digits),
                    take_profit=_real_price_or_none(item.get("takeProfit"), digits),
                )
            )
        return positions

    def get_orders(self) -> list[BrokerOrder]:
        """Return all orders for the account (working and historical)."""
        data = self._run_query("orders")
        return [self._order_from_data(item) for item in data]

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
        """Place a market order via ``TradeExecutor`` (one-shot per process).

        cTrader volumes are integers in 0.01-lot units (1 == 0.01 lots); any
        non-integer or non-positive volume is rejected with ``ORDER_REJECTED``.
        """
        side_enum = self._normalize_side(side)
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                "symbol is required",
                provider="pepperstone",
            )
        if (order_type or "market").strip().lower() not in {"market", "market_order"}:
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                f"Pepperstone place_order supports market orders only, got "
                f"{order_type!r}",
                provider="pepperstone",
            )
        try:
            volume_value = float(quantity)
        except (TypeError, ValueError):
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                f"volume must be numeric, got {quantity!r}",
                provider="pepperstone",
            ) from None
        if not volume_value.is_integer() or volume_value <= 0:
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                f"volume must be a positive integer in 0.01-lot units "
                f"(1 == 0.01 lots), got {quantity!r}",
                provider="pepperstone",
            )
        volume_int = int(volume_value)

        credentials = self._credentials()  # graceful auth error when env is unset
        result = TradeExecutor().execute_order(
            symbol, side_enum.value.upper(), volume_int
        )
        if result.get("status") != "success":
            message = result.get("message") or "Order execution failed"
            self._health.record_error(f"place_order: {message}")
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED, message, provider="pepperstone"
            )

        position_id = result.get("position_id")
        self._health.record_success("place_order")
        self._health.set_api_available(True)
        self._health.set_connection_status(ConnectionStatus.CONNECTED)
        now = datetime.now(timezone.utc)
        logger.info(
            "Pepperstone market order filled: symbol=%s side=%s volume=%s "
            "position_id=%s",
            symbol,
            side_enum.value,
            volume_int,
            position_id,
        )
        return BrokerOrder(
            provider="pepperstone",
            external_order_id=str(position_id),
            account_id=str(credentials["account_id"]),
            symbol=symbol,
            side=side_enum,
            order_type="market",
            quantity=float(volume_int),
            status=OrderStatus.FILLED,
            created_at=now,
            updated_at=now,
            filled_quantity=float(volume_int),
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def cancel_order(self, external_order_id: str) -> BrokerOrder:
        """Cancel a working order and return its terminal record."""
        try:
            order_id = int(external_order_id)
        except (TypeError, ValueError):
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                f"Invalid order id: {external_order_id!r}",
                provider="pepperstone",
            ) from None
        data = self._run_query("cancel_order", {"orderId": order_id})
        order_data = data.get("order") or {}
        return self._order_from_data(order_data, fallback_status=OrderStatus.CANCELLED)

    def modify_order(
        self,
        external_order_id: str,
        quantity: float | None = None,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> BrokerOrder:
        """Modify a working order's parameters and return the updated record.

        ``price`` is treated as the limit price of a pending limit order (the
        worker converts it to the raw fixed-point value using the order's
        symbol digits). ``quantity`` must be a positive integer in 0.01-lot
        units.
        """
        try:
            order_id = int(external_order_id)
        except (TypeError, ValueError):
            raise BrokerError(
                BrokerErrorCode.ORDER_REJECTED,
                f"Invalid order id: {external_order_id!r}",
                provider="pepperstone",
            ) from None
        params: dict[str, Any] = {"orderId": order_id}
        if quantity is not None:
            try:
                volume_value = float(quantity)
            except (TypeError, ValueError):
                raise BrokerError(
                    BrokerErrorCode.ORDER_REJECTED,
                    f"volume must be numeric, got {quantity!r}",
                    provider="pepperstone",
                ) from None
            if not volume_value.is_integer() or volume_value <= 0:
                raise BrokerError(
                    BrokerErrorCode.ORDER_REJECTED,
                    f"volume must be a positive integer in 0.01-lot units "
                    f"(1 == 0.01 lots), got {quantity!r}",
                    provider="pepperstone",
                )
            params["volume"] = int(volume_value)
        if price is not None:
            params["limitPrice"] = price
        if stop_loss is not None:
            params["stopLoss"] = stop_loss
        if take_profit is not None:
            params["takeProfit"] = take_profit
        data = self._run_query("modify_order", params)
        order_data = data.get("order") or {}
        return self._order_from_data(order_data, fallback_status=OrderStatus.ACCEPTED)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_market_data(self, symbol: str) -> MarketTick:
        """Return the first live spot tick for ``symbol`` (raw -> real price)."""
        symbol = (symbol or "").strip().upper()
        data = self._run_query("market_data", {"symbol": symbol})
        digits = int(data.get("digits") or 0)
        bid = compute_real_price(int(data.get("bid") or 0), digits)
        ask = compute_real_price(int(data.get("ask") or 0), digits)
        timestamp = _ms_to_dt(data.get("timestamp")) or datetime.now(timezone.utc)
        self._health.record_market_data()
        return MarketTick(
            provider="pepperstone",
            symbol=str(data.get("symbol") or symbol),
            bid=bid,
            ask=ask,
            last=bid,
            timestamp=timestamp,
        )

    def get_ticks(self, symbol: str, limit: int = 100) -> list[MarketTick]:
        """Return recent ticks from ClickHouse (best-effort; ``[]`` on failure)."""
        symbol = (symbol or "").strip().upper()
        try:
            from forex.clickhouse.connection import get_clickhouse_client
            from forex.clickhouse.schema import get_recent_ticks

            client = get_clickhouse_client()
            rows = get_recent_ticks(client, symbol, limit)
        except Exception as exc:  # noqa: BLE001 — tick reads are best-effort
            logger.warning("ClickHouse tick read failed for %s: %s", symbol, exc)
            return []
        ticks: list[MarketTick] = []
        for row in rows:
            try:
                timestamp = datetime.fromisoformat(
                    str(row.get("time_str", "")).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                timestamp = datetime.now(timezone.utc)
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
            ticks.append(
                MarketTick(
                    provider="pepperstone",
                    symbol=symbol,
                    bid=bid,
                    ask=ask,
                    last=bid,
                    timestamp=timestamp,
                )
            )
        return ticks

    def get_candles(
        self, symbol: str, timeframe: str, limit: int = 100
    ) -> list[MarketCandle]:
        """Return OHLC candles aggregated from ClickHouse bid ticks."""
        symbol = (symbol or "").strip().upper()
        minutes = self._timeframe_minutes(timeframe)
        try:
            from forex.clickhouse.connection import get_clickhouse_client
            from forex.clickhouse.schema import get_ohlc_bars

            client = get_clickhouse_client()
            rows = get_ohlc_bars(client, symbol, minutes, limit)
        except Exception as exc:  # noqa: BLE001 — OHLC reads are best-effort
            logger.warning("ClickHouse OHLC read failed for %s: %s", symbol, exc)
            return []
        candles: list[MarketCandle] = []
        for row in rows:
            try:
                timestamp = datetime.fromisoformat(
                    str(row.get("time", "")).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                timestamp = datetime.now(timezone.utc)
            candles.append(
                MarketCandle(
                    provider="pepperstone",
                    symbol=symbol,
                    timeframe=timeframe,
                    open=float(row.get("open") or 0.0),
                    high=float(row.get("high") or 0.0),
                    low=float(row.get("low") or 0.0),
                    close=float(row.get("close") or 0.0),
                    timestamp=timestamp,
                )
            )
        return candles

    def get_ticks_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[MarketTick]:
        """Windowed tick history via the cTrader trendbars (history) API.

        Delegates to the ``ticks_range`` query worker, which sends a
        ``ProtoOAGetTrendbarsReq`` and surfaces each 1-minute candle as one
        synthetic tick at its close. Returns ticks in chronological (ascending)
        order, capped at ``limit`` (<=1000), matching the base contract.

        Raises:
            ValueError: ``start > end`` or ``limit < 1`` (programming errors —
                base.py contract rule).
            BrokerError: provider failures, normalized to BrokerErrorCode
                (e.g. ``SYMBOL_NOT_FOUND``, ``NETWORK_ERROR``, ``TOKEN_EXPIRED``).
        """
        if start > end:
            raise ValueError("get_ticks_range: start must be <= end")
        if limit < 1:
            raise ValueError("get_ticks_range: limit must be >= 1")
        symbol = (symbol or "").strip().upper()
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        data = self._run_query(
            "ticks_range",
            {
                "symbol": symbol,
                "start": start_ms,
                "end": end_ms,
                "maxRows": int(limit),
            },
        )
        ticks_data = data.get("ticks", []) if isinstance(data, dict) else []
        ticks: list[MarketTick] = []
        for item in ticks_data:
            timestamp = _ms_to_dt(item.get("epoch")) or datetime.now(timezone.utc)
            ticks.append(
                MarketTick(
                    provider="pepperstone",
                    symbol=symbol,
                    bid=float(item.get("bid") or 0.0),
                    ask=float(item.get("ask") or 0.0),
                    last=float(item.get("last") or 0.0),
                    timestamp=timestamp,
                )
            )
        return ticks

    def get_trade_history(self, limit: int = 50) -> list[BrokerTrade]:
        """Return the most recent closed trades (deals) for the account."""
        data = self._run_query("trade_history", {"maxRows": int(limit)})
        account_id = self._account_id()
        trades: list[BrokerTrade] = []
        for item in data:
            symbol = str(item.get("symbol") or item.get("symbolId") or "")
            digits = int(item.get("digits") or 0)
            side_raw = str(item.get("tradeSide") or "")
            side = OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL
            quantity = float(item.get("volume") or item.get("filledVolume") or 0)
            entry_price = compute_real_price(
                int(item.get("executionPrice") or 0), digits
            )
            opened_at = (
                _ms_to_dt(item.get("executionTimestamp"))
                or _ms_to_dt(item.get("createTimestamp"))
                or datetime.now(timezone.utc)
            )
            trades.append(
                BrokerTrade(
                    provider="pepperstone",
                    external_trade_id=str(item.get("dealId", "")),
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    status=str(item.get("dealStatus") or "FILLED"),
                    opened_at=opened_at,
                )
            )
        return trades

    def get_symbols(self) -> list[BrokerSymbol]:
        """Return the tradable instrument catalog (with volume limits)."""
        data = self._run_query("symbols")
        # data is {"symbols": [...], "count": N} — extract the list
        rows = data.get("symbols", []) if isinstance(data, dict) else data
        symbols: list[BrokerSymbol] = []
        for item in rows:
            name = str(item.get("symbol") or "")
            symbols.append(
                BrokerSymbol(
                    provider="pepperstone",
                    symbol=name,
                    display_name=name,
                    digits=int(item.get("digits") or 0),
                    min_volume=float(item.get("minVolume") or 0.0),
                    max_volume=float(item.get("maxVolume") or 0.0),
                )
            )
        return symbols

    # ------------------------------------------------------------------
    # Lifecycle & health
    # ------------------------------------------------------------------
    def subscribe_to_market_data(
        self, symbols: list[str], callback: Callable[[MarketTick], None]
    ) -> str:
        """Register an in-process subscription callback.

        Long-running Pepperstone streaming is handled by the ``ticks_engine``
        management command — no reactor is spawned here (one-shot limitation),
        so the callback is stored but never invoked by this provider.
        """
        subscription_id = uuid.uuid4().hex
        self._subscriptions[subscription_id] = callback
        logger.warning(
            "Long-running Pepperstone streaming is handled by the ticks_engine "
            "management command; subscription %s registered in-process only "
            "(no reactor spawned — one-shot limitation). Symbols: %s",
            subscription_id,
            symbols,
        )
        return subscription_id

    def disconnect(self) -> None:
        """Tear down the provider: clear subscriptions and go offline."""
        self._subscriptions.clear()
        self._health.set_connection_status(ConnectionStatus.DISCONNECTED)
        logger.info("Pepperstone provider disconnected (no persistent session)")

    def get_connection_status(self) -> ConnectionStatus:
        """Return the current connection status from the health monitor."""
        raw = self._health.snapshot().connection_status
        try:
            return ConnectionStatus(raw)
        except ValueError:
            return ConnectionStatus.ERROR

    def health(self) -> ProviderHealth:
        """Return the current health snapshot."""
        return self._health.snapshot()

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------
    def _timeframe_minutes(self, timeframe: str) -> int:
        """Map a timeframe label (1M/5M/15M/30M/1H/4H/1D) to minutes."""
        minutes = _TIMEFRAME_MINUTES.get((timeframe or "").upper())
        if minutes is None:
            raise BrokerError(
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
                f"Unsupported timeframe {timeframe!r} (expected one of "
                f"{', '.join(_TIMEFRAME_MINUTES)})",
                provider="pepperstone",
            )
        return minutes

    def _order_from_data(
        self, data: dict, fallback_status: OrderStatus | None = None
    ) -> BrokerOrder:
        """Map a worker-serialized order dict to a :class:`BrokerOrder`."""
        trade_data = data.get("tradeData") or {}
        symbol = str(data.get("symbol") or trade_data.get("symbolId") or "")
        digits = int(data.get("digits") or 0)
        side_raw = str(trade_data.get("tradeSide") or "")
        side = OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL
        order_type = _map_order_type(str(data.get("orderType") or ""))
        status = (
            _map_order_status(str(data.get("orderStatus") or ""))
            or fallback_status
            or OrderStatus.PENDING
        )
        quantity = float(trade_data.get("volume") or 0)
        filled_quantity = float(data.get("executedVolume") or 0)
        updated_at = _ms_to_dt(data.get("utcLastUpdateTimestamp")) or datetime.now(
            timezone.utc
        )
        raw_price = (
            data.get("limitPrice")
            or data.get("stopPrice")
            or data.get("executionPrice")
        )
        return BrokerOrder(
            provider="pepperstone",
            external_order_id=str(data.get("orderId") or ""),
            account_id=self._account_id(),
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            status=status,
            created_at=updated_at,
            updated_at=updated_at,
            filled_quantity=filled_quantity,
            price=_real_price_or_none(raw_price, digits),
            stop_loss=_real_price_or_none(data.get("stopLoss"), digits),
            take_profit=_real_price_or_none(data.get("takeProfit"), digits),
        )


__all__ = ["PepperstoneProvider"]
