"""
ICT dispatch wiring test. Locks in the ``evaluate()`` branch fix.

An ``ict_*`` mode must reach the generic combo emitter and return a list.
Before the fix the branch called ``signals.extend(...)`` where no ``signals``
name exists in ``evaluate()``'s scope, raising NameError on every ICT dispatch;
and ``_evaluate_ict_strategy`` imported ``StrategyRegistry`` from the wrong
module, so a broad except swallowed it and returned [].

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
