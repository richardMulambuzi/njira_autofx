"""
Structural contradiction checks (improve.md §41 — audit: "checks are heuristic,
not structural").

These tests exercise the three structural checks added to ContradictionEngine:
  - _check_daily_high_proximity
  - _check_untouched_liquidity
  - _check_retest_break

Run:
    myvenv/bin/python manage.py test forex.tests.test_contradiction_structural
"""
from __future__ import annotations

from django.test import SimpleTestCase

from forex.analysis.contradiction import ContradictionEngine


class ContradictionStructuralTests(SimpleTestCase):
    def setUp(self):
        self.engine = ContradictionEngine()

    def _analyze(self, direction, market_state, evidence=None):
        return self.engine.analyze("EURUSD", direction, evidence or {}, market_state)

    # (a) BUY near daily high -> contradict present
    def test_buy_near_daily_high(self):
        state = {"price": 1.1000, "daily_high": 1.1005, "daily_low": 0.0}
        report = self._analyze("BUY", state)
        self.assertIn("daily_high_proximity", report.contradicting)

    # SELL near daily low -> mirror contradiction
    def test_sell_near_daily_low(self):
        state = {"price": 1.1000, "daily_high": 2.0, "daily_low": 1.0995}
        report = self._analyze("SELL", state)
        self.assertIn("daily_low_proximity", report.contradicting)

    # (b) BUY with untouched sell-side pool above -> contradict present
    def test_buy_untouched_liquidity_above(self):
        state = {
            "price": 1.1000,
            "daily_high": 1.2000,
            "daily_low": 1.0000,
            "liquidity_levels": [
                {"pool_type": "EQUAL_HIGH", "price": 1.1050, "state": "POOL"}
            ],
        }
        report = self._analyze("BUY", state)
        self.assertIn("untouched_liquidity_above", report.contradicting)

    # SELL with untouched buy-side pool below -> mirror contradiction
    def test_sell_untouched_liquidity_below(self):
        state = {
            "price": 1.1000,
            "daily_high": 1.2000,
            "daily_low": 1.0000,
            "liquidity_levels": [
                {"pool_type": "EQUAL_LOW", "price": 1.0950, "state": "POOL"}
            ],
        }
        report = self._analyze("SELL", state)
        self.assertIn("untouched_liquidity_below", report.contradicting)

    # (c) BUY with ACCEPTED retest evidence -> contradict present
    def test_buy_accepted_retest(self):
        evidence = {
            "ob_retest": [
                {
                    "zone_type": "order_block",
                    "direction": "BULLISH",
                    "reaction": "ACCEPTED",
                    "zone_top": 1.1005,
                    "zone_bottom": 1.0995,
                }
            ]
        }
        state = {"price": 1.1000}
        report = self._analyze("BUY", state, evidence)
        self.assertIn("retest_accepted", report.contradicting)

    # (d) clean case -> no new contradicts
    def test_clean_case_no_structural_contradict(self):
        state = {
            "price": 1.1000,
            "daily_high": 1.2000,
            "daily_low": 1.0000,
            "liquidity_levels": [],
        }
        evidence = {"ob_retest": [{"reaction": "REJECTED"}]}
        report = self._analyze("BUY", state, evidence)
        self.assertNotIn("daily_high_proximity", report.contradicting)
        self.assertNotIn("untouched_liquidity_above", report.contradicting)
        self.assertNotIn("untouched_liquidity_below", report.contradicting)
        self.assertNotIn("retest_accepted", report.contradicting)

    # (e) missing data -> no crash, no contradicts
    def test_missing_data_no_crash(self):
        for direction in ("BUY", "SELL"):
            report = self._analyze(direction, {}, {})
            self.assertNotIn("daily_high_proximity", report.contradicting)
            self.assertNotIn("daily_low_proximity", report.contradicting)
            self.assertNotIn("untouched_liquidity_above", report.contradicting)
            self.assertNotIn("untouched_liquidity_below", report.contradicting)
            self.assertNotIn("retest_accepted", report.contradicting)
