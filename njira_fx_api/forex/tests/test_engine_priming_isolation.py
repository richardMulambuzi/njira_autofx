"""The intelligence engines are keyed by timeframe only, so they must be primed
for the symbol under evaluation before any branch reads a zone. Without it a
signal inherits the previous pair's order block prices, which is how a EURUSD
card ended up with an index-level entry."""

from datetime import datetime, timezone

from django.test import TestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine

TF = 3600
T = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)


class _Spy:
    def __init__(self) -> None:
        self.analyzed = 0
        self.resets = 0

    def analyze(self, *args, **kwargs):
        self.analyzed += 1
        return []

    def compute_from_candles(self, *args, **kwargs):
        self.analyzed += 1

    def get_poc(self, *args, **kwargs):
        return None

    def reset(self) -> None:
        self.resets += 1


class EnginePrimingIsolationTests(TestCase):
    def setUp(self) -> None:
        self.spies = {name: _Spy() for name in ("structure", "liquidity", "ob", "poc", "fvg")}
        self.cache = HotCache()
        self.engine = SignalEngine(
            cache=self.cache,
            structure_engines={TF: self.spies["structure"]},
            liquidity_engines={TF: self.spies["liquidity"]},
            ob_engines={TF: self.spies["ob"]},
            poc_engines={TF: self.spies["poc"]},
            fvg_engines={TF: self.spies["fvg"]},
        )
        self.cache.set_candles(
            "AAA",
            TF,
            [
                {
                    "symbol": "AAA",
                    "open_time": T,
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "timeframe": TF,
                }
            ],
        )

    def test_prime_runs_engines_once_per_symbol(self) -> None:
        self.engine._primed_for = None
        self.engine._prime_engines("AAA")
        self.assertEqual({name: spy.analyzed for name, spy in self.spies.items()},
                         {name: 1 for name in self.spies})
        self.engine._prime_engines("AAA")
        self.assertEqual(self.spies["ob"].analyzed, 1)
        self.assertEqual(self.engine._primed_for, "AAA")

    def test_prime_clears_engines_for_a_symbol_without_candles(self) -> None:
        self.engine._primed_for = None
        self.engine._prime_engines("AAA")
        self.engine._prime_engines("BBB")
        self.assertEqual({name: spy.resets for name, spy in self.spies.items()},
                         {name: 1 for name in self.spies})
        self.assertEqual(self.engine._primed_for, "BBB")
