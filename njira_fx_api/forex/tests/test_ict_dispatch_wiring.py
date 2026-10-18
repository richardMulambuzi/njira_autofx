"""
ICT dispatch wiring test. Locks in the ``evaluate()`` branch fix and the
emitter's payload handling.

An ``ict_*`` mode must reach the generic combo emitter and return a list.
Before the fix the branch called ``signals.extend(...)`` where no ``signals``
name exists in ``evaluate()``'s scope, raising NameError on every ICT dispatch;
and ``_evaluate_ict_strategy`` imported ``StrategyRegistry`` from the wrong
module, so a broad except swallowed it and returned [].

The emitter also had to stop assuming record objects and float timestamps:
leaf engines return plain dicts and the cache stores ``datetime``.

Run: python manage.py test forex.tests.test_ict_dispatch_wiring
"""
from __future__ import annotations

from django.test import SimpleTestCase


def _candle(i: int) -> dict:
    o = 1.1000 + i * 0.0002
    return {
        "symbol": "EURUSD",
        "timeframe": 900,
        "open": o,
        "high": o + 0.0006,
        "low": o - 0.0006,
        "close": o + 0.0003,
        "open_time": 1_750_000_000 + i * 900,
        "volume": 100,
    }


class IctDispatchWiringTests(SimpleTestCase):
    def test_ict_modes_dispatch_without_nameerror(self):
        from forex.analysis.cache import HotCache
        from forex.analysis.signal_engine import SignalEngine

        cache = HotCache()
        for i in range(60):
            cache.add(_candle(i))
        eng = SignalEngine(cache=cache)
        for mode in ("ict_ote", "ict_sweep_mss", "ict_daily_bias"):
            with self.subTest(mode=mode):
                self.assertIsInstance(eng.evaluate("EURUSD", mode), list)

    def test_emitter_resolves_registry_from_strategy_module(self):
        from forex.analysis.ict_strategies import IctOteStrategy
        from forex.analysis.strategy import StrategyRegistry

        self.assertIs(StrategyRegistry.get("ict_ote"), IctOteStrategy)


class _FakeCombo:
    """Stand-in for a leaf adapter, returning the plain dict engines really return."""

    tf_seconds = 900
    direction = "LONG"

    def _records(self, symbol, engine):
        return [
            {
                "direction": self.direction,
                "zone_low": 100.0,
                "zone_high": 110.0,
                "strength": 0.8,
            }
        ]


class _NeutralCombo(_FakeCombo):
    direction = "NONE"


class _UpCombo(_FakeCombo):
    direction = "UP"


class IctEmitterPayloadTests(SimpleTestCase):
    """`ict_strategies` leaf engines emit plain dicts, not record objects.

    The emitter used to call ``rec.to_dict()`` unconditionally, which returned
    ``{}`` for every real record. ``int(candles[-1]["open_time"])`` then raised
    TypeError on the cache's timezone-aware datetimes. Both were swallowed by
    the broad except, so all 14 combos silently emitted nothing on real data.
    """

    def _engine(self):
        from datetime import datetime, timezone

        from forex.analysis.cache import HotCache
        from forex.analysis.signal_engine import SignalEngine

        open_time = datetime(2026, 10, 16, tzinfo=timezone.utc)
        cache = HotCache()
        for i in range(30):
            base = 100.0 + i
            cache.add(
                {
                    "symbol": "XAUUSD",
                    "timeframe": 900,
                    "open": base,
                    "high": base + 1.0,
                    "low": base - 1.0,
                    "close": base,
                    "open_time": open_time,
                    "volume": 1,
                }
            )
        return SignalEngine(cache=cache), open_time

    def _evaluate(self, combo):
        from unittest import mock

        from forex.analysis.strategy import StrategyRegistry

        engine, open_time = self._engine()
        with mock.patch.object(StrategyRegistry, "get", return_value=combo):
            signals = engine.evaluate("XAUUSD", "ict_fake", skip_status=True)
        return signals, open_time

    def test_dict_record_with_datetime_open_time_emits_signal(self):
        signals, open_time = self._evaluate(_FakeCombo)

        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.direction, "BUY")
        self.assertEqual(signal.entry, 105.0)
        self.assertEqual(signal.stop_loss, 95.0)
        self.assertEqual(signal.take_profit, 125.0)
        self.assertEqual(signal.risk_reward_ratio, 2.0)
        self.assertEqual(
            signal.signal_id, f"XAUUSD-ict_fake-{int(open_time.timestamp())}"
        )

    def test_directionless_record_emits_nothing(self):
        signals, _ = self._evaluate(_NeutralCombo)

        self.assertEqual(signals, [])

    def test_up_token_is_bullish(self):
        signals, _ = self._evaluate(_UpCombo)

        self.assertEqual([s.direction for s in signals], ["BUY"])
