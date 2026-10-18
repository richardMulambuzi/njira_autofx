"""Tests for the single-connection H4 window streamer."""

from datetime import datetime, timezone

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOASpotEvent,
    ProtoOASubscribeLiveTrendbarReq,
    ProtoOASubscribeSpotsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAPayloadType,
    ProtoOATrendbar,
    ProtoOATrendbarPeriod,
)
from django.test import SimpleTestCase

from forex.pepperstone.h4_window import BACKFILL_CAL_DAYS, H4_SECONDS, WINDOW_SIZE
from forex.pepperstone.window_session import WindowSession, decode_trendbars

# 2026-01-05 is a Monday, midnight UTC, exactly on the H4 grid.
BASE = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)
STEP = H4_SECONDS * 1000
CREDS = {"account_id": 123, "client_id": "cid", "client_secret": "secret"}
SYMBOL_ID = 41
DIGITS = 2


def tb(epoch_ms, delta_close=500_000, volume=5):
    """A trendbar: low plus deltas, XAUUSD scaled by 1e5."""
    return ProtoOATrendbar(
        low=400_000_000,
        deltaOpen=1_000_000,
        deltaHigh=2_000_000,
        deltaClose=delta_close,
        utcTimestampInMinutes=epoch_ms // 60_000,
        volume=volume,
    )


def _wire(message):
    """Wrap a specific message the way the SDK delivers it to a callback."""
    return ProtoMessage(
        payloadType=message.payloadType, payload=message.SerializeToString()
    )


def history(symbol_id, epochs):
    return _wire(
        ProtoOAGetTrendbarsRes(
            payloadType=ProtoOAPayloadType.PROTO_OA_GET_TRENDBARS_RES,
            ctidTraderAccountId=CREDS["account_id"],
            period=ProtoOATrendbarPeriod.H4,
            timestamp=epochs[-1] if epochs else BASE,
            symbolId=symbol_id,
            trendbar=[tb(epoch) for epoch in epochs],
        )
    )


def spot(symbol_id, epochs):
    return _wire(
        ProtoOASpotEvent(
            payloadType=ProtoOAPayloadType.PROTO_OA_SPOT_EVENT,
            ctidTraderAccountId=CREDS["account_id"],
            symbolId=symbol_id,
            trendbar=[tb(epoch) for epoch in epochs],
        )
    )


def make_session():
    session = WindowSession(CREDS, symbols=["XAUUSD"])
    session.sent = []
    session.client.send = lambda request: session.sent.append(request)
    session.symbols_by_id = {SYMBOL_ID: ("XAUUSD", DIGITS)}
    return session


class DecodeTrendbarsTests(SimpleTestCase):
    def test_scales_ohlc_from_low_plus_deltas(self):
        bars = decode_trendbars([tb(BASE)], "XAUUSD", DIGITS)
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar["epoch"], BASE)
        self.assertEqual(bar["low"], 4000.0)
        self.assertEqual(bar["open"], 4010.0)
        self.assertEqual(bar["high"], 4020.0)
        self.assertEqual(bar["close"], 4005.0)
        self.assertEqual(bar["volume"], 5)

    def test_orders_bars_oldest_first(self):
        bars = decode_trendbars([tb(BASE + 2 * STEP), tb(BASE)], "XAUUSD", DIGITS)
        self.assertEqual(
            [bar["epoch"] for bar in bars], [BASE, BASE + 2 * STEP]
        )


class OilPerpetualScaleTests(SimpleTestCase):
    def test_oil_perpetual_decodes_on_the_metal_scale(self):
        bar = ProtoOATrendbar(
            low=9_800_000,
            deltaOpen=10_000,
            deltaHigh=20_000,
            deltaClose=74_000,
            utcTimestampInMinutes=BASE // 60_000,
            volume=5,
        )
        decoded = decode_trendbars([bar], "WTOIL-PERP", 3)[0]
        self.assertAlmostEqual(decoded["low"], 98.0, places=2)
        self.assertAlmostEqual(decoded["close"], 98.74, places=2)

    def test_soft_commodities_keep_their_digit_scale(self):
        bar = ProtoOATrendbar(
            low=400_000,
            deltaOpen=0,
            deltaHigh=0,
            deltaClose=0,
            utcTimestampInMinutes=BASE // 60_000,
            volume=5,
        )
        decoded = decode_trendbars([bar], "SoyOil", 3)[0]
        self.assertAlmostEqual(decoded["close"], 400.0, places=2)


class StreamReadyTests(SimpleTestCase):
    def setUp(self):
        self.session = make_session()

    def test_subscribes_spots_live_bars_and_history(self):
        self.session._on_stream_ready({"XAUUSD": (SYMBOL_ID, DIGITS)})

        kinds = [type(request).__name__ for request in self.session.sent]
        self.assertEqual(
            sorted(kinds),
            sorted(["ProtoOASubscribeSpotsReq", "ProtoOASubscribeLiveTrendbarReq", "ProtoOAGetTrendbarsReq"]),
        )

        spots = next(r for r in self.session.sent if isinstance(r, ProtoOASubscribeSpotsReq))
        self.assertEqual(list(spots.symbolId), [SYMBOL_ID])

        live = next(r for r in self.session.sent if isinstance(r, ProtoOASubscribeLiveTrendbarReq))
        self.assertEqual(live.symbolId, SYMBOL_ID)
        self.assertEqual(live.period, 10)  # ProtoOATrendbarPeriod.H4

        req = next(r for r in self.session.sent if isinstance(r, ProtoOAGetTrendbarsReq))
        self.assertEqual(req.symbolId, SYMBOL_ID)
        self.assertEqual(req.period, 10)
        self.assertEqual(req.count, 1000)
        self.assertAlmostEqual(
            req.toTimestamp - req.fromTimestamp,
            BACKFILL_CAL_DAYS * 86_400 * 1000,
            delta=1000,
        )

    def test_creates_one_window_per_symbol_and_reports_it(self):
        seen = []
        self.session._user_on_ready = lambda windows: seen.append(windows)
        self.session._on_stream_ready({"XAUUSD": (SYMBOL_ID, DIGITS)})
        self.assertEqual(list(self.session.windows), ["XAUUSD"])
        self.assertEqual(seen, [{"XAUUSD": self.session.windows["XAUUSD"]}])

    def test_skips_unknown_symbols(self):
        self.session._on_stream_ready({"EURUSD": (7, 5)})
        self.assertEqual(self.session.windows, {})
        self.assertEqual(self.session.sent, [])


class HistoryAndLiveTests(SimpleTestCase):
    def setUp(self):
        self.session = make_session()
        self.session._on_stream_ready({"XAUUSD": (SYMBOL_ID, DIGITS)})
        self.window = self.session.windows["XAUUSD"]

    def test_history_backfills_the_window(self):
        self.session._on_stream_message(None, history(SYMBOL_ID, [BASE, BASE + STEP]))
        self.assertEqual(len(self.window), 2)
        self.assertEqual(self.window.latest()["epoch"], BASE + STEP)
        self.assertFalse(self.window.degraded)
        self.assertEqual(self.session._history_pending, set())

    def test_forming_bar_overwrites_without_firing_on_bar(self):
        self.session._on_stream_message(None, history(SYMBOL_ID, [BASE, BASE + STEP]))
        fired = []
        self.session.on_bar = lambda symbol, bar: fired.append((symbol, bar))

        self.session._on_stream_message(None, spot(SYMBOL_ID, [BASE + STEP]))

        self.assertEqual(len(self.window), 2)
        self.assertEqual(fired, [])

    def test_new_closed_bar_appends_and_fires_on_bar(self):
        self.session._on_stream_message(None, history(SYMBOL_ID, [BASE, BASE + STEP]))
        fired = []
        self.session.on_bar = lambda symbol, bar: fired.append((symbol, bar))

        self.session._on_stream_message(None, spot(SYMBOL_ID, [BASE + 2 * STEP]))

        self.assertEqual(len(self.window), 3)
        self.assertEqual([entry[0] for entry in fired], ["XAUUSD"])
        self.assertEqual(fired[0][1]["epoch"], BASE + 2 * STEP)

    def test_untracked_symbol_is_dropped(self):
        self.session._on_stream_message(None, spot(9999, [BASE]))
        self.assertEqual(len(self.window), 0)

    def test_spot_without_trendbars_is_ignored(self):
        self.session._on_stream_message(None, spot(SYMBOL_ID, []))
        self.assertEqual(len(self.window), 0)

    def test_user_on_message_still_receives_every_payload(self):
        received = []
        self.session._user_on_message = lambda client, message: received.append(message)
        message = history(SYMBOL_ID, [BASE])
        self.session._on_stream_message(None, message)
        self.assertEqual(received, [message])


class BackfillRaceTests(SimpleTestCase):
    def test_live_bars_arriving_during_history_are_replayed_after_it(self):
        session = make_session()
        session._on_stream_ready({"XAUUSD": (SYMBOL_ID, DIGITS)})
        window = session.windows["XAUUSD"]

        session._on_stream_message(None, spot(SYMBOL_ID, [BASE + 2 * STEP]))
        self.assertEqual(len(window), 0)
        self.assertEqual(len(session._pending[SYMBOL_ID]), 1)

        session._on_stream_message(None, history(SYMBOL_ID, [BASE, BASE + STEP]))

        self.assertEqual(len(window), 3)
        self.assertEqual(window.latest()["epoch"], BASE + 2 * STEP)
        self.assertEqual(session._pending, {})

    def test_window_caps_at_three_hundred_through_the_session(self):
        session = make_session()
        session._on_stream_ready({"XAUUSD": (SYMBOL_ID, DIGITS)})
        window = session.windows["XAUUSD"]

        epochs = [BASE + index * STEP for index in range(WINDOW_SIZE + 50)]
        session._on_stream_message(None, history(SYMBOL_ID, epochs))

        self.assertEqual(len(window), WINDOW_SIZE)
        self.assertEqual(window.latest()["epoch"], epochs[-1])
