"""
Pepperstone / cTrader order execution — non-blocking.

``TradeExecutor.execute_order`` runs a cTrader client inside a NEW background
thread with its own Twisted reactor instance, so Django request threads are
never blocked and the reactor is never run in the request thread.

Pattern (verified against Twisted 24.3.0 internals):

* The reactor is imported and run inside the worker thread
  (``reactor.run(installSignalHandlers=False)``); ``startRunning`` registers
  the worker thread as the IO thread, and ``callLater`` performs no thread-ID
  check in this Twisted version, so the pattern is safe.
* ``reactor.stop()`` is called from reactor callbacks once the execution event
  arrives, an error occurs, or the timeout fires.
* The calling thread waits on a ``threading.Event`` (memory barrier) for at
  most ``timeout + grace`` seconds and never blocks longer than that.

IMPORTANT — one-shot reactor: Twisted's global reactor can only be run once
per process (``ReactorNotRestartable`` afterwards). A second
``execute_order`` call in the same process returns a clear error instead of
crashing. For repeated execution, run each order in a fresh worker process or
extend this class with a persistent-reactor worker.

Volume convention: cTrader volumes are integers in 0.01-lot units
(1 == 0.01 lots). ``execute_order`` accepts int-like values only.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from ctrader_open_api.protobuf import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAExecutionEvent,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
    ProtoOAPayloadType,
    ProtoOATradeSide,
)

from forex.pepperstone.client import PepperstoneSession, get_credentials

logger = logging.getLogger("forex.pepperstone.execution")

DEFAULT_EXECUTION_TIMEOUT_SECONDS = 10.0
_REACTOR_SHUTDOWN_GRACE_SECONDS = 5.0

_ONE_SHOT_REACTOR_MESSAGE = (
    "The Twisted reactor is a one-shot resource per process: a previous "
    "execute_order() call already ran and stopped it. Restart the worker "
    "process (or run each order in a subprocess) before executing again."
)


class TradeExecutor:
    """
    Executes market orders against the Pepperstone demo account without
    blocking the calling (Django) thread.
    """

    _reactor_consumed_flag = False
    _guard_lock = threading.Lock()

    def __init__(self, timeout: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS) -> None:
        self.timeout = max(1.0, float(timeout))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def execute_order(self, symbol: str, side: str, volume) -> dict:
        """
        Send a market order and wait for the execution event.

        Returns ``{"status": "success", "position_id": int}`` or
        ``{"status": "error", "message": str}``. Never blocks longer than
        ``timeout + grace`` seconds.
        """
        symbol = (symbol or "").strip().upper()
        side = (side or "").strip().upper()

        if side not in ("BUY", "SELL"):
            return {
                "status": "error",
                "message": f"side must be 'BUY' or 'SELL', got {side!r}",
            }
        try:
            volume_value = float(volume)
        except (TypeError, ValueError):
            return {
                "status": "error",
                "message": f"volume must be numeric, got {volume!r}",
            }
        if not volume_value.is_integer() or volume_value <= 0:
            return {
                "status": "error",
                "message": (
                    f"volume must be a positive integer in 0.01-lot units "
                    f"(1 == 0.01 lots), got {volume!r}"
                ),
            }
        volume_int = int(volume_value)

        done = threading.Event()
        result: dict = {}

        def _worker() -> None:
            from twisted.internet import reactor

            if TradeExecutor._claim_reactor():
                result.update(
                    {"status": "error", "message": _ONE_SHOT_REACTOR_MESSAGE}
                )
                done.set()
                return
            try:
                self._run_in_reactor(
                    reactor, symbol, side, volume_int, done, result
                )
            except Exception as exc:  # noqa: BLE001 — surface any worker failure
                logger.exception("Execution worker failed: %s", exc)
                result.update(
                    {"status": "error", "message": "Execution failed"}
                )
                done.set()
                try:
                    reactor.stop()
                except Exception:  # noqa: BLE001 — best-effort stop
                    pass

        thread = threading.Thread(
            target=_worker, name="pepperstone-executor", daemon=True
        )
        thread.start()

        done.wait(self.timeout + _REACTOR_SHUTDOWN_GRACE_SECONDS)
        if not done.is_set():
            return {
                "status": "error",
                "message": (
                    f"Execution timed out after {self.timeout:.1f}s "
                    f"(no result from the worker thread)"
                ),
            }
        return result

    # ------------------------------------------------------------------
    # Reactor one-shot guard
    # ------------------------------------------------------------------
    @classmethod
    def _claim_reactor(cls) -> bool:
        """Atomically claim the process-wide reactor; True if already used."""
        with cls._guard_lock:
            if cls._reactor_consumed_flag:
                return True
            cls._reactor_consumed_flag = True
            return False

    # ------------------------------------------------------------------
    # Worker (runs inside the background thread, owns the reactor)
    # ------------------------------------------------------------------
    def _run_in_reactor(
        self,
        reactor: object,
        symbol: str,
        side: str,
        volume: int,
        done: threading.Event,
        result: dict,
    ) -> None:
        credentials = get_credentials()
        account_id = credentials["account_id"]

        session: Optional[PepperstoneSession] = None
        timeout_call = None

        def _finish() -> None:
            """Stop everything; must be called from the reactor thread."""
            if timeout_call is not None and timeout_call.active():
                timeout_call.cancel()
            if session is not None:
                try:
                    session.stop()
                except Exception:  # noqa: BLE001 — best-effort stop
                    pass
            try:
                reactor.stop()
            except Exception:  # noqa: BLE001 — best-effort stop
                pass

        def _set_error(message: str) -> None:
            result.update({"status": "error", "message": message})
            done.set()
            _finish()

        def _on_timeout() -> None:
            if not done.is_set():
                logger.error(
                    "Execution timed out after %.1fs", self.timeout
                )
                _set_error(
                    f"Timed out waiting for the execution event after "
                    f"{self.timeout:.1f}s"
                )

        def _on_ready(symbols_map: dict) -> None:
            info = symbols_map.get(symbol)
            if info is None:
                _set_error(
                    f"Symbol {symbol!r} not found in the Pepperstone account "
                    f"symbol list"
                )
                return
            symbol_id, digits = info
            order = ProtoOANewOrderReq(
                ctidTraderAccountId=account_id,
                symbolId=symbol_id,
                orderType=ProtoOAOrderType.MARKET,
                tradeSide=(
                    ProtoOATradeSide.BUY if side == "BUY" else ProtoOATradeSide.SELL
                ),
                volume=volume,
                label="njira-auto-fx",
                comment="Market order via njira_auto_fx TradeExecutor",
            )
            send_deferred = session.client.send(order)
            send_deferred.addErrback(
                lambda failure: logger.error(
                    "NewOrderReq send failed: %s",
                    failure.getErrorMessage(),
                )
            )
            logger.info(
                "Sent ProtoOANewOrderReq: symbol=%s side=%s volume=%s "
                "(symbolId=%s digits=%s)",
                symbol,
                side,
                volume,
                symbol_id,
                digits,
            )

        def _on_message(client: object, message: object) -> None:
            payload_type = message.payloadType
            if payload_type == ProtoOAPayloadType.PROTO_OA_EXECUTION_EVENT:
                try:
                    event: ProtoOAExecutionEvent = Protobuf.extract(message)
                except Exception as exc:  # noqa: BLE001 — ignore malformed
                    logger.warning("Could not parse execution event: %s", exc)
                    return
                execution_type = event.executionType
                if (
                    execution_type
                    in (
                        ProtoOAExecutionType.ORDER_FILLED,
                        ProtoOAExecutionType.ORDER_ACCEPTED,
                    )
                    and event.position is not None
                    and event.position.positionId
                ):
                    position_id = event.position.positionId
                    logger.info(
                        "Execution event received: type=%s positionId=%s",
                        execution_type,
                        position_id,
                    )
                    result.update(
                        {
                            "status": "success",
                            "position_id": position_id,
                            "execution_type": execution_type,
                        }
                    )
                    done.set()
                    _finish()
                elif execution_type == ProtoOAExecutionType.ORDER_REJECTED:
                    _set_error(f"Order rejected: {event.errorCode}")
                else:
                    logger.debug(
                        "Execution event without position (type=%s) — ignoring",
                        execution_type,
                    )
            elif payload_type == ProtoOAPayloadType.PROTO_OA_ORDER_ERROR_EVENT:
                try:
                    error_event: ProtoOAOrderErrorEvent = Protobuf.extract(message)
                except Exception as exc:  # noqa: BLE001 — ignore malformed
                    logger.warning("Could not parse order error event: %s", exc)
                    return
                _set_error(
                    f"Order error: {error_event.errorCode} "
                    f"{error_event.description or ''}".strip()
                )

        def _on_error(message: str) -> None:
            _set_error(message)

        def _on_disconnected(client: object, reason: object) -> None:
            if done.is_set():
                return
            reason_text = (
                reason.getErrorMessage()
                if hasattr(reason, "getErrorMessage")
                else ""
            )
            # Log the disconnect reason server-side only — never echo it to
            # the client (Security Gate 1 R4).
            logger.warning(
                "Disconnected before execution: %s", reason_text or "unknown"
            )
            _set_error("Connection lost")

        session = PepperstoneSession(
            credentials,
            symbols=[symbol],
            on_ready=_on_ready,
            on_message=_on_message,
            on_error=_on_error,
            on_disconnected=_on_disconnected,
        )
        session.start()
        timeout_call = reactor.callLater(self.timeout, _on_timeout)
        reactor.run(installSignalHandlers=False)