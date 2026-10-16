"""
FVG ClickHouse persistence tests (audit item 9 — best-effort, throttled).

Verifies that TicksEngine._on_candle_close persists FairValueGaps via
insert_fvg_batch when the per-symbol ~10s throttle allows, and skips the
call when throttled. The persistence block already lives in ticks_engine.py
(lines ~623-641); these tests lock in its behaviour without re-implementing it.

Run: python manage.py test forex.tests.test_fvg_persist
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from forex.analysis.fvg import FairValueGap, FVGState


def _make_fvgs() -> list[FairValueGap]:
    """Two real FairValueGap objects for symbol EURUSD (tf=900)."""
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    f1 = FairValueGap(
        fvg_id="EURUSD_900_BULLISH_1",
        symbol="EURUSD",
        timeframe=900,
        formation_time=base,
        direction="BULLISH",
        upper_boundary=1.1010,
        lower_boundary=1.1000,
        size=0.0010,
        status=FVGState.OPEN,
    )
    f2 = FairValueGap(
        fvg_id="EURUSD_900_BEARISH_2",
        symbol="EURUSD",
        timeframe=900,
        formation_time=base,
        direction="BEARISH",
        upper_boundary=1.0990,
        lower_boundary=1.0980,
        size=0.0010,
        status=FVGState.FORMED,
    )
    return [f1, f2]


def _make_engine():
    from forex.pepperstone.ticks_engine import TicksEngine

    eng = TicksEngine(symbols=["EURUSD"])
    # Avoid any ClickHouse reads during the strategy-refresh block.
    eng._read_user_strategy = lambda: "malysian"
    # Must include EURUSD so the symbol stays "selected" and the FVG
    # persistence block (gated by `symbol not in self._user_symbols`) runs.
    eng._read_user_symbols = lambda: ["EURUSD"]
    eng._subscribe_new_symbols = lambda syms: None
    eng._broadcast_symbol_list = lambda: None
    # Short-circuit the (heavy) signal engine so the test isolates the FVG path.
    eng._signal_engine = MagicMock()
    eng._signal_engine.evaluate.return_value = []
    return eng


def _candle():
    return {
        "symbol": "EURUSD",
        "timeframe": 900,
        "open": 1.1000,
        "high": 1.1050,
        "low": 1.0950,
        "close": 1.1020,
        "open_time": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        "volume_ticks": 10,
    }


class FVGPersistTests(SimpleTestCase):
    def test_persists_when_throttle_allows(self):
        eng = _make_engine()
        eng._ch_client = MagicMock()  # non-None so the guard passes
        fvgs = _make_fvgs()

        with patch("forex.pepperstone.ticks_engine.FVGEngine") as MockFVG, \
             patch("forex.clickhouse.schema.insert_fvg_batch") as mock_insert, \
             patch("forex.clickhouse.schema.insert_candle_batch"), \
             patch("forex.clickhouse.schema.insert_signal_v2"):
            fake_engine = MagicMock()
            fake_engine.get_all.return_value = fvgs
            MockFVG.return_value = fake_engine

            eng._on_candle_close(_candle())

        mock_insert.assert_called_once()
        client_arg, batch = mock_insert.call_args[0]
        self.assertIs(client_arg, eng._ch_client)
        self.assertEqual(len(batch), 2)
        self.assertEqual(
            {d["fvg_id"] for d in batch},
            {"EURUSD_900_BULLISH_1", "EURUSD_900_BEARISH_2"},
        )
        # to_dict() shape must match insert_fvg_batch's expected keys.
        self.assertEqual(batch[0]["symbol"], "EURUSD")
        self.assertIn("status", batch[0])

    def test_throttle_blocks_second_call(self):
        eng = _make_engine()
        eng._ch_client = MagicMock()
        fvgs = _make_fvgs()

        with patch("forex.pepperstone.ticks_engine.FVGEngine") as MockFVG, \
             patch("forex.clickhouse.schema.insert_fvg_batch") as mock_insert, \
             patch("forex.clickhouse.schema.insert_candle_batch"), \
             patch("forex.clickhouse.schema.insert_signal_v2"):
            fake_engine = MagicMock()
            fake_engine.get_all.return_value = fvgs
            MockFVG.return_value = fake_engine

            eng._on_candle_close(_candle())  # first: within throttle window start
            eng._on_candle_close(_candle())  # immediate second: must be throttled

        self.assertEqual(mock_insert.call_count, 1)

    def test_no_insert_when_ch_client_none(self):
        eng = _make_engine()
        eng._ch_client = None  # ClickHouse unavailable → never persist
        fvgs = _make_fvgs()

        with patch("forex.pepperstone.ticks_engine.FVGEngine") as MockFVG, \
             patch("forex.clickhouse.schema.insert_fvg_batch") as mock_insert, \
             patch("forex.clickhouse.schema.insert_candle_batch"), \
             patch("forex.clickhouse.schema.insert_signal_v2"):
            fake_engine = MagicMock()
            fake_engine.get_all.return_value = fvgs
            MockFVG.return_value = fake_engine

            eng._on_candle_close(_candle())

        mock_insert.assert_not_called()
