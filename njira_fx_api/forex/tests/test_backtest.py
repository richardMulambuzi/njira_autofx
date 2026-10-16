"""
Backtest engine tests (QA Gate 2).

(a) parse_csv handles the real gold_dataset.csv column layout.
(b) resample 1m -> 15m produces correct OHLC.
(c) BacktestEngine runs end-to-end on synthetic candles (fake signal engine
    injected) and returns per-strategy metrics + trades with execution_steps.
(d) 70% win-rate gate: >=70 passes, <70 fails; best_strategy selection.
(e) upload view: POST a small CSV -> 200 with result JSON (engine mocked).
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from forex.analysis.signal_lifecycle import Signal, SignalState
from forex.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    StrategyBacktestResult,
)
from forex.backtest.parser import parse_csv, resample


def _candle(symbol, minute, o, h, l, c):
    return {
        "symbol": symbol,
        "open_time": datetime(2025, 1, 1, 0, minute, 0, tzinfo=timezone.utc),
        "open": float(o), "high": float(h), "low": float(l), "close": float(c),
    }


def _fake_signal():
    return Signal(
        signal_id="sig_test",
        symbol="XAUUSD",
        direction="BUY",
        state=SignalState.CONFIRMED,
        entry=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        confidence_score=72.0,
        confidence_evidence={
            "htf_bias": 18,
            "ob_quality": 16,
            "contradiction": {
                "recommendation": "TRADE",
                "severity": "LOW",
                "reasons": ["no conflicting HTF bias"],
            },
        },
        explanation="Multi-TF confluence confirmed",
        strategy="malysian",
        timeframe="multi",
        risk_reward_ratio=2.0,
    )


class ParseCsvTests(SimpleTestCase):
    def test_parse_real_format(self):
        csv_text = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "XAUUSD,20250101,230100,2625.09,2625.09,2624.47,2624.95\n"
            "XAUUSD,20250101,230200,2624.95,2626.10,2624.90,2625.80\n"
        )
        candles = parse_csv(StringIO(csv_text))
        self.assertEqual(len(candles), 2)
        c0 = candles[0]
        self.assertEqual(c0["symbol"], "XAUUSD")
        self.assertEqual(c0["open_time"], datetime(2025, 1, 1, 23, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(c0["open"], 2625.09)
        self.assertEqual(c0["high"], 2625.09)
        self.assertEqual(c0["low"], 2624.47)
        self.assertEqual(c0["close"], 2624.95)

    def test_symbol_from_column_not_hardcoded(self):
        csv_text = "ticker,datestamp,timestamp,open,high,low,close\nEURUSD,20250101,000000,1.1,1.1,1.1,1.1\n"
        candles = parse_csv(StringIO(csv_text))
        self.assertEqual(candles[0]["symbol"], "EURUSD")


class ResampleTests(SimpleTestCase):
    def test_resample_1m_to_15m(self):
        candles = [
            _candle("XAUUSD", 0, 100, 101, 99, 100),
            _candle("XAUUSD", 1, 100, 102, 100, 101),
            _candle("XAUUSD", 2, 101, 103, 101, 102),
            _candle("XAUUSD", 15, 102, 104, 102, 103),  # next 15m bucket
            _candle("XAUUSD", 16, 103, 105, 103, 104),
        ]
        out = resample(candles, 900)
        self.assertEqual(len(out), 2)
        b0 = out[0]
        self.assertEqual(b0["open"], 100)
        self.assertEqual(b0["high"], 103)  # max of bucket 0
        self.assertEqual(b0["low"], 99)     # min of bucket 0
        self.assertEqual(b0["close"], 102)  # last close of bucket 0
        b1 = out[1]
        self.assertEqual(b1["open"], 102)
        self.assertEqual(b1["close"], 104)


class EngineEndToEndTests(SimpleTestCase):
    def test_run_produces_trades_with_execution_steps(self):
        candles = [
            _candle("XAUUSD", 0, 100, 100, 100, 100),
            _candle("XAUUSD", 1, 100, 103, 100, 102),  # high 103 >= tp 102 -> win
            _candle("XAUUSD", 2, 102, 102, 102, 102),
        ]

        class FakeSignalEngine:
            def __init__(self):
                self._fired = False

            def evaluate(self, symbol, mode, closed_tf=None, skip_status=False):
                if self._fired:
                    return []
                self._fired = True
                return [_fake_signal()]

        result = BacktestEngine(signal_engine=FakeSignalEngine()).run(candles, "XAUUSD")

        self.assertEqual(result.symbol, "XAUUSD")
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade["outcome"], "win")
        self.assertGreater(trade["pnl"], 0)
        self.assertTrue(trade["execution_steps"])
        # evidence keys rendered into steps
        joined = " ".join(trade["execution_steps"]).lower()
        self.assertIn("htf bias", joined)
        self.assertIn("contradiction", joined)

        self.assertEqual(len(result.per_strategy), 1)
        strat = result.per_strategy[0]
        self.assertEqual(strat.strategy, "malysian")
        self.assertEqual(strat.total_trades, 1)
        self.assertEqual(strat.wins, 1)
        self.assertAlmostEqual(strat.win_rate, 100.0)
        self.assertTrue(strat.passes_70_gate)
        self.assertEqual(result.best_strategy, "malysian")


class SimulateTradeStrengthTests(SimpleTestCase):
    def _sig(self, confidence: float, strength: str = ""):
        return Signal(
            signal_id="sig_strength",
            symbol="XAUUSD",
            direction="BUY",
            state=SignalState.CONFIRMED,
            entry=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            confidence_score=confidence,
            strength=strength,
            explanation="test",
            strategy="malysian",
            timeframe="multi",
        )

    def _trades_for(self, sig):
        engine = BacktestEngine()
        engine._base_candles = [
            _candle("XAUUSD", 0, 100, 101, 99, 100),
            _candle("XAUUSD", 1, 100, 104, 100, 103),  # high 104 >= tp 102 -> win
        ]
        return engine._simulate_trade(
            sig, idx=0, risk_per_trade=0.01, current_balance=10000.0
        )

    def test_strength_derived_from_confidence_when_unset(self):
        trade = self._trades_for(self._sig(confidence=72.0))
        self.assertIn("strength", trade)
        self.assertEqual(trade["strength"], Signal.classify_strength(72.0))
        self.assertEqual(trade["strength"], "STRONG")

    def test_explicit_strength_is_preserved(self):
        # An explicitly-set tier (e.g. RISKY is a side multi-timeframe test)
        # must survive rather than being overwritten by the confidence mapping.
        trade = self._trades_for(self._sig(confidence=90.0, strength="RISKY"))
        self.assertEqual(trade["strength"], "RISKY")


class CollectSnapshotTests(SimpleTestCase):
    """A refreshed signal keeps its id, so the first collect must win the levels.

    Without the snapshot in _on_candle_close a signal that lived for weeks was
    entered on its first bar at the price of its LAST refresh.
    """

    def test_first_collect_levels_win_over_later_refresh(self):
        engine = BacktestEngine()
        engine._base_candles = [
            _candle("XAUUSD", 0, 100, 101, 99, 100),
            _candle("XAUUSD", 1, 100, 104, 100, 103),  # high 104 >= tp 102 -> win
        ]
        sig = Signal(
            signal_id="sig_refresh", symbol="XAUUSD", direction="BUY",
            state=SignalState.CONFIRMED, entry=100.0, stop_loss=99.0,
            take_profit=102.0, confidence_score=60.0, explanation="test",
            strategy="malaysian", timeframe="multi",
            confidence_evidence={"analyzer": "snr"},
        )
        first = {"entry": 100.0, "stop_loss": 99.0, "take_profit": 102.0,
                 "evidence": {"analyzer": "snr"}}
        # ...later the signal is refreshed to a much higher entry.
        sig.entry, sig.stop_loss, sig.take_profit = 180.0, 179.0, 182.0
        sig.confidence_evidence["analyzer"] = "marriage"

        trade = engine._simulate_trade(
            sig, idx=0, levels=first, risk_per_trade=0.01, current_balance=10000.0
        )
        self.assertEqual(trade["entry"], 100.0)
        self.assertEqual(trade["stop_loss"], 99.0)
        self.assertEqual(trade["take_profit"], 102.0)
        self.assertEqual(trade["evidence"]["analyzer"], "snr")
        self.assertEqual(trade["outcome"], "win")


class GateTests(SimpleTestCase):
    def test_gate_pass_and_fail(self):
        passing = StrategyBacktestResult(strategy="A", total_trades=10, wins=7, losses=3)
        failing = StrategyBacktestResult(strategy="B", total_trades=10, wins=6, losses=4)
        self.assertTrue(passing.passes_70_gate)   # 70% exactly
        self.assertFalse(failing.passes_70_gate)  # 60%

    def test_best_strategy_prefers_gate_winner(self):
        a = StrategyBacktestResult(strategy="A", total_trades=10, wins=7, losses=3)   # 70, passes
        b = StrategyBacktestResult(strategy="B", total_trades=10, wins=8, losses=2)   # 80, passes
        c = StrategyBacktestResult(strategy="C", total_trades=10, wins=9, losses=1)   # 90 but <70? no, 90 passes
        # all pass -> highest win_rate wins
        self.assertEqual(BacktestEngine._best_strategy([a, b, c]), "C")
        # none pass -> highest win_rate among all
        low1 = StrategyBacktestResult(strategy="L1", total_trades=10, wins=5, losses=5)
        low2 = StrategyBacktestResult(strategy="L2", total_trades=10, wins=6, losses=4)
        self.assertEqual(BacktestEngine._best_strategy([low1, low2]), "L2")


class UploadViewTests(TestCase):
    def test_upload_returns_200_with_result(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        u = U.objects.create_user(username="btuser", password="btpass")

        canned = BacktestResult(
            symbol="XAUUSD",
            initial_balance=10000.0,
            final_balance=10500.0,
            total_pnl=500.0,
            overall_win_rate=75.0,
            per_strategy=[StrategyBacktestResult(
                strategy="malysian", total_trades=4, wins=3, losses=1, win_rate=75.0)],
            trades=[{"signal_id": "x", "strategy": "malysian", "outcome": "win",
                     "execution_steps": ["OB detected"]}],
            best_strategy="malysian",
        )

        csv_text = (
            "ticker,datestamp,timestamp,open,high,low,close\n"
            "XAUUSD,20250101,000000,2600,2600,2600,2600\n"
            "XAUUSD,20250101,000100,2600,2603,2600,2602\n"
            "XAUUSD,20250101,000200,2602,2602,2602,2602\n"
        )
        uploaded = SimpleUploadedFile("gold.csv", csv_text.encode("utf-8"),
                                      content_type="text/csv")

        with mock.patch("forex.backtest.views.BacktestEngine") as MockEngine:
            MockEngine.return_value.run.return_value = canned
            client = APIClient()
            client.force_authenticate(user=u)
            resp = client.post("/api/backtest/upload/", {"file": uploaded, "symbol": "XAUUSD"},
                               format="multipart")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["symbol"], "XAUUSD")
        self.assertEqual(body["data"]["best_strategy"], "malysian")
        self.assertEqual(len(body["data"]["trades"]), 1)
        # persisted
        from forex.models import BacktestResult as Model
        self.assertEqual(Model.objects.count(), 1)
