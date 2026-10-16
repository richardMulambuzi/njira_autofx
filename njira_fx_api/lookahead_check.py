"""
Lookahead / repaint detector for the SNR engine (trade.md testing §2, §9).

The definitive test: if a signal at time T depends on data AFTER T (lookahead) or
on a still-forming candle (repaint), then truncating the future will change or
remove that signal.  So:

  1. Replay the FULL dataset, collect (entry_time, direction, entry, sl, tp).
  2. Replay a TRUNCATED dataset (future removed / replaced with noise).
  3. Every signal in the overlapping (pre-truncation) window must be IDENTICAL.

Any mismatch is a lookahead/repaint bug.  This is the stage-2 gate: if it fails,
every backtest metric is meaningless.
"""
from __future__ import annotations

import io
import os
import sys
import random

sys.path.insert(0, ".")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "forex.settings")
import django
django.setup()

from forex.backtest.parser import parse_tf_csv
from forex.backtest.engine import BacktestEngine
from forex.analysis.signal_engine import reset_funnel


def _collect(candles, symbol="EURUSD"):
    reset_funnel()
    eng = BacktestEngine()
    eng.run(candles, symbol, strategies=["malysian"], base_timeframe=60, funnel_only=True)
    out = []
    for idx, sig, levels in getattr(eng, "_collected", []):
        et = levels.get("entry_time") or (candles[idx]["open_time"].isoformat() if idx < len(candles) else "")
        out.append((
            et,
            sig.direction,
            round(float(levels.get("entry", sig.entry)), 5),
            round(float(levels.get("stop_loss", sig.stop_loss)), 5),
            round(float(levels.get("take_profit", sig.take_profit)), 5),
        ))
    return sorted(out)


def main():
    csv = sys.argv[1]
    with open(csv, encoding="utf-8") as fh:
        candles = parse_tf_csv(io.StringIO(fh.read()), symbol="EURUSD", timeframe=60)
    n = len(candles)
    cut = int(n * 0.6)
    print(f"[lookahead] loaded {n} M1 bars; truncating at {cut} ({cut/n:.0%})")

    full = _collect(candles)

    # Truncated run: same past, no future.
    trunc = _collect(candles[:cut])

    # Noise-perturbed future: past identical, future replaced with random bars
    # that keep timestamps monotonic so aggregation still works.
    rng = random.Random(123)
    noisy = list(candles[:cut])
    price = candles[cut - 1]["close"]
    for c in candles[cut:]:
        price *= (1 + rng.uniform(-0.002, 0.002))
        noisy.append({
            **c, "open": price, "high": price * 1.0005,
            "low": price * 0.9995, "close": price,
        })
    pert = _collect(noisy)

    # Signals strictly before the cut that exist in the full run must match the
    # truncated (and perturbed) run exactly.
    def before_cut(sigs):
        return [s for s in sigs if s[0] and s[0] < candles[cut]["open_time"].isoformat()]

    full_pre = before_cut(full)
    trunc_pre = before_cut(trunc)
    pert_pre = before_cut(pert)

    print(f"[lookahead] full={len(full)} (pre-cut {len(full_pre)}) | "
          f"trunc={len(trunc)} (pre-cut {len(trunc_pre)}) | "
          f"perturbed-future pre-cut {len(pert_pre)}")

    ok = True
    if full_pre != trunc_pre:
        ok = False
        print("[lookahead] FAIL: signals changed when the future was removed (lookahead!).")
        for a, b in zip(full_pre, trunc_pre):
            if a != b:
                print("   full:", a, "\n   trunc:", b)
                break
        if len(full_pre) != len(trunc_pre):
            print(f"   count differs: full_pre={len(full_pre)} trunc_pre={len(trunc_pre)}")
    else:
        print("[lookahead] PASS: no signal changed when future was removed (no lookahead).")

    if full_pre != pert_pre:
        ok = False
        print("[lookahead] FAIL: signals changed when the future was replaced with noise (repaint!).")
    else:
        print("[lookahead] PASS: no signal changed when future was replaced with noise.")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
