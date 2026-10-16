"""
Unit tests for :mod:`forex.walkforward`.

The exit-resolution convention matters more than anything else here: if the
harness credits a win when a single bar spans both stop and target, every
metric it produces is inflated.  Those paths are asserted explicitly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from forex.walkforward import (
    CostAwareReplay,
    CostConfig,
    make_folds,
    train_test_drift,
)

T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)


def _bar(o, h, l, c, i, tf=60):
    return {
        "open_time": T0 + timedelta(seconds=i * tf),
        "open": o, "high": h, "low": l, "close": c, "timeframe": tf,
    }


def _flat(n, price=100.0, tf=60):
    return [_bar(price, price + 0.01, price - 0.01, price, i, tf) for i in range(n)]


class CostConfigTests(SimpleTestCase):
    def test_cost_price_charges_both_sides(self):
        c = CostConfig(spread=0.03, slippage=0.01)
        # 2x0.03 spread + 2x0.01 slippage = 0.08 price units
        self.assertAlmostEqual(c.cost_price(), 0.08, places=9)

    def test_cost_in_r_scales_with_stop_distance(self):
        c = CostConfig(spread=0.03, slippage=0.01)
        # A tight stop makes the same fixed cost a much larger slice of risk.
        self.assertAlmostEqual(c.cost_in_r(0.08), 1.0, places=9)
        self.assertAlmostEqual(c.cost_in_r(0.40), 0.2, places=9)

    def test_cost_in_r_is_infinite_on_zero_stop(self):
        self.assertEqual(CostConfig().cost_in_r(0.0), float("inf"))

    def test_move_in_r_direction(self):
        c = CostConfig()
        self.assertAlmostEqual(c.move_in_r(100.0, 101.0, 99.0, "BUY"), 1.0, places=9)
        self.assertAlmostEqual(c.move_in_r(100.0, 101.0, 99.0, "SELL"), -1.0, places=9)
        self.assertAlmostEqual(c.move_in_r(100.0, 99.0, 99.0, "BUY"), -1.0, places=9)


class ExitResolutionTests(SimpleTestCase):
    def _replay(self, bars, entry_at=0, **kw):
        r = CostAwareReplay(bars, costs=CostConfig(), **kw)
        return r, entry_at

    def test_stop_wins_when_both_touched_in_one_bar(self):
        # Bar 1 spans BOTH the stop and the target.  Must resolve as a LOSS.
        bars = [_bar(100, 100, 100, 100, 0), _bar(100, 105, 95, 100, 1)]
        r, _ = self._replay(bars)
        price, outcome, held = r.resolve_exit(0, "BUY", stop=98.0, take=102.0)
        self.assertEqual(outcome, "loss")
        self.assertAlmostEqual(price, 98.0, places=6)

    def test_target_wins_when_stop_not_touched(self):
        bars = [_bar(100, 100, 100, 100, 0), _bar(100, 103, 99.5, 102, 1)]
        r, _ = self._replay(bars)
        price, outcome, _ = r.resolve_exit(0, "BUY", stop=98.0, take=102.0)
        self.assertEqual(outcome, "win")
        self.assertAlmostEqual(price, 102.0, places=6)

    def test_sell_side_stop_also_first(self):
        bars = [_bar(100, 100, 100, 100, 0), _bar(100, 105, 95, 100, 1)]
        r, _ = self._replay(bars)
        price, outcome, _ = r.resolve_exit(0, "SELL", stop=102.0, take=98.0)
        self.assertEqual(outcome, "loss")
        self.assertAlmostEqual(price, 102.0, places=6)

    def test_no_exit_expires_at_last_close(self):
        bars = _flat(5, price=100.0)
        r, _ = self._replay(bars)
        price, outcome, _ = r.resolve_exit(0, "BUY", stop=90.0, take=110.0)
        self.assertEqual(outcome, "expired")
        self.assertAlmostEqual(price, 100.0, places=6)

    def test_expiry_does_not_price_at_dataset_end(self):
        # REGRESSION: a trade that never resolves used to exit at the close of
        # the LAST bar in the dataset.  If price rises over the run, that booked
        # an expired trade as a winner and let it peek at the future.  The exit
        # must be at the close of the bar at the time limit only.
        bars = [_bar(100, 100, 100, 100, 0)]
        bars += [_bar(i, i, i, i, k) for k, i in enumerate(range(101, 121), start=1)]
        r = CostAwareReplay(bars, costs=CostConfig(), max_bars_in_trade=5)
        price, outcome, held = r.resolve_exit(0, "BUY", stop=50.0, take=500.0)
        self.assertEqual(outcome, "expired")
        # Time limit is index 0 + 5 = bar 5, close 105 -- NOT the final 120.
        self.assertLess(price, float(bars[-1]["close"]))
        self.assertAlmostEqual(price, 105.0, places=6)
        self.assertEqual(held, 5)

    def test_expiry_at_final_bar_does_not_look_backwards(self):
        # Trade opened on the very last usable bar: no forward bar exists, so
        # it must exit at that bar's own close, never a later one.
        bars = _flat(6, price=100.0)
        r = CostAwareReplay(bars, costs=CostConfig(), max_bars_in_trade=50)
        price, outcome, held = r.resolve_exit(5, "BUY", stop=50.0, take=500.0)
        self.assertEqual(outcome, "expired")
        self.assertAlmostEqual(price, float(bars[5]["close"]), places=6)
        self.assertEqual(held, 0)

    def test_exit_search_is_bounded(self):
        bars = _flat(50, price=100.0)
        r = CostAwareReplay(bars, costs=CostConfig(),
                            max_bars_in_trade=5)
        _, outcome, held = r.resolve_exit(0, "BUY", stop=90.0, take=110.0)
        self.assertEqual(outcome, "expired")
        # The window bounds the SEARCH (range end), so bars_held may sit one
        # past it when nothing triggered and it fell through to the last close.
        self.assertLessEqual(held, 6)


class RunTests(SimpleTestCase):
    def test_winning_trade_records_gross_and_cost(self):
        bars = [_bar(100, 100, 100, 100, 0), _bar(100, 103, 99.5, 102, 1),
                _bar(102, 102, 102, 102, 2)]
        costs = CostConfig(spread=0.2, slippage=0.05, risk_fraction=0.1)
        r = CostAwareReplay(bars, costs=costs, initial_balance=1000.0)

        def fn(i, c):
            if i == 0:
                return [{"direction": "BUY", "entry": 100.0, "stop_loss": 98.0,
                         "take_profit": 102.0, "strategy": "s", "analyzer": "snr",
                         "entry_time": bars[0]["open_time"].timestamp()}]
            return []

        trades = r.run(fn)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.outcome, "win")
        # cost = 2*0.20 + 2*0.05 = 0.50 price units
        # cost = (2*spread + 2*slippage) * units * dollar_per_price_unit
        self.assertGreater(t.cost, 0.0)
        self.assertLess(t.pnl, t.gross_pnl)  # costs always reduce
        self.assertGreater(t.r, 0.0)

    def test_inverted_bracket_rejected(self):
        bars = _flat(4)
        r = CostAwareReplay(bars, costs=CostConfig())

        def fn(i, c):
            if i == 0:
                return [{"direction": "BUY", "entry": 100.0, "stop_loss": 105.0,
                         "take_profit": 102.0}]
            return []

        self.assertEqual(r.run(fn), [])

    def test_bad_direction_rejected(self):
        bars = _flat(4)
        r = CostAwareReplay(bars, costs=CostConfig())

        def fn(i, c):
            if i == 0:
                return [{"direction": "SIDEWAYS", "entry": 100.0,
                         "stop_loss": 98.0, "take_profit": 102.0}]
            return []

        self.assertEqual(r.run(fn), [])

    def test_signal_exception_does_not_kill_run(self):
        bars = _flat(4)
        r = CostAwareReplay(bars, costs=CostConfig())

        def fn(i, c):
            raise RuntimeError("boom")

        self.assertEqual(r.run(fn), [])

    def test_net_pnl_is_gross_minus_cost(self):
        bars = [_bar(100, 100, 100, 100, 0), _bar(100, 103, 99.5, 102, 1),
                _bar(102, 102, 102, 102, 2)]
        costs = CostConfig(spread=0.2, slippage=0.05, risk_fraction=0.1)
        r = CostAwareReplay(bars, costs=costs, initial_balance=1000.0)

        def fn(i, c):
            if i == 0:
                return [{"direction": "BUY", "entry": 100.0, "stop_loss": 98.0,
                         "take_profit": 102.0, "entry_time": 0.0}]
            return []

        t = r.run(fn)[0]
        self.assertAlmostEqual(t.pnl, t.gross_pnl - t.cost, places=9)


class FoldTests(SimpleTestCase):
    def test_folds_cover_span_without_overlap(self):
        bars = _flat(4000, tf=900)  # ~50 days of 15M bars
        folds = make_folds(bars, n_folds=4)
        self.assertEqual(len(folds), 4)
        # Adjacent folds must tile the span: each test window starts exactly
        # where the previous one ended (half-open, so no bar is counted twice).
        for a, b in zip(folds, folds[1:]):
            self.assertEqual(a.test_end, b.test_start)
        for f in folds:
            self.assertEqual(f.train_end, f.test_start)

    def test_train_window_precedes_test(self):
        folds = make_folds(_flat(2000, tf=900), n_folds=3, train_ratio=0.5)
        for f in folds:
            self.assertLess(f.train_start, f.test_start)
            self.assertEqual(f.train_end, f.test_start)

    def test_train_ratio_is_clamped(self):
        folds = make_folds(_flat(2000, tf=900), n_folds=2, train_ratio=5.0)
        for f in folds:
            span = (f.test_start - f.train_start).total_seconds()
            test_span = (f.test_end - f.test_start).total_seconds()
            self.assertLessEqual(span, test_span)

    def test_empty_input_yields_no_folds(self):
        self.assertEqual(make_folds([], n_folds=4), [])

    def test_contains_is_half_open(self):
        folds = make_folds(_flat(100, tf=900), n_folds=2)
        f = folds[0]
        self.assertTrue(f.contains(f.test_start.timestamp()))
        self.assertFalse(f.contains(f.test_end.timestamp()))
        self.assertFalse(f.contains(None))


class DriftTests(SimpleTestCase):
    def test_no_data_reports_none(self):
        d = train_test_drift([])
        self.assertEqual(d["folds_with_data"], 0)
        self.assertIsNone(d["overfit_ratio"])

    def test_ratio_reflects_test_versus_train(self):
        from forex.metrics import compute_metrics

        good_tr = [{"pnl": 10.0, "entry_time": 0.0} for _ in range(20)]
        bad_tr = [{"pnl": -1.0, "entry_time": 0.0} for _ in range(20)]
        rows = [("f1", compute_metrics(good_tr), compute_metrics(bad_tr))]
        d = train_test_drift(rows)
        self.assertLess(d["overfit_ratio"], 1.0)