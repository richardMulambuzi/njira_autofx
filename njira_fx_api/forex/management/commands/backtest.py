"""
backtest — run the backtest engine on a local CSV file (no server, no cTrader).

Usage:
    python manage.py backtest --csv historical-data/AUDJPY/AUDJPYm15.csv \
        --symbol AUDJPY --timeframe 900 --bars 5000

Options:
    --csv PATH        CSV file (required)
    --symbol SYM      Symbol (required)
    --timeframe SEC   Seconds per base bar (default 60)
    --bars N          Limit to first N bars (default: all)
    --strategies A,B  Comma-separated strategy keys (default: all registered)
    --balance N       Initial balance (default 10000)
    --risk N          Risk per trade fraction (default 0.01)
    --funnel-only     Replay signals but skip trade simulation (fast gate tuning)
"""
from __future__ import annotations

import io

from django.core.management.base import BaseCommand, CommandError

from forex.backtest.engine import BacktestEngine
from forex.backtest.parser import parse_tf_csv
from forex.analysis.signal_engine import reset_funnel, log_funnel


class Command(BaseCommand):
    help = "Run a backtest on a local CSV file."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True)
        parser.add_argument("--symbol", required=True)
        parser.add_argument("--timeframe", type=int, default=60)
        parser.add_argument("--bars", type=int, default=0)
        parser.add_argument("--strategies", default="")
        parser.add_argument("--balance", type=float, default=10000.0)
        parser.add_argument("--risk", type=float, default=0.01)
        parser.add_argument(
            "--funnel-only", action="store_true",
            help="Replay signals and print the funnel, but skip trade simulation.",
        )
        parser.add_argument(
            "--fast", action="store_true",
            help="Skip per-candle intelligence engines (~40%% faster; SNR-only).",
        )

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

        strategies = (
            [s.strip() for s in options["strategies"].split(",") if s.strip()]
            or None
        )
        reset_funnel()
        funnel_only = options.get("funnel_only", False)
        result = BacktestEngine(fast=options.get("fast", False)).run(
            candles,
            options["symbol"],
            strategies=strategies,
            initial_balance=options["balance"],
            risk_per_trade=options["risk"],
            base_timeframe=options["timeframe"],
            funnel_only=funnel_only,
        )

        if funnel_only:
            self.stdout.write(
                f"[backtest] {options['symbol']} {options['timeframe']}s "
                f"x{len(candles)} bars: funnel-only (trade simulation skipped)"
            )
            return

        self.stdout.write(
            f"[backtest] {options['symbol']} {options['timeframe']}s "
            f"x{len(candles)} bars: trades={len(result.trades)} "
            f"best={result.best_strategy} pnl={result.total_pnl:.2f} "
            f"win_rate={result.overall_win_rate:.1f}%"
        )
        for s in result.per_strategy:
            self.stdout.write(
                f"  {s.strategy:<20} trades={s.total_trades:<4} "
                f"win_rate={s.win_rate:.1f}%  pnl={s.total_pnl:.2f}  "
                f"gate70={'PASS' if s.passes_70_gate else 'fail'}"
            )