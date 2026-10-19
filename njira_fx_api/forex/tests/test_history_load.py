"""Tests for the broker-sourced history load into the hot cache."""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsRes
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAPayloadType,
    ProtoOATrendbar,
    ProtoOATrendbarPeriod,
)
from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.pepperstone.h4_window import H4_SECONDS
from forex.pepperstone.ticks_engine import (
    HISTORY_PERIODS,
    TicksEngine,
    _trendbar_to_candle,
)

# 2026-01-05 is a Monday, midnight UTC, exactly on the H4 grid. It sits in the
# past, so every bar built from it counts as closed.
BASE = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)
STEP = H4_SECONDS * 1000
SYMBOL_ID = 41
DIGITS = 2
HISTORY_END = BASE + 10 * STEP


def tb(epoch_ms, volume=5):
    """A raw cTrader trendbar: low plus deltas, scaled by 1e5."""
    return ProtoOATrendbar(
        low=400_000_000,
        deltaOpen=1_000_000,
        deltaHigh=2_000_000,
        deltaClose=500_000,
        utcTimestampInMinutes=epoch_ms // 60_000,
        volume=volume,
    )


def bar(epoch_ms, volume=5):
    """A bar in the shape decode_trendbars hands to the converter."""
    return {
        "epoch": epoch_ms,
        "open": 4010.0,
        "high": 4020.0,
        "low": 4000.0,
        "close": 4005.0,
        "volume": volume,
    }


def history(period, epochs, symbol_id=SYMBOL_ID):
    """A trendbar response wrapped the way the transport delivers it."""
    response = ProtoOAGetTrendbarsRes(
        ctidTraderAccountId=123,
        period=period,
        timestamp=HISTORY_END,
        symbolId=symbol_id,
        trendbar=[tb(epoch) for epoch in epochs],
    )
    return ProtoMessage(
        payloadType=ProtoOAPayloadType.PROTO_OA_GET_TRENDBARS_RES,
        payload=response.SerializeToString(),
    )


class TrendbarToCandleTests(SimpleTestCase):
    def test_closed_bar_becomes_a_candle(self):
        candle = _trendbar_to_candle(
            "XAUUSD", H4_SECONDS, bar(BASE, volume=7), HISTORY_END
        )
        self.assertEqual(candle["symbol"], "XAUUSD")
        self.assertEqual(candle["timeframe"], H4_SECONDS)
        self.assertEqual(candle["volume_ticks"], 7)
        self.assertEqual(candle["open"], 4010.0)
        self.assertEqual(candle["close"], 4005.0)
        self.assertTrue(candle["is_closed"])
        self.assertEqual(
            candle["open_time"], datetime.fromtimestamp(BASE / 1000, tz=timezone.utc)
        )
        self.assertEqual(candle["close_time"] - candle["open_time"], timedelta(hours=4))

    def test_forming_bar_is_dropped(self):
        still_open = _trendbar_to_candle(
            "XAUUSD", H4_SECONDS, bar(HISTORY_END - 60_000), HISTORY_END
        )
        self.assertIsNone(still_open)

    def test_bar_closing_exactly_now_is_kept(self):
        just_closed = _trendbar_to_candle(
            "XAUUSD", H4_SECONDS, bar(HISTORY_END - STEP), HISTORY_END
        )
        self.assertTrue(just_closed["is_closed"])


class LoadHistoryTests(SimpleTestCase):
    def _engine(self, symbols_by_id=None):
        engine = TicksEngine.__new__(TicksEngine)
        engine._hot_cache = HotCache(max_size=500)
        engine.session = SimpleNamespace(
            symbols_by_id=(
                {SYMBOL_ID: ("XAUUSD", DIGITS)}
                if symbols_by_id is None
                else symbols_by_id
            )
        )
        return engine

    def test_loads_closed_bars_into_the_cache(self):
        engine = self._engine()
        engine._load_history(
            history(ProtoOATrendbarPeriod.H4, [BASE, BASE + STEP, BASE + 2 * STEP])
        )
        candles = engine._hot_cache.get("XAUUSD", H4_SECONDS)
        self.assertEqual(len(candles), 3)
        self.assertEqual(candles[0]["open_time"].timestamp() * 1000, BASE)

    def test_requested_periods_match_the_ones_the_engine_reads(self):
        engine = self._engine()
        for period in HISTORY_PERIODS:
            engine._load_history(history(period, [BASE, BASE + STEP]))
        self.assertEqual(
            sorted(engine._hot_cache.timeframes("XAUUSD")),
            sorted(HISTORY_PERIODS.values()),
        )

    def test_unrequested_period_is_ignored(self):
        engine = self._engine()
        engine._load_history(history(ProtoOATrendbarPeriod.D1, [BASE]))
        self.assertEqual(engine._hot_cache.get("XAUUSD", 86400), [])

    def test_unknown_symbol_is_ignored(self):
        engine = self._engine(symbols_by_id={})
        engine._load_history(history(ProtoOATrendbarPeriod.H4, [BASE], symbol_id=9999))
        self.assertEqual(engine._hot_cache.get("XAUUSD", H4_SECONDS), [])


class ErrorHandlingTests(SimpleTestCase):
    """A broker rate limit must not take the tick stream down with it."""

    def _engine(self):
        engine = TicksEngine.__new__(TicksEngine)
        engine.exit_code = 0
        engine.shutdown_event = threading.Event()
        return engine

    def test_rate_limit_does_not_shut_down_the_engine(self):
        engine = self._engine()
        engine._on_error("cTrader error BLOCKED_PAYLOAD_TYPE: You are being rate limited")
        self.assertEqual(engine.exit_code, 0)
        self.assertFalse(engine.shutdown_event.is_set())
