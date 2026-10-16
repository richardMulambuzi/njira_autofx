"""
Pepperstone / cTrader subprocess query worker.

Runnable as ``python -m forex.pepperstone.query_worker <query_type> <json_params>``
from the backend root. Each invocation runs in a FRESH process so it may freely
own the Twisted reactor — which is a one-shot resource per process (see
``forex.pepperstone.execution_engine`` for the documented limitation).

Flow
----
1. Read credentials via ``get_credentials()`` (env-driven, never logged).
2. Build a :class:`PepperstoneSession` (subclassed to capture full symbol
   details and raw error codes) and run the standard auth + symbol handshake.
3. On ``on_ready`` send the query-specific request and wait for the matching
   response payload type.
4. Print a SINGLE JSON line to stdout, then stop the reactor.

Output contract (exit code 0 either way):
    success: {"ok": true, "data": <json-serializable>}
    failure: {"ok": false, "error": "<message>", "code": "<BROKER_ERROR_CODE>"}

Supported query types
---------------------
=====================  =====================================================
``account``            ``ProtoOAGetAccountListByAccessTokenReq`` -> Res; the
                       account matching ``PEPPERSTONE_ACCOUNT_ID`` is returned
``balance``            ``ProtoOATraderReq`` -> ``ProtoOATraderRes``
``positions``          ``ProtoOAReconcileReq`` -> ``ProtoOAReconcileRes``
                       (position list, enriched with symbol + digits)
``orders``             ``ProtoOAReconcileReq`` -> ``ProtoOAReconcileRes``
                       (order list, enriched with symbol + digits)
``trade_history``      ``ProtoOADealListReq`` -> ``ProtoOADealListRes``
                       (``maxRows`` from params, default 50)
``symbols``            handshake ``symbols_map`` + full ``ProtoOASymbol``
                       details (minVolume / maxVolume)
``market_data``        subscribe spots for the symbol -> first
                       ``ProtoOASpotEvent`` (bid, ask, timestamp, symbolId)
``cancel_order``       ``ProtoOACancelOrderReq`` -> ``ProtoOAExecutionEvent``
                       (ORDER_CANCELLED) or ``ProtoOAOrderErrorEvent``
``modify_order``       ``ProtoOAAmendOrderReq`` -> ``ProtoOAExecutionEvent``
                       (ORDER_ACCEPTED / ORDER_FILLED) or
                       ``ProtoOAOrderErrorEvent``. The order is reconciled
                       first so its symbol digits can convert real prices to
                       raw fixed-point values before amending.
``close_position``     ``ProtoOAClosePositionReq`` -> ``ProtoOAExecutionEvent``
                       or ``ProtoOAOrderErrorEvent``
``ticks_range``        ``ProtoOAGetTrendbarsReq`` -> ``ProtoOAGetTrendbarsRes``
                        (the cTrader history API). It returns trendbars
                        (``ProtoOATrendbar``), each surfaced as one synthetic
                        tick at its close plus full OHLCV. Input contract:
                        ``{symbol, start (epoch ms), end (epoch ms),
                        maxRows (<=1000), optional period (M1 default),
                        optional spread}``; output:
                        ``{ok: true, data: {ticks: [{epoch, bid, ask, last,
                        open, high, low, volume}]}}`` (chronological). NOTE:
                        cTrader caps a response at 1000 bars — a wider range
                        must be paged by the caller.

=====================  =====================================================

Error normalization
-------------------
cTrader error codes are mapped to :class:`BrokerErrorCode` values:
``CH_ACCESS_TOKEN_INVALID`` / ``OA_AUTH_TOKEN_EXPIRED`` -> ``TOKEN_EXPIRED``;
``CH_CLIENT_AUTH_FAILURE`` / ``ACCOUNT_NOT_AUTHORIZED`` -> 
``AUTHENTICATION_ERROR``; ``CH_CTID_TRADER_ACCOUNT_NOT_FOUND`` ->
``ACCOUNT_NOT_FOUND``; anything else -> ``UNKNOWN_PROVIDER_ERROR``. Network /
connection failures (disconnects, timeouts) map to ``NETWORK_ERROR``.

Secrets: only environment variable NAMES are ever logged — never values.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from google.protobuf.json_format import MessageToDict

from ctrader_open_api.protobuf import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAmendOrderReq,
    ProtoOACancelOrderReq,
    ProtoOAClosePositionReq,
    ProtoOADealListReq,
    ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetTrendbarsReq,
    ProtoOAOrderErrorEvent,
    ProtoOAReconcileReq,
    ProtoOATraderReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAPayloadType,
    ProtoOATrendbarPeriod,
)

from forex.broker.errors import BrokerErrorCode
from forex.pepperstone.client import (
    PepperstoneSession,
    get_credentials,
    price_scale,
)

logger = logging.getLogger("forex.pepperstone.query_worker")

DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_DEAL_WINDOW_DAYS = 30
MAX_DEAL_ROWS = 1000

# ---------------------------------------------------------------------------
# Error-code normalization
# ---------------------------------------------------------------------------
_AUTH_TOKEN_CODES = frozenset({"CH_ACCESS_TOKEN_INVALID", "OA_AUTH_TOKEN_EXPIRED"})
_AUTHENTICATION_CODES = frozenset(
    {"CH_CLIENT_AUTH_FAILURE", "CH_CLIENT_NOT_AUTHENTICATED", "ACCOUNT_NOT_AUTHORIZED"}
)
_ACCOUNT_NOT_FOUND_CODES = frozenset({"CH_CTID_TRADER_ACCOUNT_NOT_FOUND"})


def _map_error_code(code) -> BrokerErrorCode:
    """Map a cTrader error-code string to a normalized :class:`BrokerErrorCode`."""
    normalized = str(code or "").strip().upper()
    if normalized in _AUTH_TOKEN_CODES:
        return BrokerErrorCode.TOKEN_EXPIRED
    if normalized in _AUTHENTICATION_CODES:
        return BrokerErrorCode.AUTHENTICATION_ERROR
    if normalized in _ACCOUNT_NOT_FOUND_CODES:
        return BrokerErrorCode.ACCOUNT_NOT_FOUND
    return BrokerErrorCode.UNKNOWN_PROVIDER_ERROR


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def _serialize(message) -> dict:
    """Serialize a protobuf message to a plain dict (snake_case keys).

    int64 fields (ids, volumes, timestamps, raw prices) serialize as strings
    to avoid JSON precision loss — callers must coerce with ``int()`` /
    ``float()``.
    """
    return MessageToDict(message, preserving_proto_field_name=True)


def _real_to_raw(real_price: float, digits: int) -> int:
    """Convert a real price to the cTrader raw fixed-point integer."""
    return int(round(real_price * (10 ** max(int(digits), 0))))


# ---------------------------------------------------------------------------
# Session subclass
# ---------------------------------------------------------------------------
class _QuerySession(PepperstoneSession):
    """PepperstoneSession subclass capturing full symbol details and raw error codes.

    * ``symbol_details`` maps symbolId -> ``ProtoOASymbol`` so the worker can
      expose ``minVolume`` / ``maxVolume`` without re-requesting details.
    * ``last_error_code`` keeps the raw cTrader error code so ``on_error`` can
      normalize it into a :class:`BrokerErrorCode`.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.symbol_details: dict[int, Any] = {}
        self.last_error_code: Optional[str] = None

    def _on_symbol_by_id_res(self, client: object, response) -> None:
        for symbol in response.symbol:
            self.symbol_details[symbol.symbolId] = symbol
        super()._on_symbol_by_id_res(client, response)

    def _on_error_res(self, error, client: object = None) -> None:
        self.last_error_code = str(error.errorCode or "")
        super()._on_error_res(error, client=client)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class QueryWorker:
    """Runs one cTrader query inside a fresh process and emits a single JSON line."""

    def __init__(
        self,
        query_type: str,
        params: Optional[dict] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.query_type = query_type
        self.params: dict = params or {}
        self.timeout = float(timeout)
        if self.timeout < 1.0:
            raise ValueError(f"timeout must be >= 1s, got {timeout!r}")
        # RuntimeError (missing credentials) propagates to main().
        self.credentials: dict = get_credentials()
        self.account_id: int = int(self.credentials["account_id"])

        self.session: Optional[_QuerySession] = None
        self.reactor = None
        self.timeout_call = None
        self.result_emitted = False
        self._pending_modify: Optional[dict] = None

    # -- lifecycle ----------------------------------------------------------
    def run(self) -> None:
        """Start the session and run the reactor until a result or timeout."""
        from twisted.internet import reactor

        self.reactor = reactor
        self.session = _QuerySession(
            self.credentials,
            on_ready=self._on_ready,
            on_message=self._on_message,
            on_error=self._on_error,
            on_disconnected=self._on_disconnected,
        )
        self.session.start()
        self.timeout_call = reactor.callLater(self.timeout, self._on_timeout)
        reactor.run(installSignalHandlers=False)

    def stop(self) -> None:
        """Stop the session and the reactor (safe from any thread)."""
        if self.timeout_call is not None and self.timeout_call.active():
            self.timeout_call.cancel()
        if self.session is not None:
            try:
                self.session.stop()
            except Exception:  # noqa: BLE001 — best-effort stop
                pass
        if self.reactor is not None:
            try:
                self.reactor.stop()
            except Exception:  # noqa: BLE001 — best-effort stop
                pass

    # -- result emission ----------------------------------------------------
    def _succeed(self, data) -> None:
        if self.result_emitted:
            return
        self.result_emitted = True
        print(json.dumps({"ok": True, "data": data}, default=str), flush=True)
        self.stop()

    def _fail(
        self,
        message: str,
        code: BrokerErrorCode,
        code_value: Optional[str] = None,
    ) -> None:
        """Emit a normalized failure line and stop.

        ``code_value`` overrides the wire ``code`` when the failure is a
        capability signal that has no :class:`BrokerErrorCode` equivalent
        (e.g. ``not_implemented`` for the ``ticks_range`` stub) — the
        ``code`` argument still drives logging.
        """
        if self.result_emitted:
            return
        self.result_emitted = True
        logger.error("Query %s failed: %s", self.query_type, message)
        print(
            json.dumps(
                {"ok": False, "error": message, "code": code_value or code.value},
                default=str,
            ),
            flush=True,
        )
        self.stop()

    # -- session callbacks --------------------------------------------------
    def _on_ready(self, symbols_map: dict) -> None:
        try:
            self._dispatch_query()
        except Exception as exc:  # noqa: BLE001 — surface any dispatch failure
            logger.exception(
                "Query dispatch failed for %s: %s", self.query_type, exc
            )
            self._fail(
                f"Query {self.query_type} failed: {exc}",
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            )

    def _on_message(self, client: object, message: object) -> None:
        payload_type = message.payloadType
        try:
            if (
                payload_type
                == ProtoOAPayloadType.PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_RES
            ):
                self._handle_account_list(Protobuf.extract(message))
            elif payload_type == ProtoOAPayloadType.PROTO_OA_TRADER_RES:
                self._handle_trader(Protobuf.extract(message))
            elif payload_type == ProtoOAPayloadType.PROTO_OA_RECONCILE_RES:
                self._handle_reconcile(Protobuf.extract(message))
            elif payload_type == ProtoOAPayloadType.PROTO_OA_DEAL_LIST_RES:
                self._handle_deal_list(Protobuf.extract(message))
            elif payload_type == ProtoOAPayloadType.PROTO_OA_SPOT_EVENT:
                self._handle_spot_event(Protobuf.extract(message))
            elif payload_type == ProtoOAPayloadType.PROTO_OA_EXECUTION_EVENT:
                self._handle_execution_event(Protobuf.extract(message))
            elif payload_type == ProtoOAPayloadType.PROTO_OA_ORDER_ERROR_EVENT:
                self._handle_order_error(Protobuf.extract(message))
            elif payload_type == ProtoOAPayloadType.PROTO_OA_GET_TRENDBARS_RES:
                self._handle_history_period(Protobuf.extract(message))
        except Exception as exc:  # noqa: BLE001 — one bad message must not hang
            logger.exception(
                "Error handling message payloadType=%s: %s", payload_type, exc
            )
            self._fail(
                f"Unexpected error processing payload type {payload_type}: {exc}",
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            )

    def _on_error(self, message: str) -> None:
        code = BrokerErrorCode.UNKNOWN_PROVIDER_ERROR
        if self.session is not None and self.session.last_error_code:
            code = _map_error_code(self.session.last_error_code)
        self._fail(message, code)

    def _on_disconnected(self, client: object, reason: object) -> None:
        if self.result_emitted:
            return
        reason_text = (
            reason.getErrorMessage() if hasattr(reason, "getErrorMessage") else ""
        )
        logger.warning(
            "Disconnected before the %s query completed: %s",
            self.query_type,
            reason_text or "unknown",
        )
        self._fail(
            "Connection lost before the query completed",
            BrokerErrorCode.NETWORK_ERROR,
        )

    def _on_timeout(self) -> None:
        if not self.result_emitted:
            self._fail(
                f"Timed out waiting for the {self.query_type} response after "
                f"{self.timeout:.1f}s",
                BrokerErrorCode.NETWORK_ERROR,
            )

    # -- query dispatch -----------------------------------------------------
    def _dispatch_query(self) -> None:
        if self.query_type == "account":
            self._send_account_list()
        elif self.query_type == "balance":
            self._send_trader()
        elif self.query_type == "positions":
            self._send_reconcile()
        elif self.query_type == "orders":
            self._send_reconcile()
        elif self.query_type == "trade_history":
            self._send_deal_list()
        elif self.query_type == "symbols":
            self._emit_symbols()
        elif self.query_type == "market_data":
            self._subscribe_market_data()
        elif self.query_type == "cancel_order":
            self._send_cancel_order()
        elif self.query_type == "modify_order":
            self._send_modify_order()
        elif self.query_type == "close_position":
            self._send_close_position()
        elif self.query_type == "ticks_range":
            self._send_history_period()
        else:
            self._fail(
                f"Unknown query type: {self.query_type!r}",
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            )

    # -- request senders ----------------------------------------------------
    def _send_account_list(self) -> None:
        request = ProtoOAGetAccountListByAccessTokenReq(
            accessToken=self.credentials["access_token"]
        )
        self.session.client.send(request)
        logger.info("Sent ProtoOAGetAccountListByAccessTokenReq")

    def _send_trader(self) -> None:
        request = ProtoOATraderReq(ctidTraderAccountId=self.account_id)
        self.session.client.send(request)
        logger.info("Sent ProtoOATraderReq for account %s", self.account_id)

    def _send_reconcile(self) -> None:
        request = ProtoOAReconcileReq(ctidTraderAccountId=self.account_id)
        self.session.client.send(request)
        logger.info("Sent ProtoOAReconcileReq for account %s", self.account_id)

    def _send_deal_list(self) -> None:
        try:
            max_rows = max(1, min(int(self.params.get("maxRows", 50)), MAX_DEAL_ROWS))
        except (TypeError, ValueError):
            max_rows = 50
        try:
            window_days = max(
                1, int(self.params.get("windowDays", DEFAULT_DEAL_WINDOW_DAYS))
            )
        except (TypeError, ValueError):
            window_days = DEFAULT_DEAL_WINDOW_DAYS
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        from_ms = now_ms - window_days * 24 * 3600 * 1000
        request = ProtoOADealListReq(
            ctidTraderAccountId=self.account_id,
            fromTimestamp=from_ms,
            toTimestamp=now_ms,
            maxRows=max_rows,
        )
        self.session.client.send(request)
        logger.info(
            "Sent ProtoOADealListReq (maxRows=%d, windowDays=%d)",
            max_rows,
            window_days,
        )

    def _emit_symbols(self) -> None:
        """Emit the full symbol catalogue from the session's symbols_map."""
        symbols_map = self.session.symbols_map  # {name: (id, digits)}
        symbols_list = []
        for name, (symbol_id, digits) in symbols_map.items():
            symbols_list.append({
                "symbol": name,
                "symbolId": symbol_id,
                "digits": digits,
            })
        self._succeed({"symbols": symbols_list, "count": len(symbols_list)})

    def _subscribe_market_data(self) -> None:
        symbol = str(self.params.get("symbol", "")).strip().upper()
        info = self.session.symbols_map.get(symbol)
        if info is None:
            self._fail(
                f"Symbol {symbol!r} not found in the Pepperstone account symbol list",
                BrokerErrorCode.SYMBOL_NOT_FOUND,
            )
            return
        symbol_id, _digits = info
        self.session.subscribe_spots([symbol_id])
        logger.info(
            "Subscribed to spot updates for %s (symbolId=%s)", symbol, symbol_id
        )

    def _send_cancel_order(self) -> None:
        try:
            order_id = int(self.params.get("orderId"))
        except (TypeError, ValueError):
            self._fail(
                f"Invalid orderId param: {self.params.get('orderId')!r}",
                BrokerErrorCode.ORDER_REJECTED,
            )
            return
        request = ProtoOACancelOrderReq(
            ctidTraderAccountId=self.account_id, orderId=order_id
        )
        self.session.client.send(request)
        logger.info("Sent ProtoOACancelOrderReq for order %s", order_id)

    def _send_modify_order(self) -> None:
        try:
            order_id = int(self.params.get("orderId"))
        except (TypeError, ValueError):
            self._fail(
                f"Invalid orderId param: {self.params.get('orderId')!r}",
                BrokerErrorCode.ORDER_REJECTED,
            )
            return
        self._pending_modify = {
            "orderId": order_id,
            "volume": self.params.get("volume"),
            "limitPrice": self.params.get("limitPrice"),
            "stopPrice": self.params.get("stopPrice"),
            "stopLoss": self.params.get("stopLoss"),
            "takeProfit": self.params.get("takeProfit"),
        }
        # Reconcile first so the order's symbol digits can convert the real
        # prices supplied by the caller into raw fixed-point values.
        self._send_reconcile()

    def _send_close_position(self) -> None:
        try:
            position_id = int(self.params.get("positionId"))
        except (TypeError, ValueError):
            self._fail(
                f"Invalid positionId param: {self.params.get('positionId')!r}",
                BrokerErrorCode.ORDER_REJECTED,
            )
            return
        try:
            volume = int(self.params.get("volume", 0))
        except (TypeError, ValueError):
            volume = 0
        request = ProtoOAClosePositionReq(
            ctidTraderAccountId=self.account_id,
            positionId=position_id,
            volume=volume,
        )
        self.session.client.send(request)
        logger.info(
            "Sent ProtoOAClosePositionReq for position %s (volume=%s)",
            position_id,
            volume,
        )

    def _send_history_period(self) -> None:
        """Send a cTrader history (trendbars) request for the ticks_range query.

        cTrader's history API (``ProtoOAGetTrendbarsReq``) returns 1-minute
        candles (``ProtoOATrendbar``), not raw ticks. We request the finest
        granularity (M1) over the requested window and surface each candle as a
        single synthetic tick at its close (see ``_handle_history_period``).

        Input contract: ``{symbol, start (epoch ms), end (epoch ms),
        maxRows (<=1000), optional spread}``.
        """
        symbol = str(self.params.get("symbol", "")).strip().upper()
        info = self.session.symbols_map.get(symbol)
        if info is None:
            self._fail(
                f"Symbol {symbol!r} not found in the Pepperstone account symbol list",
                BrokerErrorCode.SYMBOL_NOT_FOUND,
            )
            return
        symbol_id, _digits = info
        try:
            start_ms = int(self.params.get("start"))
            end_ms = int(self.params.get("end"))
            max_rows = max(1, min(int(self.params.get("maxRows", 1000)), 1000))
        except (TypeError, ValueError) as exc:
            self._fail(
                f"Invalid ticks_range params (start/end/maxRows): {exc}",
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            )
            return
        # Optional timeframe (M1/M5/M15/M30/H1/H4/H12/D1/W1/MN1); M1 keeps the
        # original contract. cTrader caps a response at 1000 bars, so a wide
        # range is paged backwards by the caller.
        period_label = str(self.params.get("period", "M1")).strip().upper()
        period = getattr(ProtoOATrendbarPeriod, period_label, None)
        if period is None:
            self._fail(
                f"Unknown trendbar period {period_label!r}",
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            )
            return
        # cTrader trendbars: the request window (fromTimestamp/toTimestamp) is
        # in Unix MILLISECONDS (per cTrader Open API docs), while each returned
        # bar's utcTimestampInMinutes is in minutes. So we pass the window in ms
        # and convert the bar timestamps back to ms on the way out.
        request = ProtoOAGetTrendbarsReq(
            ctidTraderAccountId=self.account_id,
            symbolId=symbol_id,
            fromTimestamp=start_ms,
            toTimestamp=end_ms,
            period=period,
            count=max_rows,
        )
        self.session.client.send(request)
        logger.info(
            "Sent ProtoOAGetTrendbarsReq for %s (symbolId=%s, period=%s, count=%d)",
            symbol,
            symbol_id,
            period_label,
            max_rows,
        )

    def _handle_history_period(self, response) -> None:
        """Convert a ``ProtoOAGetTrendbarsRes`` into synthetic ticks.

        Each ``ProtoOATrendbar`` carries OHLC as ``low`` plus deltas
        (``deltaOpen`` / ``deltaClose`` / ``deltaHigh``) in raw fixed-point
        form. We reconstruct the real close price with the symbol's digit
        precision and surface one tick per candle:

        * ``epoch`` = ``utcTimestampInMinutes * 60000`` (ms)
        * ``last``  = close
        * ``bid``   = close - spread/2
        * ``ask``   = close + spread/2

        ``spread`` defaults to 0 (so bid == ask == close) because OHLC carries
        no bid/ask spread; callers may pass ``spread`` (price units) to apply a
        symmetric spread. Ticks are returned in chronological (ascending) order
        per the ``get_ticks_range`` contract.
        """
        symbol_id = int(response.symbolId) if response.symbolId else 0
        name, digits = self._symbol_info(symbol_id)
        try:
            spread = float(self.params.get("spread") or 0.0)
        except (TypeError, ValueError):
            spread = 0.0
        ticks = []
        for bar in response.trendbar:
            low_raw = int(bar.low)
            close_raw = low_raw + int(bar.deltaClose)
            scale = price_scale(name, digits, close_raw)
            close = close_raw / scale
            epoch_ms = int(bar.utcTimestampInMinutes) * 60_000
            ticks.append({
                "epoch": epoch_ms,
                "bid": close - spread / 2.0,
                "ask": close + spread / 2.0,
                "last": close,
                # Full OHLCV (cTrader encodes open/high/close as deltas off low).
                "open": (low_raw + int(bar.deltaOpen)) / scale,
                "high": (low_raw + int(bar.deltaHigh)) / scale,
                "low": low_raw / scale,
                "volume": int(bar.volume),
            })
        ticks.sort(key=lambda t: t["epoch"])
        self._succeed({"ticks": ticks})

    # -- response handlers --------------------------------------------------
    def _handle_account_list(self, response) -> None:
        accounts = list(response.ctidTraderAccount)
        if not accounts:
            self._fail(
                "The access token returned no accounts",
                BrokerErrorCode.ACCOUNT_NOT_FOUND,
            )
            return
        match = next(
            (a for a in accounts if a.ctidTraderAccountId == self.account_id),
            None,
        )
        if match is None:
            available = ", ".join(str(a.ctidTraderAccountId) for a in accounts)
            self._fail(
                f"Account {self.account_id} not found in the access token's "
                f"account list (available: {available})",
                BrokerErrorCode.ACCOUNT_NOT_FOUND,
            )
            return
        self._succeed(_serialize(match))

    def _handle_trader(self, response) -> None:
        trader = response.trader
        if trader is None or not trader.ctidTraderAccountId:
            self._fail(
                "Trader response did not include a trader record",
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            )
            return
        self._succeed(_serialize(trader))

    def _handle_reconcile(self, response) -> None:
        if self.query_type == "positions":
            self._succeed([self._enrich_position(p) for p in response.position])
        elif self.query_type == "orders":
            self._succeed([self._enrich_order(o) for o in response.order])
        elif self.query_type == "modify_order" and self._pending_modify is not None:
            self._send_amend_after_reconcile(response)
        else:
            self._fail(
                "Unexpected reconcile response",
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
            )

    def _send_amend_after_reconcile(self, response) -> None:
        pending = self._pending_modify
        order = next(
            (o for o in response.order if o.orderId == pending["orderId"]),
            None,
        )
        if order is None:
            self._fail(
                f"Order {pending['orderId']} not found for modification",
                BrokerErrorCode.ORDER_REJECTED,
            )
            return
        symbol_id = int(order.tradeData.symbolId) if order.tradeData.symbolId else 0
        _name, digits = self._symbol_info(symbol_id)
        request = ProtoOAAmendOrderReq(
            ctidTraderAccountId=self.account_id, orderId=pending["orderId"]
        )
        if pending.get("volume") is not None:
            try:
                request.volume = int(pending["volume"])
            except (TypeError, ValueError):
                self._fail(
                    f"Invalid volume param: {pending['volume']!r}",
                    BrokerErrorCode.ORDER_REJECTED,
                )
                return
        for field in ("limitPrice", "stopPrice", "stopLoss", "takeProfit"):
            value = pending.get(field)
            if value is None:
                continue
            try:
                setattr(request, field, _real_to_raw(float(value), digits))
            except (TypeError, ValueError):
                self._fail(
                    f"Invalid {field} param: {value!r}",
                    BrokerErrorCode.ORDER_REJECTED,
                )
                return
        self.session.client.send(request)
        logger.info(
            "Sent ProtoOAAmendOrderReq for order %s (digits=%d)",
            pending["orderId"],
            digits,
        )

    def _handle_deal_list(self, response) -> None:
        self._succeed([self._enrich_deal(d) for d in response.deal])

    def _handle_spot_event(self, event) -> None:
        if self.query_type != "market_data":
            return
        name, digits = self._symbol_info(event.symbolId)
        self._succeed(
            {
                "symbol": name,
                "symbolId": event.symbolId,
                "bid": event.bid,
                "ask": event.ask,
                "timestamp": event.timestamp,
                "digits": digits,
            }
        )

    def _handle_execution_event(self, event: ProtoOAExecutionEvent) -> None:
        if self.query_type not in ("cancel_order", "modify_order", "close_position"):
            return
        execution_type = event.executionType
        if self.query_type == "cancel_order":
            if execution_type == ProtoOAExecutionType.ORDER_CANCELLED:
                self._succeed(self._execution_payload(event))
                return
            if execution_type in (
                ProtoOAExecutionType.ORDER_REJECTED,
                ProtoOAExecutionType.ORDER_CANCEL_REJECTED,
            ):
                self._fail(
                    f"Cancel rejected: {event.errorCode or 'unknown error'}",
                    BrokerErrorCode.ORDER_REJECTED,
                )
                return
        elif self.query_type == "modify_order":
            if execution_type in (
                ProtoOAExecutionType.ORDER_ACCEPTED,
                ProtoOAExecutionType.ORDER_FILLED,
            ):
                self._succeed(self._execution_payload(event))
                return
            if execution_type == ProtoOAExecutionType.ORDER_REJECTED:
                self._fail(
                    f"Modify rejected: {event.errorCode or 'unknown error'}",
                    BrokerErrorCode.ORDER_REJECTED,
                )
                return
        else:  # close_position
            if execution_type in (
                ProtoOAExecutionType.ORDER_FILLED,
                ProtoOAExecutionType.ORDER_ACCEPTED,
                ProtoOAExecutionType.ORDER_CANCELLED,
            ):
                self._succeed(self._execution_payload(event))
                return
            if execution_type == ProtoOAExecutionType.ORDER_REJECTED:
                self._fail(
                    f"Close rejected: {event.errorCode or 'unknown error'}",
                    BrokerErrorCode.ORDER_REJECTED,
                )
                return
        logger.debug(
            "Ignoring execution event type=%s for query %s",
            execution_type,
            self.query_type,
        )

    def _handle_order_error(self, event: ProtoOAOrderErrorEvent) -> None:
        code = _map_error_code(event.errorCode)
        description = (event.description or "").strip()
        message = f"Order error: {event.errorCode} {description}".strip()
        self._fail(message, code)

    # -- enrichment helpers -------------------------------------------------
    def _symbol_info(self, symbol_id: int) -> tuple[str, int]:
        """Resolve a symbolId to (name, digits); fall back to the raw id."""
        info = self.session.symbols_by_id.get(symbol_id)
        if info is not None:
            return info  # (name, digits)
        return (str(symbol_id), 0)

    def _enrich_position(self, position) -> dict:
        item = _serialize(position)
        symbol_id = (
            int(position.tradeData.symbolId) if position.tradeData.symbolId else 0
        )
        name, digits = self._symbol_info(symbol_id)
        item["symbol"] = name
        item["digits"] = digits
        return item

    def _enrich_order(self, order) -> dict:
        item = _serialize(order)
        symbol_id = int(order.tradeData.symbolId) if order.tradeData.symbolId else 0
        name, digits = self._symbol_info(symbol_id)
        item["symbol"] = name
        item["digits"] = digits
        return item

    def _enrich_deal(self, deal) -> dict:
        item = _serialize(deal)
        symbol_id = int(deal.symbolId) if deal.symbolId else 0
        name, digits = self._symbol_info(symbol_id)
        item["symbol"] = name
        item["digits"] = digits
        return item

    def _execution_payload(self, event: ProtoOAExecutionEvent) -> dict:
        payload = _serialize(event)
        if event.order is not None and event.order.orderId:
            payload["order"] = self._enrich_order(event.order)
        if event.position is not None and event.position.positionId:
            payload["position"] = self._enrich_position(event.position)
        return payload


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _install_signal_handlers(worker: QueryWorker) -> None:
    """Stop the worker gracefully on SIGINT/SIGTERM."""

    def _on_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        logger.info("Received signal %d — stopping query worker", signum)
        try:
            from twisted.internet import reactor

            reactor.callFromThread(worker.stop)
        except Exception:  # noqa: BLE001 — best-effort stop
            pass

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


def _print_usage_error(message: str) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "error": message,
                "code": BrokerErrorCode.UNKNOWN_PROVIDER_ERROR.value,
            },
            default=str,
        ),
        flush=True,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Worker entry point; returns the process exit code (0 either way)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _print_usage_error(
            "Usage: python -m forex.pepperstone.query_worker "
            "<query_type> [<json_params>]"
        )
        return 0
    query_type = args[0]
    params: dict = {}
    if len(args) > 1:
        try:
            params = json.loads(args[1])
        except ValueError as exc:
            _print_usage_error(f"Invalid JSON params: {exc}")
            return 0
        if not isinstance(params, dict):
            _print_usage_error("JSON params must be an object")
            return 0

    try:
        worker = QueryWorker(query_type, params)
    except RuntimeError as exc:
        # Missing credentials — graceful failure, never crash, never log values.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": BrokerErrorCode.AUTHENTICATION_ERROR.value,
                },
                default=str,
            ),
            flush=True,
        )
        return 0

    _install_signal_handlers(worker)
    worker.run()
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(main())

__all__ = ["QueryWorker", "main"]
