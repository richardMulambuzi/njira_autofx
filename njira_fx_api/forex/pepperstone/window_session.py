"""Live H4 window streaming on a single cTrader connection.

One :class:`PepperstoneSession` carries every requested symbol and fans each
decoded trendbar out to its own :class:`H4Window`. Sharing one connection keeps
every window on one authentication, one reconnect and one market clock, so the
symbols cannot drift apart the way N independent clients would.

Per symbol the session runs, in this order:

    subscribe spots
      -> subscribe live H4 trendbars
      -> request BACKFILL_CAL_DAYS of H4 history

A live bar that lands while history is still in flight is buffered and replayed
through ``H4Window.ingest`` once the backfill lands, so the forming bar is
always the live one. The buffer exists because ``H4Window.backfill`` clears and
re-extends the deque.

Reconnect needs no code here: ``PepperstoneSession._on_connected`` replays the
whole handshake, which fires ``on_ready`` again and re-runs the sequence above.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable, Optional, Sequence

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTrendbarsReq,
    ProtoOASubscribeLiveTrendbarReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAPayloadType,
    ProtoOATrendbarPeriod,
)
from ctrader_open_api.protobuf import Protobuf

from forex.pepperstone.client import PepperstoneSession, price_scale
from forex.pepperstone.h4_window import H4Window, backfill_start_ms

logger = logging.getLogger(__name__)

#: Bars requested per symbol. 91 calendar days of H4 is about 546, so one page.
HISTORY_COUNT = 1000

#: Actions from ``H4Window.ingest`` that mean a new bar closed.
NEW_BAR_ACTIONS = ("append", "gap")


def decode_trendbars(
    bars: Iterable[Any], name: str, digits: int
) -> list[dict]:
    """Decode cTrader trendbars into window bar dicts, oldest first.

    cTrader encodes OHLC as ``low`` plus ``deltaOpen`` / ``deltaHigh`` /
    ``deltaClose``, all raw fixed-point integers scaled by ``price_scale``.
    """
    decoded: list[dict] = []
    for bar in bars:
        low_raw = int(bar.low)
        close_raw = low_raw + int(bar.deltaClose)
        scale = price_scale(name, digits, close_raw)
        decoded.append(
            {
                "epoch": int(bar.utcTimestampInMinutes) * 60_000,
                "open": (low_raw + int(bar.deltaOpen)) / scale,
                "high": (low_raw + int(bar.deltaHigh)) / scale,
                "low": low_raw / scale,
                "close": close_raw / scale,
                "volume": int(bar.volume),
            }
        )
    decoded.sort(key=lambda bar: bar["epoch"])
    return decoded


class WindowSession(PepperstoneSession):
    """One cTrader connection streaming H4 bars into one window per symbol.

    ``symbols=None`` means every symbol the account exposes, which is usually
    much more than a strategy needs; pass an explicit list in practice.
    """

    def __init__(
        self,
        credentials: dict,
        symbols: Optional[list[str]] = None,
        on_ready: Optional[Callable[[dict], None]] = None,
        on_message: Optional[Callable[[object, object], None]] = None,
        on_bar: Optional[Callable[[str, dict], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_disconnected: Optional[Callable[[object, object], None]] = None,
        retry_policy: Optional[Callable[[int], float]] = None,
    ) -> None:
        self.windows: dict[str, H4Window] = {}
        self._symbol_ids: dict[int, str] = {}
        self._pending: dict[int, list] = {}
        self._history_pending: set[int] = set()
        self._user_on_ready = on_ready
        self._user_on_message = on_message
        self.on_bar = on_bar
        super().__init__(
            credentials,
            symbols=symbols,
            on_ready=self._on_stream_ready,
            on_message=self._on_stream_message,
            on_error=on_error,
            on_disconnected=on_disconnected,
            retry_policy=retry_policy,
        )

    def _on_stream_ready(self, symbols_map: dict) -> None:
        """Resolve symbols, then start spots, live bars and history."""
        self.windows.clear()
        self._symbol_ids.clear()
        self._pending.clear()
        self._history_pending.clear()

        names = list(self.symbols) if self.symbols else list(symbols_map)
        symbol_ids: list[int] = []
        for name in names:
            info = symbols_map.get(name)
            if info is None:
                logger.warning(
                    "Symbol %s is not in the account symbol list; skipped", name
                )
                continue
            symbol_id = int(info[0])
            self._symbol_ids[symbol_id] = name
            self.windows[name] = H4Window(symbol=name)
            self._pending[symbol_id] = []
            symbol_ids.append(symbol_id)

        if not symbol_ids:
            logger.error("No streamable symbols resolved; window stream idle")
        else:
            self.subscribe_spots(symbol_ids)
            now_ms = int(time.time() * 1000)
            for symbol_id in symbol_ids:
                self._subscribe_live_trendbars(symbol_id)
                self._history_pending.add(symbol_id)
                self._request_history(symbol_id, now_ms)

        if self._user_on_ready is not None:
            self._user_on_ready(dict(self.windows))

    def _subscribe_live_trendbars(self, symbol_id: int) -> None:
        request = ProtoOASubscribeLiveTrendbarReq(
            ctidTraderAccountId=self.credentials["account_id"],
            period=ProtoOATrendbarPeriod.H4,
            symbolId=symbol_id,
        )
        self.client.send(request)

    def _request_history(self, symbol_id: int, now_ms: int) -> None:
        request = ProtoOAGetTrendbarsReq(
            ctidTraderAccountId=self.credentials["account_id"],
            symbolId=symbol_id,
            fromTimestamp=backfill_start_ms(now_ms),
            toTimestamp=now_ms,
            period=ProtoOATrendbarPeriod.H4,
            count=HISTORY_COUNT,
        )
        self.client.send(request)

    def _on_stream_message(self, client: object, message: object) -> None:
        payload_type = getattr(message, "payloadType", None)
        if payload_type == ProtoOAPayloadType.PROTO_OA_GET_TRENDBARS_RES:
            self._handle_history(Protobuf.extract(message))
        elif payload_type == ProtoOAPayloadType.PROTO_OA_SPOT_EVENT:
            self._handle_spot(Protobuf.extract(message))
        if self._user_on_message is not None:
            self._user_on_message(client, message)

    def _resolve(self, symbol_id: int) -> tuple[Optional[H4Window], str, int]:
        name = self._symbol_ids.get(symbol_id)
        window = self.windows.get(name) if name else None
        if window is None:
            logger.debug("Dropping message for untracked symbolId %s", symbol_id)
            return None, "", 0
        digits = self.symbols_by_id.get(symbol_id, (name, 0))[1]
        return window, name, int(digits)

    def _handle_history(self, response: object) -> None:
        symbol_id = int(getattr(response, "symbolId", 0) or 0)
        window, name, digits = self._resolve(symbol_id)
        if window is not None:
            window.backfill(decode_trendbars(response.trendbar, name, digits))
        self._history_pending.discard(symbol_id)
        for bar in self._pending.pop(symbol_id, []):
            if window is not None:
                self._ingest(window, bar)

    def _handle_spot(self, event: object) -> None:
        bars = list(getattr(event, "trendbar", []) or [])
        if not bars:
            return
        symbol_id = int(getattr(event, "symbolId", 0) or 0)
        window, name, digits = self._resolve(symbol_id)
        if window is None:
            return
        decoded = decode_trendbars(bars, name, digits)
        if symbol_id in self._history_pending:
            self._pending[symbol_id].extend(decoded)
            return
        for bar in decoded:
            self._ingest(window, bar)

    def _ingest(self, window: H4Window, bar: dict) -> None:
        action = window.ingest(bar)
        if action in NEW_BAR_ACTIONS and self.on_bar is not None:
            self.on_bar(window.symbol, bar)
