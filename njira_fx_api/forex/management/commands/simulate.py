"""
simulate — run collected signals through the broker-realistic trade simulation
engine (trade.md).

Takes the SAME historical replay as ``backtest`` but pipes every CONFIRMED signal
through :class:`forex.sim.broker.SimulatedBroker`, so results include spread,
slippage, commission, swap, 1%-of-equity compounding risk, lot rounding, the
$500 risk limits, seeded user-behaviour deviations, and a full auditable journal
+ summary (trade.md Part 6/7).

Usage:
    python manage.py simulate --csv data/EURUSD_2021.csv --symbol EURUSD \
        --balance 500 --seed 42

Options:
    --csv PATH        CSV file (required)
    --symbol SYM      Symbol (required)
    --timeframe SEC   Seconds per base bar (default 60)
    --bars N          Limit to first N bars (default: all)
    --balance N       Initial balance (default 500 — trade.md Part 1)
    --seed N          RNG seed for slippage + user behaviour (default 0)
    --risk N          Risk fraction per trade (default 0.01)
    --no-user         Disable the user-behaviour layer
    --json PATH       Write the full journal + summary as JSON
"""
from __future__ import annotations

import io
import json

from django.core.management.base import BaseCommand, CommandError

from forex.analysis.signal_engine import reset_funnel, log_funnel
from forex.backtest.engine import BacktestEngine
from forex.backtest.parser import parse_tf_csv
from forex.sim.broker import SimSignal
from forex.sim.journal import summarize
from forex.sim.rr import Regime, StrategyType


# Map the SNR analyzer to the closest strategy bucket for the dynamic-RR engine.
# The engine itself is strategy-agnostic; this only labels the journal.
_ANALYZER_STRATEGY = {
    "snr": StrategyType.PULLBACK,
    "engulfing": StrategyType.REVERSAL,
    "marriage": StrategyType.TREND_CONTINUATION,
    "flip": StrategyType.BREAKOUT,
    "qml": StrategyType.REVERSAL,
    "confluence": StrategyType.BREAKOUT,
}


class Command(BaseCommand):
    help = "Run signals through the broker-realistic trade simulation engine."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True)
        parser.add_argument("--symbol", required=True)
        parser.add_argument("--timeframe", type=int, default=60)
        parser.add_argument("--bars", type=int, default=0)
        parser.add_argument("--balance", type=float, default=500.0)
        parser.add_argument("--seed", type=int, default=0)
        parser.add_argument("--risk", type=float, default=0.01)
        parser.add_argument("--no-user", action="store_true")
        parser.add_argument("--json", dest="json_path", default="")

    def handle(self, *args, **options):
        try:
            with open(options["csv"], encoding="utf-8") as fh:
                text = "\n".join(fh.readlines()[: options["bars"] or None])
        except OSError as exc:
            raise CommandError(f"cannot read CSV: {exc}") from exc

        candles = parse_tf_csv(
            io.StringIO(text), symbol=options["symbol"], timeframe=options["timeframe"]
        )
        if not candles:
            raise CommandError("no valid rows parsed from CSV")

        symbol = options["symbol"]
        # Replay the historical candles once through the strategy engine to
        # collect its CONFIRMED signals (funnel-only skips trade simulation —
        # the realistic simulation happens below).
        reset_funnel()
        engine = BacktestEngine()
        engine.run(
            candles, symbol, strategies=["malysian"],
            initial_balance=options["balance"], risk_per_trade=options["risk"],
            base_timeframe=options["timeframe"], funnel_only=True,
        )
        log_funnel()
        collected = list(getattr(engine, "_collected", []))

        # Replay each collected signal through the realistic broker.
        from forex.sim.broker import SimulatedBroker

        broker = SimulatedBroker(
            initial_balance=options["balance"],
            seed=options["seed"],
            use_user_behavior=not options["no_user"],
        )
        journals = []
        for idx, sig, levels in collected:
            ev = dict(getattr(sig, "confidence_evidence", None) or {})
            direction = "BUY" if str(sig.direction).upper() == "BUY" else "SELL"
            strength = max(0.0, min(1.0, float(getattr(sig, "confidence_score", 50.0)) / 100.0))
            entry = float(levels.get("entry", sig.entry))
            sl = float(levels.get("stop_loss", sig.stop_loss))
            tp = float(levels.get("take_profit", sig.take_profit))
            # Bars after the entry drive the SL/TP monitor.
            future = candles[idx + 1: idx + 1 + 240]
            if not future:
                continue
            sim_sig = SimSignal(
                symbol=symbol, direction=direction, entry=entry, stop_loss=sl,
                strength=strength,
                strategy_type=_ANALYZER_STRATEGY.get(ev.get("analyzer"), StrategyType.PULLBACK),
                take_profit=tp, signal_id=sig.signal_id,
                setup_label=ev.get("analyzer", ""),
                atr_percentile=0.5, regime=Regime.WEAK_TREND, exit_bars=240,
            )
            j = broker.process_signal(sim_sig, future)
            if j is not None:
                journals.append(j)

        summary = summarize(broker.journals, initial_balance=options["balance"],
                            account=broker.account)

        self.stdout.write(
            f"[simulate] {symbol} x{len(candles)} bars: "
            f"signals={summary.total_signals_received} "
            f"taken={summary.signals_taken} rejected={summary.signals_rejected}"
        )
        self.stdout.write(
            f"  balance {summary.initial_balance:.2f} → {summary.final_balance:.2f} "
            f"({summary.return_pct:+.2f}%)  win_rate={summary.win_rate:.1f}%  "
            f"profit_factor={summary.profit_factor:.2f}  "
            f"max_dd={summary.max_drawdown_pct:.1f}%"
        )
        if summary.trading_halted:
            self.stdout.write(f"  ⚠ TRADING HALTED — {summary.halt_reason}")
        if not summary.credible:
            self.stdout.write(
                f"  ⚠ IMPLAUSIBLE: balance grew {summary.growth_multiple:.1f}x via "
                f"1%-risk compounding — treat PnL as an artifact, not an edge"
            )
        if summary.rejection_breakdown:
            self.stdout.write(
                "  rejections: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(summary.rejection_breakdown.items()))
            )
        if summary.performance_by_strategy_type:
            self.stdout.write("  by strategy: " + ", ".join(
                f"{k}={v['n']}/{v['pnl']:+.2f}"
                for k, v in sorted(summary.performance_by_strategy_type.items())))
        if summary.deviations_count_by_type:
            self.stdout.write("  user deviations: " + ", ".join(
                f"{k}={v}" for k, v in sorted(summary.deviations_count_by_type.items())))

        if options["json_path"]:
            payload = {
                "summary": summary.to_dict(),
                "journal": [j.to_dict() for j in broker.journals],
            }
            with open(options["json_path"], "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            self.stdout.write(f"  journal+summary written to {options['json_path']}")
