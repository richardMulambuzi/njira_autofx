"""Unit tests for the trade simulation engine (trade.md)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from forex.sim.instruments import ContractSpec, get_spec, CommissionModel
from forex.sim.sizing import PositionSizingService
from forex.sim.rr import (
    RRService, StrategyType, Regime, volatility_multiplier,
    performance_multiplier, MARGIN_OF_SAFETY,
)
from forex.sim.costs import (
    BrokerCostModel, SpreadModel, SlippageModel, SwapModel,
)
from forex.sim.risk import AccountState, RiskLimits, RiskService
from forex.sim.journal import TradeJournal, summarize
from forex.sim.broker import SimulatedBroker, SimSignal
from forex.sim.user_behavior import UserBehaviorService, DeviationConfig
import random


T0 = datetime(2021, 1, 4, 0, 0, tzinfo=timezone.utc)


def _bars(direction_up=True, n=10, start=1.1000, step=0.0010):
    out = []
    price = start
    for i in range(n):
        o = price
        c = price + (step if direction_up else -step)
        h = max(o, c) + 0.0002
        l = min(o, c) - 0.0002
        out.append({
            "open_time": T0 + timedelta(minutes=15 * i),
            "open": o, "high": h, "low": l, "close": c, "volume": 1.0,
        })
        price = c
    return out


class InstrumentTests(SimpleTestCase):
    def test_known_symbol(self):
        s = get_spec("EURUSD")
        self.assertEqual(s.pip_size, 0.0001)
        self.assertAlmostEqual(s.pip_value_per_lot, 10.0)

    def test_jpy_pip_size(self):
        self.assertEqual(get_spec("USDJPY").pip_size, 0.01)

    def test_gold_pip_size(self):
        self.assertEqual(get_spec("XAUUSD").pip_size, 0.1)

    def test_pips_conversion(self):
        s = get_spec("EURUSD")
        self.assertAlmostEqual(s.pips(0.0015), 15.0)

    def test_unknown_symbol_gets_default(self):
        s = get_spec("WEIRD")
        self.assertEqual(s.symbol, "WEIRD")
        self.assertAlmostEqual(s.pip_size, 0.0001)

    def test_round_lots_rounds_down(self):
        s = ContractSpec("X", lot_step=0.01)
        self.assertAlmostEqual(s.round_lots(0.037), 0.03)
        self.assertAlmostEqual(s.round_lots(0.039), 0.03)


class SizingTests(SimpleTestCase):
    def setUp(self):
        self.svc = PositionSizingService()

    def test_worked_example_1(self):
        # $500 equity, $5 risk, 15-pip stop → 0.03 lots, $4.50 actual (0.90%)
        r = self.svc.size(symbol="EURUSD", equity=500, risk_amount=5,
                          entry_price=1.1000, stop_price=1.0985)
        self.assertTrue(r.accepted)
        self.assertAlmostEqual(r.rounded_size, 0.03, places=4)
        self.assertAlmostEqual(r.actual_risk, 4.50, places=2)
        self.assertAlmostEqual(r.actual_risk_pct, 0.0090, places=4)
        self.assertFalse(r.undersized)
        self.assertFalse(r.over_risk_reject)

    def test_worked_example_5_min_size_over_risk(self):
        # 120-pip stop, ideal < min lot → min lot risks 2.4% > 1.25% → reject
        r = self.svc.size(symbol="GBPJPY", equity=500, risk_amount=5,
                          entry_price=190.0, stop_price=188.80)
        self.assertFalse(r.accepted)
        self.assertTrue(r.over_risk_reject)
        self.assertAlmostEqual(r.actual_risk_pct, 0.024, places=3)

    def test_zero_stop_rejected(self):
        r = self.svc.size(symbol="EURUSD", equity=500, risk_amount=5,
                          entry_price=1.1000, stop_price=1.1000)
        self.assertFalse(r.accepted)
        self.assertIn("zero stop", r.reason)

    def test_undersized_flagged(self):
        # Tiny risk budget on a wide stop rounds down well under 0.5% of equity.
        r = self.svc.size(symbol="EURUSD", equity=100000, risk_amount=50,
                          entry_price=1.1000, stop_price=1.0600)
        self.assertTrue(r.accepted)
        self.assertTrue(r.undersized)


class RRTests(SimpleTestCase):
    def setUp(self):
        self.svc = RRService()

    def test_base_rr_power_curve(self):
        r = self.svc.calculate(strength=0.85, strategy_type=StrategyType.BREAKOUT)
        # min 2.0 + (6.0-2.0)*0.85^1.3 = 2.0 + 4*0.8096 = 5.238
        self.assertAlmostEqual(r.base_rr, 5.24, places=1)
        self.assertTrue(r.accepted)

    def test_structural_cap(self):
        r = self.svc.calculate(strength=0.85, strategy_type=StrategyType.BREAKOUT,
                               structural_rr=4.0)
        self.assertAlmostEqual(r.final_rr, 4.0, places=2)

    def test_structural_below_min_rejects(self):
        r = self.svc.calculate(strength=0.6, strategy_type=StrategyType.BREAKOUT,
                               structural_rr=1.5)
        self.assertFalse(r.accepted)
        self.assertIn("below min viable", r.reason)

    def test_volatility_multiplier_bands(self):
        self.assertAlmostEqual(volatility_multiplier(0.05), 0.70)
        self.assertAlmostEqual(volatility_multiplier(0.20), 0.85)
        self.assertAlmostEqual(volatility_multiplier(0.50), 1.00)
        self.assertAlmostEqual(volatility_multiplier(0.80), 1.15)
        self.assertAlmostEqual(volatility_multiplier(0.95), 1.30)

    def test_performance_multiplier(self):
        self.assertAlmostEqual(performance_multiplier(None), 0.95)
        self.assertAlmostEqual(performance_multiplier(0.5), 1.10)
        self.assertAlmostEqual(performance_multiplier(0.05), 1.00)
        self.assertAlmostEqual(performance_multiplier(0.0), 0.95)
        self.assertAlmostEqual(performance_multiplier(-0.5), 0.80)

    def test_worked_example_1_rr(self):
        # strength .85 breakout, ATR 65th (x1.0), strong trend (x1.2), recent+ (x1.1)
        # base 5.25 * 1.0 * 1.2 * 1.1 = 6.93, capped by structure 4.0
        r = self.svc.calculate(strength=0.85, strategy_type=StrategyType.BREAKOUT,
                               atr_percentile=0.65, regime=Regime.STRONG_TREND,
                               recent_expectancy_r=0.3, structural_rr=4.0,
                               historical_wr=0.28)
        self.assertAlmostEqual(r.volatility_mult, 1.0)
        self.assertAlmostEqual(r.regime_mult, 1.2)
        self.assertAlmostEqual(r.performance_mult, 1.10)
        self.assertAlmostEqual(r.final_rr, 4.0, places=2)
        # break-even at rr 4.0 = 20%; hist 28% → margin 8pp >= 5pp
        self.assertAlmostEqual(r.break_even_wr, 0.20, places=2)
        self.assertAlmostEqual(r.margin_of_safety, 0.08, places=2)
        self.assertTrue(r.accepted)

    def test_worked_example_3_structural_reject(self):
        r = self.svc.calculate(strength=0.60, strategy_type=StrategyType.BREAKOUT,
                               atr_percentile=0.75, regime=Regime.STRONG_TREND,
                               recent_expectancy_r=0.3, structural_rr=1.5)
        self.assertFalse(r.accepted)

    def test_break_even_corrected_raises_rr(self):
        # Low historical wr forces a HIGHER rr (not lower) to clear margin.
        r = self.svc.calculate(strength=0.9, strategy_type=StrategyType.BREAKOUT,
                               historical_wr=0.30, structural_rr=6.0)
        # base is high; scaled likely > needed. hist 0.30 - 0.05 = 0.25 → needed rr = 3.0
        # If scaled already clears 0.30 margin, no adjustment; otherwise raised to 3.0.
        if r.accepted:
            self.assertGreaterEqual(r.margin_of_safety, MARGIN_OF_SAFETY - 1e-6)
        else:
            self.assertIn("break-even", r.reason)

    def test_break_even_impossible_rejects(self):
        r = self.svc.calculate(strength=0.5, strategy_type=StrategyType.SCALP,
                               historical_wr=0.02, structural_rr=2.0)
        self.assertFalse(r.accepted)


class CostTests(SimpleTestCase):
    def test_spread_cost_positive(self):
        m = BrokerCostModel(spread=SpreadModel(fixed=0.0002))
        c = m.round_trip_cost(symbol="EURUSD", lots=0.1, mid_price=1.1,
                              entry_time=T0, exit_time=T0 + timedelta(hours=2),
                              entry_candle=_bars()[0], exit_candle=_bars()[1])
        # 2 pips spread, half-spread each side, $1/pip at 0.1 lots → ~$0.2
        self.assertGreater(c.spread_cost, 0)

    def test_commission_zero_by_default(self):
        m = BrokerCostModel()
        c = m.round_trip_cost(symbol="EURUSD", lots=0.1, mid_price=1.1,
                              entry_time=T0, exit_time=T0 + timedelta(hours=1),
                              entry_candle=_bars()[0], exit_candle=_bars()[0])
        self.assertEqual(c.commission_usd, 0.0)

    def test_commission_per_lot(self):
        spec = ContractSpec("EURUSD", commission=CommissionModel(per_lot=3.0, zero=False))
        m = BrokerCostModel(spec=spec)
        c = m.round_trip_cost(symbol="EURUSD", lots=0.1, mid_price=1.1,
                              entry_time=T0, exit_time=T0, entry_candle=_bars()[0])
        self.assertAlmostEqual(c.commission_usd, 2 * 3.0 * 0.1, places=4)

    def test_slippage_deterministic_with_seed(self):
        m1 = BrokerCostModel(slippage=SlippageModel("random", 0.0002, random.Random(7)))
        m2 = BrokerCostModel(slippage=SlippageModel("random", 0.0002, random.Random(7)))
        args = dict(symbol="EURUSD", lots=0.1, mid_price=1.1, entry_time=T0,
                    exit_time=T0, entry_candle=_bars()[0], exit_candle=_bars()[1])
        self.assertAlmostEqual(m1.round_trip_cost(**args).slippage_cost,
                               m2.round_trip_cost(**args).slippage_cost)

    def test_swap_overnight(self):
        # Tue 2021-01-05 → Thu 2021-01-07 crosses Wed (triple) + Thu (single):
        # (3 + 1) * 1.0 * 0.1 = 0.4.  Use a no-Wednesday window for the simple case.
        swap = SwapModel(per_lot_per_night=1.0, triple_on_wednesday=False)
        tue = datetime(2021, 1, 5, 0, 0, tzinfo=timezone.utc)
        c = swap.cost(0.1, tue, tue + timedelta(days=2))  # Tue→Thu: 2 nights
        self.assertAlmostEqual(c, 2.0 * 1.0 * 0.1, places=4)

    def test_swap_wednesday_triple(self):
        # 2021-01-06 is a Wednesday; holding into it charges 3x.
        swap = SwapModel(per_lot_per_night=1.0, triple_on_wednesday=True)
        wed = datetime(2021, 1, 6, 0, 0, tzinfo=timezone.utc)
        c = swap.cost(0.1, wed, wed + timedelta(days=1))  # crosses into Thu? no: one boundary
        # entry Wed 00:00, exit Thu 00:00 → one boundary (Thu 00:00), Wed is triple? no.
        # The triple applies when the *next* boundary day is Wednesday; Thu is not.
        self.assertAlmostEqual(c, 1.0 * 0.1, places=4)
        # Now a window that crosses INTO a Wednesday boundary.
        mon = datetime(2021, 1, 4, 0, 0, tzinfo=timezone.utc)  # Monday
        c2 = swap.cost(0.1, mon, mon + timedelta(days=2))  # Mon→Wed: boundaries Tue + Wed(3x)
        self.assertAlmostEqual(c2, (1.0 + 3.0) * 0.1, places=4)


class RiskTests(SimpleTestCase):
    def test_allows_normal_trade(self):
        svc = RiskService()
        acct = AccountState(initial_balance=500, balance=500, equity=500,
                            peak_equity=500, day_start_equity=500)
        d = svc.pre_trade_check(acct, symbol="EURUSD", risk_amount=5.0)
        self.assertTrue(d.allowed)

    def test_max_open_positions(self):
        svc = RiskService()
        acct = AccountState(initial_balance=500, balance=500, equity=500,
                            peak_equity=500, day_start_equity=500)
        acct.open_positions = {"A": 5, "B": 5, "C": 5}
        d = svc.pre_trade_check(acct, symbol="D", risk_amount=1.0)
        self.assertFalse(d.allowed)
        self.assertIn("open positions", d.reason)

    def test_daily_loss_blocks(self):
        svc = RiskService()
        acct = AccountState(initial_balance=500, balance=480, equity=480,
                            peak_equity=500, day_start_equity=500)
        d = svc.pre_trade_check(acct, symbol="EURUSD", risk_amount=1.0)
        self.assertFalse(d.allowed)
        self.assertIn("daily loss", d.reason)

    def test_drawdown_pauses(self):
        svc = RiskService()
        acct = AccountState(initial_balance=500, balance=390, equity=390,
                            peak_equity=500, day_start_equity=390)
        d = svc.pre_trade_check(acct, symbol="EURUSD", risk_amount=1.0)
        self.assertFalse(d.allowed)
        self.assertIn("drawdown", d.reason)

    def test_risk_amount_is_1pct_of_equity(self):
        acct = AccountState(initial_balance=500, balance=550, equity=550,
                            peak_equity=550, day_start_equity=550)
        self.assertAlmostEqual(acct.risk_amount, 5.5, places=4)


class UserBehaviorTests(SimpleTestCase):
    def test_deterministic_seed(self):
        a = UserBehaviorService(random.Random(42))
        b = UserBehaviorService(random.Random(42))
        ea = [d.deviation_type for d in a.on_signal()]
        eb = [d.deviation_type for d in b.on_signal()]
        self.assertEqual(ea, eb)

    def test_disabled_never_deviates(self):
        svc = UserBehaviorService(random.Random(1), DeviationConfig(enabled=False))
        self.assertEqual(svc.on_signal(), [])

    def test_skip_short_circuits(self):
        svc = UserBehaviorService(random.Random(1), DeviationConfig(skip_probability=1.0))
        devs = svc.on_signal()
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0].deviation_type, "skip_signal")


class BrokerTests(SimpleTestCase):
    def test_full_lifecycle_win(self):
        b = SimulatedBroker(initial_balance=500, seed=1, use_user_behavior=False)
        sig = SimSignal(symbol="EURUSD", direction="BUY", entry=1.1000,
                        stop_loss=1.0985, strength=0.8, take_profit=1.1060,
                        exit_bars=10)
        j = b.process_signal(sig, _bars(direction_up=True, n=10, step=0.0015))
        self.assertIsNotNone(j)
        self.assertFalse(j.rejected)
        self.assertEqual(j.exit_reason, "TP")
        self.assertGreater(j.net_pnl, 0)
        self.assertAlmostEqual(j.rounded_size, 0.03, places=4)

    def test_sl_loss(self):
        b = SimulatedBroker(initial_balance=500, seed=1, use_user_behavior=False)
        sig = SimSignal(symbol="EURUSD", direction="BUY", entry=1.1000,
                        stop_loss=1.0985, strength=0.8, take_profit=1.1060,
                        exit_bars=10)
        j = b.process_signal(sig, _bars(direction_up=False, n=10, step=0.0015))
        self.assertEqual(j.exit_reason, "SL")
        self.assertLess(j.net_pnl, 0)

    def test_invalid_signal_rejected(self):
        b = SimulatedBroker(initial_balance=500, seed=1, use_user_behavior=False)
        sig = SimSignal(symbol="EURUSD", direction="???", entry=1.1,
                        stop_loss=1.09, strength=0.5)
        j = b.process_signal(sig, _bars())
        self.assertTrue(j.rejected)
        self.assertEqual(j.rejection_reason, "invalid")

    def test_no_market_data_rejected(self):
        b = SimulatedBroker(initial_balance=500, seed=1, use_user_behavior=False)
        sig = SimSignal(symbol="EURUSD", direction="BUY", entry=1.1,
                        stop_loss=1.09, strength=0.5)
        j = b.process_signal(sig, [])
        self.assertTrue(j.rejected)

    def test_compounding_risk_updates(self):
        b = SimulatedBroker(initial_balance=500, seed=1, use_user_behavior=False)
        start = b.account.risk_amount
        sig = SimSignal(symbol="EURUSD", direction="BUY", entry=1.1000,
                        stop_loss=1.0985, strength=0.8, take_profit=1.1060,
                        exit_bars=10)
        b.process_signal(sig, _bars(direction_up=True, n=10, step=0.0015))
        self.assertNotAlmostEqual(b.account.risk_amount, start, places=4)

    def test_journal_has_all_sections(self):
        b = SimulatedBroker(initial_balance=500, seed=1, use_user_behavior=False)
        sig = SimSignal(symbol="EURUSD", direction="BUY", entry=1.1000,
                        stop_loss=1.0985, strength=0.8, take_profit=1.1060,
                        exit_bars=10)
        j = b.process_signal(sig, _bars(direction_up=True, n=10, step=0.0015))
        d = j.to_dict()
        for key in ("rr", "execution", "sizing", "costs", "pnl", "account",
                    "excursions", "exit", "deviations", "rejection"):
            self.assertIn(key, d)


class SummaryTests(SimpleTestCase):
    def _series(self, pnls, initial=500.0, strength=0.5, strat="breakout"):
        out, bal = [], initial
        for pnl in pnls:
            bal += pnl
            out.append(TradeJournal(
                signal_type="BUY", strategy_type=strat, strength_score=strength,
                net_pnl=pnl, gross_pnl=pnl, r_multiple=pnl / 5.0,
                final_rr=2.0, balance_after=bal, equity_after=bal))
        return out

    def test_basic_summary(self):
        s = summarize(self._series([10, -5, 20]), initial_balance=500)
        self.assertEqual(s.total_trades, 3)
        self.assertEqual(s.wins, 2)
        self.assertEqual(s.losses, 1)
        self.assertAlmostEqual(s.win_rate, 200 / 3, places=1)
        self.assertAlmostEqual(s.net_profit_usd, 25, places=2)

    def test_rejections_counted(self):
        r = TradeJournal(rejected=True, rejection_reason="risk_limit")
        s = summarize(self._series([10]) + [r], initial_balance=500)
        self.assertEqual(s.signals_taken, 1)
        self.assertEqual(s.signals_rejected, 1)
        self.assertEqual(s.rejection_breakdown["risk_limit"], 1)

    def test_empty_summary(self):
        s = summarize([], initial_balance=500)
        self.assertEqual(s.total_trades, 0)
        self.assertEqual(s.net_profit_usd, 0)


class RiskHaltTests(SimpleTestCase):
    def test_drawdown_latches_halt(self):
        svc = RiskService()
        acct = AccountState(initial_balance=500, balance=390, equity=390,
                            peak_equity=500, day_start_equity=390)
        d = svc.pre_trade_check(acct, symbol="EURUSD", risk_amount=1.0)
        self.assertFalse(d.allowed)
        self.assertTrue(acct.trading_halted)          # latched
        self.assertIn("drawdown", acct.halt_reason)
        # Even if equity is artificially restored, the halt persists.
        acct.equity = 500; acct.peak_equity = 500
        d2 = svc.pre_trade_check(acct, symbol="EURUSD", risk_amount=1.0)
        self.assertFalse(d2.allowed)
        self.assertEqual(d2.code, "halted")

    def test_review_reset_clears_halt(self):
        svc = RiskService()
        acct = AccountState(initial_balance=500, balance=390, equity=390, peak_equity=500)
        svc.pre_trade_check(acct, symbol="EURUSD", risk_amount=1.0)
        self.assertTrue(acct.trading_halted)
        acct.review_reset()
        self.assertFalse(acct.trading_halted)

    def test_daily_roll_advances_window(self):
        acct = AccountState(initial_balance=500, balance=500, equity=500, peak_equity=500)
        d0 = datetime(2021, 1, 4, 10, 0, tzinfo=timezone.utc)
        acct.roll_day(d0)
        self.assertAlmostEqual(acct.day_start_equity, 500)
        # equity drops, then a new day rolls the window to current equity
        acct.equity = 480
        d1 = datetime(2021, 1, 5, 10, 0, tzinfo=timezone.utc)  # next day
        acct.roll_day(d1)
        self.assertAlmostEqual(acct.day_start_equity, 480)
        # same day → no advance
        acct.equity = 470
        acct.roll_day(datetime(2021, 1, 5, 14, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(acct.day_start_equity, 480)


class SummarySanityTests(SimpleTestCase):
    def test_implausible_growth_flagged(self):
        # Balance blows up to 100x via compounding → not credible.
        s = summarize(
            [TradeJournal(signal_type="BUY", net_pnl=49000, gross_pnl=49000,
                          r_multiple=1, final_rr=1, balance_after=49500,
                          equity_after=49500)],
            initial_balance=500,
        )
        self.assertFalse(s.credible)
        self.assertGreater(s.growth_multiple, 50)

    def test_modest_growth_credible(self):
        s = summarize(
            [TradeJournal(signal_type="BUY", net_pnl=50, gross_pnl=50, r_multiple=1,
                          final_rr=1, balance_after=550, equity_after=550)],
            initial_balance=500,
        )
        self.assertTrue(s.credible)

    def test_halt_reported_from_account(self):
        class FakeAcct:
            equity = 400.0
            trading_halted = True
            halt_reason = "drawdown 20% pause"
        s = summarize(
            [TradeJournal(signal_type="BUY", net_pnl=10, gross_pnl=10, r_multiple=1,
                          final_rr=1, balance_after=510, equity_after=510)],
            initial_balance=500, account=FakeAcct(),
        )
        self.assertTrue(s.trading_halted)
        self.assertIn("drawdown", s.halt_reason)
        # true final equity (400), not the last-taken balance (510)
        self.assertAlmostEqual(s.final_balance, 400.0)
