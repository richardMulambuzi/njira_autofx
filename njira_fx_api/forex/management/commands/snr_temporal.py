"""
Temporal distribution analysis (improvee.md PHASE 5).

Breaks a strategy's trades down by year, quarter, month, day-of-week, hour and
trading session, so "is the edge persistent, seasonal, session-specific,
regime-specific, episodic or decaying?" is answered from data rather than
assumed.

The session breakdown is the one that matters most here: EURUSD currently
trades London/NewYork only, and that gate discards roughly half of all valid
marriage setups.  This command is how we find out whether the gate earns its
keep — by measuring the sessions it currently rejects instead of deleting them
on the strength of a default.

improvee.md is explicit: *"Do not remove losing periods simply because they
reduce performance."*  Nothing here removes anything; it only measures. Any
change to the session gate has to be judged on out-of-sample expectancy, not
on the in-sample table printed here.
"""
from __future__ import annotations

import bisect
from collections import defaultdict

from django.core.management.base import BaseCommand

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.signal_lifecycle import SignalState
from forex.analysis.sessions import session_of
from forex.backtest.parser import parse_tf_csv
from forex.metrics import compute_metrics
from forex.management.commands.snr_fast import SNR_TFS, _aggregate, _floor

_HTF = (3600, 14400, 86400)


class Command(BaseCommand):
    help = "Temporal/session distribution of SNR-family trades (improvee.md PHASE 5)."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True)
        parser.add_argument("--symbol", default="EURUSD")
        parser.add_argument("--timeframe", type=int, default=60)
        parser.add_argument("--analyzer", default="marriage")
        parser.add_argument("--spread", type=float, default=0.00012)
        parser.add_argument("--slippage", type=float, default=0.00002)

    def handle(self, *args, **opts):
        symbol, analyzer = opts["symbol"], opts["analyzer"]
        cost_price = 2 * opts["spread"] + 2 * opts["slippage"]
        with open(opts["csv"], encoding="utf-8") as fh:
            base = parse_tf_csv(fh, symbol=symbol, timeframe=opts["timeframe"])
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

        trades = []
        for bar in m15:
            bar_epoch = int(bar["open_time"].timestamp())
            for tf in _HTF:
                for hb in pending(tf, bar_epoch):
                    cache.add(hb)
            cache.add(bar)
            # Bypass _evaluate_snr's session gate: we WANT the rejected ones.
            try:
                drafts = engine._malaysian_candidates(symbol)
            except Exception:  # noqa: BLE001 — one bad bar must not kill the run
                continue
            idx = bisect.bisect_left(open_times, bar["open_time"])
            for d in drafts:
                if analyzer and (d.evidence or {}).get("analyzer") != analyzer:
                    continue
                if d.direction not in ("BUY", "SELL"):
                    continue
                entry, sl, tp = float(d.entry), float(d.stop_loss), float(d.take_profit)
                risk = abs(entry - sl)
                if risk <= 0:
                    continue
                rr = abs(tp - entry) / risk
                outcome_r = None
                for k in range(idx + 1, len(base)):
                    c = base[k]
                    if d.direction == "BUY":
                        if c["low"] <= sl:
                            outcome_r = -1.0
                            break
                        if c["high"] >= tp:
                            outcome_r = rr
                            break
                    else:
                        if c["high"] >= sl:
                            outcome_r = -1.0
                            break
                        if c["low"] <= tp:
                            outcome_r = rr
                            break
                if outcome_r is None:
                    continue
                t = base[idx]["open_time"]
                trades.append({
                    "r": outcome_r - cost_price / risk,
                    "t": t,
                    "direction": d.direction,
                    "hour": t.hour,
                    "dow": t.strftime("%a"),
                })

        if not trades:
            self.stderr.write("no trades resolved")
            return

        self.stdout.write(
            f"\n=== {analyzer} on {symbol} — temporal distribution "
            f"(n={len(trades)}, cost {cost_price:g} price units/trade) ==="
        )
        self._table("BY MONTH", trades, lambda x: x["t"].strftime("%Y-%m"))
        self._table("BY DAY OF WEEK", trades, lambda x: x["dow"],
                    order=("Mon", "Tue", "Wed", "Thu", "Fri"))
        self._table("BY HOUR (UTC)", trades, lambda x: f"{x['hour']:02d}:00")
        self._table("BY SESSION", trades, self._session)

        live = engine._sessions_for(symbol)
        self.stdout.write(f"\n  live gate allows: {live}")
        rejected = [x for x in trades if not self._in_live(x, live)]
        kept = [x for x in trades if self._in_live(x, live)]
        for label, grp in (("KEPT by current gate", kept), ("REJECTED by current gate", rejected)):
            if not grp:
                continue
            m = compute_metrics([{"pnl": x["r"]} for x in grp])
            w = sum(1 for x in grp if x["r"] > 0)
            self.stdout.write(
                f"  {label:<26} n={len(grp):<5} wr={w / len(grp) * 100:5.1f}%  "
                f"expR={m.expectancy:+.3f}  totalR={sum(x['r'] for x in grp):+.1f}"
            )
        self.stdout.write(
            "\n  NOTE: the rejected row above is IN-SAMPLE. Removing or adding a\n"
            "  session gate must be judged on out-of-sample expectancy\n"
            "  (snr_measure --folds), never on this table."
        )

    @staticmethod
    def _session(t: dict) -> str:
        s = session_of(t["t"].timestamp())
        return "+".join(s) if s else "none(gap)"

    @staticmethod
    def _in_live(t: dict, live) -> bool:
        return any(s in live for s in session_of(t["t"].timestamp()))

    def _table(self, title, trades, keyfn, order=None):
        buckets = defaultdict(list)
        for t in trades:
            buckets[keyfn(t)].append(t["r"])
        keys = [k for k in order if k in buckets] if order else sorted(buckets)
        if not keys:
            return
        self.stdout.write(f"\n  {title}")
        for k in keys:
            v = buckets[k]
            w = sum(1 for x in v if x > 0)
            bar = "#" * int(max(0, sum(v)) * 2) if sum(v) > 0 else ""
            self.stdout.write(
                f"    {str(k):<12} n={len(v):<5} wr={w / len(v) * 100:5.1f}%  "
                f"expR={sum(v) / len(v):+.3f}  totalR={sum(v):+8.1f}  {bar}"
            )