"""
Per-setup metrics report (trade.md testing §4 metrics + §8 per-setup).

Reads one or more `simulate --json` journal exports and reports, PER SIGNAL
TYPE (setup), the exact fields §4 asks for: trade count, win rate, avg win/loss,
expectancy, profit factor, max drawdown, max consecutive losses, avg duration,
and MAE/MFE.  It also flags which setups clear the "100+ trades to be tradable"
bar and which have positive expectancy after costs.

Usage:  python setup_report.py journal_2021.json journal_2022.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict


def _metrics(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if t["pnl"]["net_pnl"] > 0]
    losses = [t for t in trades if t["pnl"]["net_pnl"] < 0]
    gw = sum(t["pnl"]["net_pnl"] for t in wins)
    gl = abs(sum(t["pnl"]["net_pnl"] for t in losses))
    wr = len(wins) / n * 100
    avg_win = gw / len(wins) if wins else 0.0
    avg_loss = gl / len(losses) if losses else 0.0
    expectancy = (wr / 100 * avg_win) - ((1 - wr / 100) * avg_loss)
    pf = (gw / gl) if gl > 0 else float("inf")

    # max consecutive losses
    streak = worst = 0
    for t in trades:
        if t["pnl"]["net_pnl"] < 0:
            streak += 1; worst = max(worst, streak)
        else:
            streak = 0

    # drawdown over the trade sequence (net PnL curve)
    bal = 0.0; peak = 0.0; max_dd = 0.0
    for t in trades:
        bal += t["pnl"]["net_pnl"]
        peak = max(peak, bal)
        max_dd = max(max_dd, peak - bal)

    durations = [t["exit"]["duration_bars"] for t in trades if t["exit"]["duration_bars"] is not None]
    maes = [t["excursions"]["mae_pips"] for t in trades]
    mfes = [t["excursions"]["mfe_pips"] for t in trades]

    return {
        "n": n, "wins": len(wins), "losses": len(losses), "win_rate": wr,
        "avg_win": avg_win, "avg_loss": avg_loss, "expectancy": expectancy,
        "profit_factor": pf, "max_dd": max_dd, "max_consec_losses": worst,
        "avg_duration_bars": (sum(durations) / len(durations)) if durations else 0,
        "avg_mae_pips": (sum(maes) / len(maes)) if maes else 0,
        "avg_mfe_pips": (sum(mfes) / len(mfes)) if mfes else 0,
        "net_pnl": gw - gl,
    }


def main(paths):
    by_setup = defaultdict(list)
    total_all = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        for j in data["journal"]:
            if j["rejection"]["rejected"]:
                continue
            by_setup[j.get("setup_label") or "?"].append(j)
            total_all.append(j)

    def fmt(m):
        if m["n"] == 0:
            return "n=0"
        pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        flag = ""
        if m["n"] < 100:
            flag += " [TOO FEW]"
        if m["expectancy"] <= 0:
            flag += " [NEGATIVE EXP]"
        return (f"n={m['n']:<4} wr={m['win_rate']:.0f}%( {m['wins']}/{m['losses']} ) "
                f"exp=${m['expectancy']:+.2f} pf={pf} dd=${m['max_dd']:.0f} "
                f"maxL={m['max_consec_losses']} dur={m['avg_duration_bars']:.0f} "
                f"mae={m['avg_mae_pips']:.0f} mfe={m['avg_mfe_pips']:.0f}{flag}")

    print("=" * 100)
    print("PER-SETUP METRICS (after spread/slippage/commission/swap, from journal)")
    print("=" * 100)
    for setup in sorted(by_setup):
        print(f"\n[{setup}]")
        print("   " + fmt(_metrics(by_setup[setup])))
    print("\n" + "=" * 100)
    print("ALL SETUPS COMBINED")
    print("   " + fmt(_metrics(total_all)))
    print("=" * 100)
    # Which setups clear the 100-trade bar?
    enough = [s for s in by_setup if len(by_setup[s]) >= 100]
    print(f"\nSetups with >=100 trades: {enough if enough else 'NONE'}")


if __name__ == "__main__":
    main(sys.argv[1:])
