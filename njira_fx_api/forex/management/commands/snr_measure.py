"""
Measure a strategy family with COSTS and WALK-FORWARD out-of-sample folds.

Replaces the cost-free reporting of ``snr_fast`` for anything that drives a
tuning decision.  ``snr_fast`` applies no spread, slippage or commission, so a
rule that looks profitable there may be unprofitable once the broker is paid.

Usage
-----
    python manage.py snr_measure --csv ../historical-data/XAGUSD_2024/EURUSD_2023.csv \
        --symbol EURUSD --analyzer marriage --folds 4 --balance 100000 \
        --spread 0.00012 --slippage 0.00002

The exit convention is conservative: when a single bar spans both the stop and
the target, the STOP is taken.  OHLC cannot tell which printed first, and
assuming the win would inflate every number this reports.
"""
from __future__ import annotations

import bisect
from collections import defaultdict

from django.core.management.base import BaseCommand

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.signal_lifecycle import SignalState
from forex.backtest.parser import parse_tf_csv
from forex.metrics import compare_to_baseline, compute_metrics, group_metrics, print_metrics_table
from forex.management.commands.snr_fast import SNR_TFS, _aggregate, _floor
from forex.walkforward import (
    CostAwareReplay,
    CostConfig,
    folds_report,
    make_folds,
    print_folds_report,
    train_test_drift,
)

#: Timeframes replayed incrementally, mirroring the engine's own dependencies.
_HTF = (3600, 14400, 86400)


class Command(BaseCommand):
    help = "Cost-aware, walk-forward measurement of a Malaysian SNR analyzer."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True)
        parser.add_argument("--symbol", default="XAGUSD")
        parser.add_argument("--timeframe", type=int, default=60)
        parser.add_argument("--analyzer", default="snr",
                            help="only count this analyzer's trades (default: snr)")
        parser.add_argument("--folds", type=int, default=4)
        parser.add_argument("--balance", type=float, default=100000.0)
        parser.add_argument("--risk", type=float, default=0.01)
        # Defaults are EURUSD 1.2-pip. Costs are quoted in price units, so the
        # right value is instrument-specific: gold ~0.30/oz, silver ~0.03/oz.
        parser.add_argument("--spread", type=float, default=0.00012,
                            help="spread in price units (0.00012 = 1.2 pips on EURUSD)")
        parser.add_argument("--slippage", type=float, default=0.00002)
        parser.add_argument("--commission", type=float, default=7.0)
        parser.add_argument("--baseline-json", default="",
                            help="optional metrics JSON to compare against")

    def handle(self, *args, **opts):
        symbol = opts["symbol"]
        analyzer = opts["analyzer"]
        with open(opts["csv"], encoding="utf-8") as fh:
            base = parse_tf_csv(fh, symbol=symbol, timeframe=opts["timeframe"])
        if len(base) < 64:
            self.stderr.write("not enough candles")
            return

        # Pre-aggregate the HTFs once, then feed them incrementally so no
        # analyzer can see a bar the live engine would not have had.
        htf = {tf: _aggregate(base, tf, symbol) for tf in SNR_TFS}
        m15 = _aggregate(base, 900, symbol)
        cache = HotCache()
        engine = SignalEngine(cache=cache)
        series = {tf: htf[tf] for tf in _HTF}
        ptrs = {tf: 0 for tf in _HTF}
        open_times = [c["open_time"] for c in base]

        def pending(tf, upto):
            out = []
            arr = series[tf]
            i = ptrs[tf]
            while i < len(arr):
                if i + 1 < len(arr) and _floor(arr[i]["open_time"], tf) + tf > upto:
                    break
                out.append(arr[i])
                i += 1
            ptrs[tf] = i
            return out

        costs = CostConfig(
            spread=opts["spread"],
            slippage=opts["slippage"],
            commission_per_lot=opts["commission"],
            risk_fraction=opts["risk"],
        )

        def _fill_time(idx: int, price: float, horizon: int = 400) -> float:
            """
            Timestamp of the candle whose OPEN equals the signal entry price.

            ``Signal`` carries no entry timestamp, so the fill candle is located
            by matching its open.  Fills are defined as a candle open throughout
            the engine, so an exact match is the normal case; when none is found
            within the horizon we fall back to the confirmation bar so the trade
            still lands in the right fold rather than being dropped.
            """
            for k in range(idx, min(len(base), idx + horizon)):
                if abs(float(base[k]["open"]) - price) <= 1e-9:
                    return base[k]["open_time"].timestamp()
            return float(base[idx]["open_time"].timestamp())

        # Collect signals keyed by the M15 bar they were confirmed on, so the
        # replay can resolve the exit against the base series.
        per_bar: dict[int, list[dict]] = defaultdict(list)
        for bar in m15:
            bar_epoch = int(bar["open_time"].timestamp())
            for tf in _HTF:
                for hb in pending(tf, bar_epoch):
                    cache.add(hb)
            cache.add(bar)
            try:
                signals = engine._evaluate_snr(symbol)
            except Exception:  # noqa: BLE001 — one bad bar must not kill the run
                continue
            idx = bisect.bisect_left(open_times, bar["open_time"])
            for sig in signals or []:
                if sig.state != SignalState.CONFIRMED:
                    continue
                ev = dict(sig.confidence_evidence or {})
                if analyzer and ev.get("analyzer") != analyzer:
                    continue
                per_bar[idx].append({
                    "direction": sig.direction,
                    "entry": float(sig.entry),
                    "stop_loss": float(sig.stop_loss),
                    "take_profit": float(sig.take_profit),
                    "strategy": sig.strategy,
                    "analyzer": ev.get("analyzer", "?"),
                    "entry_time": _fill_time(idx, float(sig.entry)),
                })

        replay = CostAwareReplay(base, costs=costs, initial_balance=opts["balance"])
        trades = replay.run(lambda i, c: per_bar.get(i, []))

        dicts = [t.to_metrics_dict() for t in trades]
        overall = compute_metrics(dicts, equity=opts["balance"])
        self.stdout.write(
            f"\n=== {analyzer or 'all'} on {symbol} — {len(base)} bars, "
            f"costs: spread={opts['spread']} slip={opts['slippage']} "
            f"comm=${opts['commission']}/lot ==="
        )
        print_metrics_table({"all": overall}, "")
        # Cost realism diagnostic: a stop narrower than the round-trip cost
        # cannot be profitable at ANY win rate, so report the distribution.
        if per_bar:
            widths = []
            for idx, entries in per_bar.items():
                for e in entries:
                    widths.append(abs(e["entry"] - e["stop_loss"]))
            if widths:
                widths.sort()
                n = len(widths)
                med = widths[n // 2]
                cost_r = costs.cost_in_r(med) if med else float("inf")
                self.stdout.write(
                    f"  stop width (price units): p10={widths[n//10]:.4f} "
                    f"median={med:.4f} p90={widths[(9*n)//10]:.4f}"
                )
                self.stdout.write(
                    f"  round-trip cost {costs.cost_price():.4f} price units = "
                    f"{cost_r:.2f}R at the median stop"
                    + ("  <-- COSTS EXCEED 1R: unprofitable at any win rate"
                       if cost_r >= 1.0 else "")
                )
        if overall.trade_count:
            total_cost = sum(t.cost for t in trades)
            total_gross = sum(t.gross_pnl for t in trades)
            self.stdout.write(
                f"  costs ${total_cost:,.2f} on gross ${total_gross:,.2f} "
                f"({total_cost / max(abs(total_gross), 1e-9) * 100:.1f}% of gross)"
            )
            # Expectancy in R is the only unit-free comparison: dollar PnL
            # compounds and hits zero on a losing book, which hides the shape.
            er = overall.expectancy_r
            self.stdout.write(
                f"  expectancy = {'n/a' if er is None else f'{er:+.3f}'}R/trade"
                + ("" if er is None else ("  (profitable)" if er > 0 else "  (LOSS-MAKING)"))
            )
            self.stdout.write(f"  {overall.row()}")

        folds = make_folds(base, n_folds=opts["folds"])
        rows = folds_report(folds, trades)
        print_folds_report(rows)
        drift = train_test_drift(rows)
        if drift["overfit_ratio"] is not None:
            ratio = drift["overfit_ratio"]
            verdict = "OK" if ratio >= 0.5 else "SUSPECT OVERFIT"
            self.stdout.write(
                f"  train/test expectancy ratio = {ratio:.2f}  [{verdict}]"
            )

        if opts["baseline_json"]:
            import json
            with open(opts["baseline_json"], encoding="utf-8") as fh:
                baseline_dict = json.load(fh)
            verdict = compare_to_baseline(
                overall,
                _metrics_from_dict(baseline_dict),
                min_trades=30,
            )
            self.stdout.write(f"\n  baseline verdict: "
                              f"{'ACCEPT' if verdict.accepted else 'REJECT'}")
            for line in verdict.improvements:
                self.stdout.write(f"    + {line}")
            for line in verdict.regressions + verdict.reasons:
                self.stdout.write(f"    - {line}")


def _metrics_from_dict(d: dict):
    """Rebuild a TradeMetrics from its to_dict() form (for baseline compare)."""
    from forex.metrics import TradeMetrics

    fields = {f: d.get(f) for f in TradeMetrics.__dataclass_fields__ if f in d}
    fields["trade_count"] = int(fields.get("trade_count") or 0)
    fields["max_consecutive_losses"] = int(fields.get("max_consecutive_losses") or 0)
    return TradeMetrics(**fields)