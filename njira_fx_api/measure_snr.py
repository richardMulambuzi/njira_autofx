#!/usr/bin/env python
"""
measure_snr.py — Fetch Oct 10–15, 2026 OHLC data via Yahoo Finance and
measure Malaysian SNR signal performance across XAUUSD, EURUSD, GBPUSD, USDJPY.

Yahoo Finance tickers:
    XAUUSD → GC=F  (Gold futures, used as XAUUSD proxy)
    EURUSD → EURUSD=X
    GBPUSD → GBPUSD=X
    USDJPY → USDJPY=X

Run from njira_fx_api/:
    source venv/bin/activate
    pip install requests yfinance
    python measure_snr.py

Output:
    - signals/measure_YYYYMMDD_HHMM/  — one .md per signal
    - signals/measure_YYYYMMDD_HHMM/REPORT.md — full measurement report
"""
from __future__ import annotations

import os
import sys
import pathlib
import types
import json
from collections import defaultdict
from datetime import datetime, timezone

# ── Load .env ────────────────────────────────────────────────────────────────
_env = pathlib.Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

# ── Stubs for unavailable Django/clickhouse deps ──────────────────────────────
import unittest

class _Stub(types.ModuleType):
    def __getattr__(self, k):
        s = _Stub(f"{self.__name__}.{k}"); setattr(self, k, s); return s
    def __call__(self, *a, **kw): return _Stub("_r")
    def __iter__(self): return iter([])
    def __bool__(self): return True

_dt = _Stub("django.test")
_dt.SimpleTestCase = unittest.TestCase
_dt.TestCase = unittest.TestCase

class _FakeOvr:
    def __init__(self, *a, **kw): pass
    def __call__(self, f): return f
    def __enter__(self): return self
    def __exit__(self, *a): pass

_dt.override_settings = _FakeOvr

for _m in [
    "django", "django.db", "django.db.models", "django.db.models.signals",
    "django.conf", "django.utils", "django.utils.timezone",
    "django.utils.functional", "django.apps", "django.apps.registry",
    "django.core", "django.core.management",
    "clickhouse_connect", "ctrader_open_api",
    "twisted", "twisted.internet",
    "pusher",
    # NOTE: do NOT stub google.protobuf — yfinance uses the real one
]:
    sys.modules.setdefault(_m, _Stub(_m))
sys.modules["django.test"] = _dt

# Patch forex/__init__ to skip the clickhouse import
import importlib, importlib.util, importlib.abc

_forex_root = pathlib.Path(__file__).parent / "forex"

class _ForexInit(importlib.abc.Loader):
    def create_module(self, spec): return None
    def exec_module(self, module):
        try:
            from dotenv import load_dotenv
            load_dotenv(pathlib.Path(__file__).parent / ".env")
        except ImportError:
            pass

_spec = importlib.util.spec_from_file_location(
    "forex", _forex_root / "__init__.py",
    loader=_ForexInit(),
    submodule_search_locations=[str(_forex_root)],
)
_forex_mod = importlib.util.module_from_spec(_spec)
_forex_mod.__path__ = [str(_forex_root)]
_forex_mod.__package__ = "forex"
sys.modules["forex"] = _forex_mod
_spec.loader.exec_module(_forex_mod)

# ── Now safe to import forex internals ───────────────────────────────────────
from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine

# ── Config ────────────────────────────────────────────────────────────────────
# Yahoo Finance tickers → internal symbol names
YF_SYMBOLS = {
    "GC=F":      "XAUUSD",   # Gold futures proxy
    "EURUSD=X":  "EURUSD",
    "GBPUSD=X":  "GBPUSD",
    "USDJPY=X":  "USDJPY",
}

START_STR = "2026-10-10"
END_STR   = "2026-10-16"   # yfinance end is exclusive

H4_SECS  = 14400
M15_SECS = 900

COSTS = {
    "XAUUSD": {"spread": 0.30,   "slippage": 0.05},
    "EURUSD": {"spread": 0.00012,"slippage": 0.00002},
    "GBPUSD": {"spread": 0.00020,"slippage": 0.00003},
    "USDJPY": {"spread": 0.015,  "slippage": 0.003},
}

OUT_DIR = pathlib.Path("signals") / f"measure_{datetime.now().strftime('%Y%m%d_%H%M')}"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Data fetching via yfinance ────────────────────────────────────────────────

def fetch_ohlc(yf_ticker: str, interval: str, start: str, end: str) -> list[dict]:
    """Download OHLC from Yahoo Finance. interval: '15m', '1h', etc."""
    if not _YF_OK:
        print("  [ERROR] yfinance not installed. Run: pip install yfinance")
        return []

    symbol_name = YF_SYMBOLS.get(yf_ticker, yf_ticker)
    print(f"  Fetching {yf_ticker} ({symbol_name}) {interval} {start}→{end} ...", flush=True)

    try:
        df = _yf.download(
            yf_ticker,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=True,
            progress=False,
        )
    except Exception as e:
        print(f"  [ERROR] yfinance download failed: {e}")
        return []

    if df is None or df.empty:
        print(f"  [WARN] No data returned for {yf_ticker} {interval}")
        return []

    bars = []
    tf_secs = M15_SECS if interval == "15m" else H4_SECS

    for ts, row in df.iterrows():
        try:
            dt_utc = ts.to_pydatetime().replace(tzinfo=timezone.utc)
        except Exception:
            continue
        try:
            o = float(row["Open"])
            h = float(row["High"])
            l = float(row["Low"])
            c = float(row["Close"])
        except Exception:
            continue
        bars.append({
            "symbol":    symbol_name,
            "timeframe": tf_secs,
            "open_time": dt_utc,
            "open":  o,
            "high":  h,
            "low":   l,
            "close": c,
        })

    print(f"    → {len(bars)} bars for {symbol_name} {interval}", flush=True)
    return bars


# ── Signal generation ─────────────────────────────────────────────────────────

def run_signals(symbol: str, h4_bars: list[dict], m15_bars: list[dict]) -> list[dict]:
    """Walk M15 bars, run SNR engine at each close, collect all signals."""
    all_sigs: list[dict] = []
    WARMUP = 25  # need at least this many bars before starting

    for i in range(WARMUP, len(m15_bars)):
        # Build cache: all 4H data + M15 up to current bar
        cache = HotCache()
        h4_cut = [b for b in h4_bars if b["open_time"] <= m15_bars[i]["open_time"]]
        for b in h4_cut:
            cache.add(dict(b))
        for b in m15_bars[:i + 1]:
            cache.add(dict(b))

        try:
            eng = SignalEngine(cache=cache)
            drafts = eng._malaysian_candidates(symbol)
        except Exception:
            continue

        for d in drafts:
            if d.entry is None or d.stop_loss is None or d.take_profit is None:
                continue
            ev = d.evidence or {}
            all_sigs.append({
                "symbol":      symbol,
                "direction":   d.direction,
                "entry":       d.entry,
                "stop_loss":   d.stop_loss,
                "take_profit": d.take_profit,
                "risk_reward": d.risk_reward,
                "confidence":  d.confidence,
                "entry_time":  m15_bars[i]["open_time"].timestamp(),
                "bar_index":   i,
                "evidence":    ev,
                "analyzer":    ev.get("analyzer", "?"),
                "session":     ev.get("session", ""),
                "in_kill_zone":ev.get("in_kill_zone", False),
                "snr_fresh":   ev.get("snr_fresh", False),
                "snr_validation": ev.get("snr_validation", ""),
                "snr_clean":   ev.get("snr_clean", False),
                "snr_rule3_ok":ev.get("snr_rule3_ok", False),
                "snr_body_first": ev.get("snr_body_first", False),
            })

    # Deduplicate: keep only the first signal per (direction, analyzer, bar_index)
    seen: set = set()
    unique: list[dict] = []
    for s in all_sigs:
        key = (s["direction"], s["analyzer"], s["bar_index"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique

# ── Exit simulation ───────────────────────────────────────────────────────────

def simulate_exits(signals: list[dict], m15_bars: list[dict], costs: dict) -> list[dict]:
    spread   = costs["spread"]
    slippage = costs["slippage"]
    cost_price = 2 * spread + 2 * slippage

    bar_times = [b["open_time"].timestamp() for b in m15_bars]
    results = []

    for sig in signals:
        start_idx = next((i for i, t in enumerate(bar_times) if t > sig["entry_time"]), None)
        if start_idx is None:
            continue

        d   = sig["direction"]
        ent = float(sig["entry"])
        sl  = float(sig["stop_loss"])
        tp  = float(sig["take_profit"])

        if d == "BUY"  and not (sl < ent < tp): continue
        if d == "SELL" and not (tp < ent < sl): continue

        stop_dist = abs(ent - sl)
        if stop_dist <= 0: continue

        outcome = "EXPIRED"
        exit_price = float(m15_bars[min(start_idx, len(m15_bars)-1)]["close"])
        bars_held = 0

        for k in range(start_idx, min(len(m15_bars), start_idx + 400)):
            bar = m15_bars[k]
            lo, hi = float(bar["low"]), float(bar["high"])
            if d == "BUY":
                if lo <= sl: exit_price = sl; outcome = "LOSS"; bars_held = k - start_idx; break
                if hi >= tp: exit_price = tp; outcome = "WIN";  bars_held = k - start_idx; break
            else:
                if hi >= sl: exit_price = sl; outcome = "LOSS"; bars_held = k - start_idx; break
                if lo <= tp: exit_price = tp; outcome = "WIN";  bars_held = k - start_idx; break

        gross_r = ((exit_price - ent) / stop_dist) if d == "BUY" else ((ent - exit_price) / stop_dist)
        cost_r  = cost_price / stop_dist
        net_r   = gross_r - cost_r

        results.append({
            **sig,
            "outcome":    outcome,
            "exit_price": exit_price,
            "gross_r":    gross_r,
            "net_r":      net_r,
            "cost_r":     cost_r,
            "bars_held":  bars_held,
        })
    return results

# ── Confluence scoring ────────────────────────────────────────────────────────

def confluence_score(sig: dict) -> int:
    ev = sig.get("evidence", {})
    s = 0
    if ev.get("snr_fresh"):          s += 2
    if ev.get("snr_clean"):          s += 1
    v = ev.get("snr_validation", "")
    if v == "engulfing":             s += 2
    elif v == "pin_bar":             s += 1
    if ev.get("snr_rule3_ok"):       s += 1
    if ev.get("in_kill_zone"):       s += 1
    if ev.get("session") in ("london", "new_york"): s += 1
    rr = float(sig.get("risk_reward") or 0)
    if rr >= 3.0:   s += 2
    elif rr >= 2.0: s += 1
    a = ev.get("analyzer", "")
    if a == "confluence": s += 3
    elif a == "marriage": s += 1
    return s

# ── Signal markdown logger ────────────────────────────────────────────────────

def log_signal_md(sig: dict, result: dict | None) -> pathlib.Path:
    ev  = sig.get("evidence", {})
    ts  = datetime.fromtimestamp(sig["entry_time"], tz=timezone.utc)
    fname = f"{sig['symbol']}_{sig['direction']}_{ts.strftime('%Y%m%d_%H%M%S')}_{sig['analyzer'][:6]}.md"
    path = OUT_DIR / fname

    if result:
        emo = "✅" if result["outcome"] == "WIN" else ("❌" if result["outcome"] == "LOSS" else "⏳")
        outcome_block = (
            f"\n## Outcome\n"
            f"**Result:** {emo} {result['outcome']}  \n"
            f"**Net R:** {result['net_r']:+.2f} | **Gross R:** {result['gross_r']:+.2f} | "
            f"**Cost R:** {result['cost_r']:.3f} | **Bars held:** {result['bars_held']}\n"
        )
    else:
        outcome_block = "\n## Outcome\n_Pending_\n"

    score = confluence_score(sig)

    content = f"""# Signal: {sig['symbol']} {sig['direction']} | {ts.strftime('%Y-%m-%d %H:%M')} UTC

## Entry
| Field       | Value        |
|-------------|--------------|
| Entry       | {sig['entry']} |
| Stop Loss   | {sig['stop_loss']} |
| Take Profit | {sig['take_profit']} |
| Risk:Reward | {sig.get('risk_reward', 'N/A')} |
| Confidence  | {sig.get('confidence', 'N/A')} |
| Confluence Score | {score}/12 |

## Analyzer Evidence
| Key                  | Value       |
|----------------------|-------------|
| analyzer             | {ev.get('analyzer', '?')} |
| snr_fresh            | {ev.get('snr_fresh', '?')} |
| snr_clean            | {ev.get('snr_clean', '?')} |
| snr_validation       | {ev.get('snr_validation', '?')} |
| snr_rule3_ok         | {ev.get('snr_rule3_ok', '?')} |
| snr_body_first       | {ev.get('snr_body_first', '?')} |
| in_kill_zone         | {ev.get('in_kill_zone', '?')} |
| session              | {ev.get('session', '?')} |

## Confluence Factors
| Factor               | Present |
|----------------------|---------|
| Fresh level (+2)     | {'✅' if ev.get('snr_fresh') else '❌'} |
| Clean rejection (+1) | {'✅' if ev.get('snr_clean') else '❌'} |
| Engulfing (+2)       | {'✅' if ev.get('snr_validation') == 'engulfing' else '❌'} |
| Pin bar (+1)         | {'✅' if ev.get('snr_validation') == 'pin_bar' else '❌'} |
| Rule3 confirmed (+1) | {'✅' if ev.get('snr_rule3_ok') else '❌'} |
| In kill zone (+1)    | {'✅' if ev.get('in_kill_zone') else '❌'} |
| RR ≥ 2.0 (+1)        | {'✅' if float(sig.get('risk_reward') or 0) >= 2.0 else '❌'} |
| Marriage analyzer (+1) | {'✅' if ev.get('analyzer') == 'marriage' else '❌'} |
| **Total**            | **{score}/12** |
{outcome_block}
---
_Measured: Oct 10–15, 2026 | Data: Yahoo Finance_
"""
    path.write_text(content)
    return path

# ── Metrics helpers ───────────────────────────────────────────────────────────

def compute_metrics(results: list[dict]) -> dict:
    if not results:
        return {"n": 0, "wr": 0.0, "net_r": 0.0, "expectancy": 0.0, "profit_factor": "N/A"}
    wins   = [r for r in results if r["outcome"] == "WIN"]
    losses = [r for r in results if r["outcome"] == "LOSS"]
    n = len(results)
    wr = round(len(wins) / n * 100, 1) if n else 0.0
    net_r = round(sum(r["net_r"] for r in results), 2)
    expectancy = round(net_r / n, 3) if n else 0.0
    gp = sum(r["gross_r"] for r in wins)
    gl = abs(sum(r["gross_r"] for r in losses))
    pf = round(gp / gl, 2) if gl > 0 else ("∞" if gp > 0 else "N/A")
    return {"n": n, "wr": wr, "net_r": net_r, "expectancy": expectancy, "profit_factor": pf}


def group_by(results: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in results:
        groups[str(r.get(key, "?"))].append(r)
    return {k: compute_metrics(v) for k, v in sorted(groups.items())}


def _md_table(metrics_dict: dict, col: str = "Group") -> str:
    rows = [f"| {col} | N | WR% | Net R | Expectancy | Profit Factor |",
            "|-------|---|-----|-------|------------|---------------|"]
    for k, m in metrics_dict.items():
        rows.append(f"| {k} | {m['n']} | {m['wr']}% | {m['net_r']} | {m['expectancy']} | {m['profit_factor']} |")
    return "\n".join(rows)

# ── Report writer ─────────────────────────────────────────────────────────────

def write_report(all_results: list[dict]) -> pathlib.Path:
    overall = compute_metrics(all_results)

    # Confluence filter simulation
    conf_rows = ["| Min Score | N | WR% | Net R | Expectancy | % Signals Kept |",
                 "|-----------|---|-----|-------|------------|----------------|"]
    for sc in [4, 5, 6, 7, 8]:
        filtered = [r for r in all_results if confluence_score(r) >= sc]
        m = compute_metrics(filtered)
        pct = round(len(filtered) / len(all_results) * 100, 0) if all_results else 0
        conf_rows.append(f"| ≥{sc} | {m['n']} | {m['wr']}% | {m['net_r']} | {m['expectancy']} | {pct}% |")

    signal_files = sorted(p.name for p in OUT_DIR.glob("*.md") if p.name != "REPORT.md")
    file_index = "\n".join(f"- [{f}]({f})" for f in signal_files) or "_No signals generated_"

    # Determine recommended threshold
    best_score = 6
    best_wr = 0.0
    for sc in [4, 5, 6, 7, 8]:
        filtered = [r for r in all_results if confluence_score(r) >= sc]
        if len(filtered) >= 3:
            m = compute_metrics(filtered)
            if m["wr"] > best_wr:
                best_wr = m["wr"]
                best_score = sc

    report = f"""# Malaysian SNR Measurement Report
**Period:** Oct 10–15, 2026
**Symbols:** XAUUSD (GC=F), EURUSD, GBPUSD, USDJPY
**Data Source:** Yahoo Finance (15M + 4H OHLC)
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC

---

## Overall Baseline (Unfiltered)

| Metric | Value |
|--------|-------|
| Total Signals | {len(all_results)} |
| Win Rate | **{overall['wr']}%** |
| Net R (total) | {overall['net_r']} |
| Expectancy | {overall['expectancy']} R/trade |
| Profit Factor | {overall['profit_factor']} |

> **Target:** ≥ 70% WR. Current baseline **before** any confluence filtering.

---

## By Symbol

{_md_table(group_by(all_results, 'symbol'), 'Symbol')}

---

## By Analyzer

{_md_table(group_by(all_results, 'analyzer'), 'Analyzer')}

---

## By Session

{_md_table(group_by(all_results, 'session'), 'Session')}

---

## By Direction

{_md_table(group_by(all_results, 'direction'), 'Direction')}

---

## Confluence Filter Simulation (Phase 2 Preview)

{chr(10).join(conf_rows)}

> **Recommended threshold: ≥{best_score}** → {best_wr}% WR
> (highest WR while keeping ≥3 signals for statistical validity)

---

## Key Findings

{"✅ Already at target! Confluence gate will further improve it." if overall['wr'] >= 70 else f"⚠️  Baseline WR is {overall['wr']}% — below 70% target. Confluence gate at ≥{best_score} should close the gap."}

### Recommended Actions (from plan Phase 2–3):
1. Set `CONFLUENCE_REQUIRED = {best_score}` — drop signals below this score
2. Drop any analyzer with WR < 50% in the By Analyzer table above
3. Prioritise **marriage** and **confluence** analyzer signals (highest expected WR)
4. Enable kill-zone-only trading for the weakest session

---

## Signal Files (one per signal)

{file_index}
"""

    path = OUT_DIR / "REPORT.md"
    path.write_text(report)
    return path

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Malaysian SNR Measurement — Oct 10–15, 2026")
    print("Data: Yahoo Finance (15M + 4H OHLC)")
    print("=" * 60)

    all_results: list[dict] = []

    for yf_ticker, symbol in YF_SYMBOLS.items():
        print(f"\n{'─'*50}")
        print(f"Symbol: {symbol} ({yf_ticker})")

        # Fetch 4H and 15M data
        h4_bars  = fetch_ohlc(yf_ticker, "1h",  START_STR, END_STR)  # use 1H then aggregate to 4H
        m15_bars = fetch_ohlc(yf_ticker, "15m", START_STR, END_STR)

        if not h4_bars or not m15_bars:
            print(f"  [SKIP] No data for {symbol}")
            continue

        # Aggregate 1H → 4H
        buckets: dict[int, dict] = {}
        for b in h4_bars:
            ts = int(b["open_time"].timestamp())
            bucket = (ts // H4_SECS) * H4_SECS
            if bucket not in buckets:
                buckets[bucket] = {
                    "symbol": symbol, "timeframe": H4_SECS,
                    "open_time": datetime.fromtimestamp(bucket, tz=timezone.utc),
                    "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                }
            else:
                c = buckets[bucket]
                c["high"]  = max(c["high"], b["high"])
                c["low"]   = min(c["low"],  b["low"])
                c["close"] = b["close"]
        h4_bars_agg = sorted(buckets.values(), key=lambda x: x["open_time"])
        print(f"  4H bars (aggregated from 1H): {len(h4_bars_agg)}")
        print(f"  15M bars: {len(m15_bars)}")

        if len(h4_bars_agg) < 10 or len(m15_bars) < 30:
            print(f"  [SKIP] Insufficient bars ({len(h4_bars_agg)} 4H, {len(m15_bars)} 15M)")
            continue

        # Run signal engine
        print(f"  Running SNR engine ...", flush=True)
        signals = run_signals(symbol, h4_bars_agg, m15_bars)
        print(f"  Generated {len(signals)} unique signals")

        if not signals:
            print(f"  [INFO] No signals generated — not enough pattern setup in this 6-day window")
            continue

        # Simulate exits
        costs = COSTS.get(symbol, {"spread": 0.0002, "slippage": 0.00003})
        results = simulate_exits(signals, m15_bars, costs)
        print(f"  Resolved {len(results)} exits")

        # Log each signal to .md
        for sig in signals:
            result = next(
                (r for r in results
                 if r["entry_time"] == sig["entry_time"] and r["direction"] == sig["direction"]),
                None,
            )
            log_signal_md(sig, result)

        all_results.extend(results)

        # Per-symbol summary
        m = compute_metrics(results)
        print(f"  {symbol}: N={m['n']} WR={m['wr']}% NetR={m['net_r']} Exp={m['expectancy']}R")

    # Final report
    print(f"\n{'='*60}")
    print("FINAL RESULTS")
    print("=" * 60)
    overall = compute_metrics(all_results)
    print(f"  Total signals: {len(all_results)}")
    print(f"  Win Rate:      {overall['wr']}%  (target ≥ 70%)")
    print(f"  Net R:         {overall['net_r']}")
    print(f"  Expectancy:    {overall['expectancy']} R/trade")
    print(f"  Profit Factor: {overall['profit_factor']}")

    report_path = write_report(all_results)
    print(f"\n  Report → {report_path}")
    print(f"  Signals → {OUT_DIR}/")
    print("Done.")


if __name__ == "__main__":
    main()
