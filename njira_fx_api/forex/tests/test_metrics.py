"""
Unit tests for :mod:`forex.metrics`.

These matter more than usual: the acceptance guardrail
(:func:`compare_to_baseline`) is the thing that decides whether a tuning
change ships, so its rejections are asserted explicitly rather than assumed.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.metrics import (
    avg_loss,
    avg_win,
    compare_to_baseline,
    compute_metrics,
    expectancy,
    group_metrics,
    max_consecutive_losses,
    max_drawdown,
    monthly_returns,
    outcome_of,
    payoff_ratio,
    print_metrics_table,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)


def _t(pnl: float, r: float | None = None, ts: float | None = None, **kw):
    d = {"pnl": pnl}
    if r is not None:
        d["r"] = r
    if ts is not None:
        d["entry_time"] = ts
    d.update(kw)
    return d


class OutcomeTests(SimpleTestCase):
    def test_outcome_from_pnl(self):
        self.assertEqual(outcome_of(_t(10.0)), "win")
        self.assertEqual(outcome_of(_t(-10.0)), "loss")
        self.assertEqual(outcome_of(_t(0.0)), "breakeven")

    def test_explicit_outcome_wins(self):
        self.assertEqual(outcome_of(_t(-10.0, outcome="win")), "win")

    def test_tiny_pnl_is_breakeven_not_win(self):
        # Float noise from a scratch exit must not inflate the win rate.
        self.assertEqual(outcome_of(_t(1e-15)), "breakeven")


class BasicMetricTests(SimpleTestCase):
    def setUp(self):
        # +100, -50, +75, -25  -> 2W 2L, net 100
        self.trades = [_t(100.0), _t(-50.0), _t(75.0), _t(-25.0)]

    def test_win_rate_excludes_breakevens(self):
        trades = self.trades + [_t(0.0)]
        self.assertAlmostEqual(win_rate(trades), 0.5)

    def test_profit_factor(self):
        # (100+75) / (50+25) = 175 / 75 = 2.3333
        self.assertAlmostEqual(profit_factor(self.trades), 175.0 / 75.0, places=6)

    def test_expectancy(self):
        self.assertAlmostEqual(expectancy(self.trades), 25.0, places=6)

    def test_avg_win_and_loss(self):
        self.assertAlmostEqual(avg_win(self.trades), 87.5, places=6)
        self.assertAlmostEqual(avg_loss(self.trades), 37.5, places=6)
        self.assertAlmostEqual(payoff_ratio(self.trades), 87.5 / 37.5, places=6)

    def test_net_profit(self):
        m = compute_metrics(self.trades)
        self.assertAlmostEqual(m.net_profit, 100.0, places=6)
        self.assertEqual(m.trade_count, 4)
        self.assertEqual(m.win_count, 2)
        self.assertEqual(m.loss_count, 2)


class UndefinedMetricTests(SimpleTestCase):
    def test_profit_factor_undefined_without_losses(self):
        self.assertIsNone(profit_factor([_t(10.0), _t(5.0)]))

    def test_profit_factor_undefined_on_empty(self):
        self.assertIsNone(profit_factor([]))

    def test_payoff_ratio_undefined_without_losses(self):
        self.assertIsNone(payoff_ratio([_t(10.0)]))

    def test_sharpe_undefined_without_dispersion(self):
        # Identical returns -> zero deviation -> no meaningful Sharpe.
        self.assertIsNone(sharpe_ratio([_t(10.0, r=1.0), _t(20.0, r=1.0)]))

    def test_sharpe_undefined_on_single_trade(self):
        self.assertIsNone(sharpe_ratio([_t(10.0, r=1.0)]))

    def test_expectancy_r_none_without_r(self):
        self.assertIsNone(compute_metrics([_t(10.0)]).expectancy_r)


class DrawdownTests(SimpleTestCase):
    def test_max_drawdown_absolute(self):
        # equity path: +100, +50, +75, +50  -> trough drawdown 50
        trades = [_t(100.0), _t(-50.0), _t(75.0), _t(-25.0)]
        self.assertAlmostEqual(max_drawdown(trades), 50.0, places=6)

    def test_max_drawdown_is_positive_magnitude(self):
        self.assertGreater(max_drawdown([_t(-30.0)]), 0.0)

    def test_max_drawdown_percent_with_seeded_equity(self):
        # start 100, +100 -> 200 peak, -100 -> 100 => 50% of the 200 peak
        dd = max_drawdown([_t(100.0), _t(-100.0)], equity=100.0)
        self.assertAlmostEqual(dd, 50.0, places=6)

    def test_max_drawdown_zero_when_monotonic_gains(self):
        self.assertAlmostEqual(max_drawdown([_t(10.0), _t(10.0)]), 0.0, places=6)


class ConsecutiveLossTests(SimpleTestCase):
    def test_counts_longest_run(self):
        trades = [_t(-1), _t(-1), _t(5), _t(-1), _t(-1), _t(-1), _t(5)]
        self.assertEqual(max_consecutive_losses(trades), 3)

    def test_breakeven_does_not_break_run(self):
        # A scratch exit mid-losing-streak must not reset the streak.
        self.assertEqual(max_consecutive_losses([_t(-1), _t(0.0), _t(-1)]), 2)

    def test_no_losses_is_zero(self):
        self.assertEqual(max_consecutive_losses([_t(1), _t(2)]), 0)


class SharpeSortinoTests(SimpleTestCase):
    def test_sharpe_positive_for_consistent_gains(self):
        # Gains must vary trade to trade; ten identical returns have zero
        # dispersion, so Sharpe is undefined by design (see the sibling test).
        trades = [_t(pnl=10.0 * (i + 1), r=1.0 + 0.1 * i) for i in range(10)]
        self.assertIsNotNone(sharpe_ratio(trades))
        self.assertGreater(sharpe_ratio(trades), 0.0)

    def test_sharpe_undefined_for_identical_returns(self):
        self.assertIsNone(sharpe_ratio([_t(0.0, r=1.0) for _ in range(10)]))

    def test_sortino_ignores_upside_dispersion(self):
        # Big wins, tiny losses: Sortino should be far above Sharpe.
        trades = [_t(0.0, r=3.0), _t(0.0, r=3.0), _t(0.0, r=3.0), _t(0.0, r=-0.1)]
        self.assertGreater(sortino_ratio(trades), sharpe_ratio(trades))

    def test_r_used_when_present(self):
        trades = [_t(pnl=999.0, r=1.0), _t(pnl=-1.0, r=-0.5)]
        self.assertAlmostEqual(compute_metrics(trades).expectancy_r, 0.25, places=6)


class MonthlyReturnsTests(SimpleTestCase):
    def test_buckets_by_utc_month(self):
        jan = 1767225600.0   # 2026-01-01
        feb = jan + 40 * 86400
        out = monthly_returns([_t(10.0, ts=jan), _t(5.0, ts=feb), _t(-3.0, ts=feb)])
        self.assertEqual(list(out), ["2026-01", "2026-02"])
        self.assertAlmostEqual(out["2026-02"], 2.0, places=6)

    def test_trades_without_timestamp_skipped(self):
        self.assertEqual(monthly_returns([_t(10.0)]), {})


class GroupingTests(SimpleTestCase):
    def test_groups_by_analyzer(self):
        trades = [
            _t(100.0, analyzer="snr"),
            _t(-50.0, analyzer="snr"),
            _t(10.0, analyzer="marriage"),
        ]
        g = group_metrics(trades, "analyzer")
        self.assertEqual(set(g), {"snr", "marriage"})
        self.assertAlmostEqual(g["snr"].net_profit, 50.0, places=6)

    def test_losing_segment_visible_not_averaged_away(self):
        # The whole point of grouping: a loser must be readable on its own.
        trades = [
            _t(500.0, analyzer="marriage"),
            _t(-100.0, analyzer="marriage"),
            _t(-40.0, analyzer="marriage"),
            _t(-30.0, analyzer="qml"),
        ]
        g = group_metrics(trades, "analyzer")
        self.assertLess(g["qml"].net_profit, 0.0)
        self.assertGreater(g["marriage"].net_profit, 0.0)

    def test_missing_key_becomes_unknown(self):
        self.assertEqual(set(group_metrics([_t(1.0)])), {"unknown"})

    def test_print_table_renders(self):
        import io

        buf = io.StringIO()
        print_metrics_table(group_metrics([_t(1.0, analyzer="snr")], "analyzer"), "t", buf)
        out = buf.getvalue()
        self.assertIn("snr", out)
        self.assertIn("t", out)


class BaselineGuardrailTests(SimpleTestCase):
    """The acceptance gate — every rejection path asserted explicitly."""

    def _base(self, n=40):
        # 40 trades: 25W/15L, win 100, loss -50 -> pf 3.33, net 1750
        trades = [_t(100.0) for _ in range(25)] + [_t(-50.0) for _ in range(15)]
        return compute_metrics(trades)

    def test_rejects_tiny_sample(self):
        m = compute_metrics([_t(500.0) for _ in range(5)])
        v = compare_to_baseline(m, self._base(), min_trades=30)
        self.assertFalse(v.accepted)
        self.assertTrue(any("sample too small" in r for r in v.reasons))

    def test_rejects_lower_profit_factor(self):
        trades = [_t(100.0) for _ in range(20)] + [_t(-100.0) for _ in range(20)]
        v = compare_to_baseline(compute_metrics(trades), self._base(), min_trades=30)
        self.assertFalse(v.accepted)
        self.assertTrue(any("profit_factor" in r for r in v.regressions))

    def test_rejects_worse_drawdown_even_with_more_profit(self):
        # Candidate nets more but has a deeper drawdown -> still rejected.
        trades = [_t(100.0) for _ in range(40)] + [_t(-4000.0)]
        v = compare_to_baseline(compute_metrics(trades), self._base(), min_trades=30)
        self.assertFalse(v.accepted)
        self.assertTrue(any("max_drawdown" in r for r in v.regressions))

    def test_rejects_longer_losing_streak(self):
        trades = [_t(100.0) for _ in range(25)] + [_t(-50.0) for _ in range(16)] + [_t(50.0)]
        v = compare_to_baseline(compute_metrics(trades), self._base(), min_trades=30)
        self.assertFalse(v.accepted)
        self.assertTrue(any("consecutive" in r for r in v.regressions))

    def test_accepts_clear_improvement(self):
        trades = [_t(200.0) for _ in range(30)] + [_t(-50.0) for _ in range(10)]
        v = compare_to_baseline(compute_metrics(trades), self._base(), min_trades=30)
        self.assertTrue(v.accepted, v.reasons)
        self.assertTrue(v.improvements)

    def test_rejects_strict_tie(self):
        v = compare_to_baseline(self._base(), self._base(), min_trades=30)
        self.assertFalse(v.accepted)
        self.assertTrue(any("no metric improved" in r for r in v.reasons))

    def test_undefined_pf_cannot_beat_defined(self):
        # All-winner candidate: PF undefined, must not slip through as "better".
        trades = [_t(100.0) for _ in range(40)]
        v = compare_to_baseline(compute_metrics(trades), self._base(), min_trades=30)
        self.assertFalse(v.accepted)
        self.assertTrue(any("undefined" in r for r in v.regressions))


class ToDictTests(SimpleTestCase):
    def test_round_trips_and_serialises(self):
        import json

        m = compute_metrics([_t(100.0, r=1.0), _t(-50.0, r=-0.5)])
        d = m.to_dict()
        json.dumps(d)  # must be JSON-safe for the API
        self.assertEqual(d["trade_count"], 2)
        self.assertAlmostEqual(d["win_rate"], 0.5, places=6)