"""Fetch a symbol's trendbars from cTrader (via the query worker), paging
backwards past the 1000-bar cap, and write an MT-format OHLCV CSV.

Usage:
  python fetch_history.py SYMBOL TF START END OUT.csv
    SYMBOL : e.g. XAUUSD
    TF     : M1 M5 M15 M30 H1 H4 H12 D1 W1 MN1
    START  : YYYY-MM-DD (UTC, inclusive)
    END    : YYYY-MM-DD (UTC, inclusive)
    OUT    : output csv path

Requires Pepperstone env vars (source the project .env first) and runs from
the njira_fx_api root under myvenv:
  set -a && source .env && set +a && ./myvenv/bin/python fetch_history.py ...
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

_PERIOD_MS = {
    "M1": 60_000, "M2": 120_000, "M3": 180_000, "M4": 240_000, "M5": 300_000,
    "M10": 600_000, "M15": 900_000, "M30": 1_800_000, "H1": 3_600_000,
    "H4": 14_400_000, "H12": 43_200_000, "D1": 86_400_000,
    "W1": 604_800_000, "MN1": 2_592_000_000,
}


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _worker(symbol, start_ms, end_ms, tf, attempts=4):
    """One paged request with retries; returns bars (each with epoch + OHLCV).

    The cTrader demo history endpoint intermittently drops a request; retry
    with a pause before giving up.
    """
    params = {"symbol": symbol, "start": start_ms, "end": end_ms,
              "maxRows": 1000, "period": tf}
    last = None
    for i in range(attempts):
        proc = subprocess.run(
            [sys.executable, "-m", "forex.pepperstone.query_worker",
             "ticks_range", json.dumps(params)],
            capture_output=True, text=True, timeout=180,
        )
        lines = (proc.stdout or "").strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
            if last.get("ok"):
                return last["data"]["ticks"]
        else:
            last = {"error": "no worker output"}
        if i < attempts - 1:
            print(f"  retry {i + 1}/{attempts - 1}: {last.get('error')}",
                  file=sys.stderr)
            time.sleep(10)
    raise SystemExit(f"worker error after {attempts} attempts: {last}")


def fetch(symbol, tf, start_ms, end_ms):
    step = _PERIOD_MS[tf]
    bars: dict[int, dict] = {}
    end = end_ms
    while end >= start_ms:
        ticks = _worker(symbol, start_ms, end, tf)
        if not ticks:
            break
        for t in ticks:
            bars[t["epoch"]] = t
        earliest = min(t["epoch"] for t in ticks)
        if earliest <= start_ms:
            break
        end = earliest - step  # step back one bar; dedup absorbs the overlap
        print(f"  ...{len(bars)} bars, back to {_iso(earliest)}", file=sys.stderr)
        time.sleep(2)
    # cTrader returns the last <=1000 bars at/before `end` and can overshoot
    # `start`, so clip to the requested window.
    return [bars[k] for k in sorted(bars) if start_ms <= k <= end_ms]


def main(argv):
    if len(argv) != 6:
        raise SystemExit(__doc__)
    symbol, tf, start, end, out = argv[1:6]
    tf = tf.upper()
    if tf not in _PERIOD_MS:
        raise SystemExit(f"unknown TF {tf!r}; pick one of {', '.join(_PERIOD_MS)}")
    start_ms = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    bars = fetch(symbol.upper(), tf, start_ms, end_ms)
    with open(out, "w") as fh:
        for b in bars:
            dt = datetime.fromtimestamp(b["epoch"] / 1000, tz=timezone.utc)
            fh.write(f"{dt:%Y.%m.%d},{dt:%H:%M},{b['open']:.2f},{b['high']:.2f},"
                     f"{b['low']:.2f},{b['last']:.2f},{b['volume']}\n")
    print(f"wrote {len(bars)} bars -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
