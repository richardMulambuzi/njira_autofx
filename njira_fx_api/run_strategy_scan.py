#!/usr/bin/env python3
"""Scan every registered strategy on real historical candles and report signals.

Runs each strategy twice: once sequentially on its own engine, once across
independent threads so the whole roster executes at the same time. The two
passes are compared to show whether concurrency changes the result.

Usage (from njira_fx_api/):
    DJANGO_SETTINGS_MODULE=forex.settings ./myvenv/bin/python run_strategy_scan.py
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone


def _bootstrap_django() -> None:
    """Load Django so imports that touch models (ClickHouse loggers) resolve."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "forex.settings")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import django

        django.setup()
    except Exception as exc:  # noqa: BLE001 - scan must run without a DB
        print(f"[warn] django.setup() failed, continuing: {exc}")


_bootstrap_django()

from forex.analysis.cache import HotCache  # noqa: E402
from forex.analysis.signal_engine import SignalEngine  # noqa: E402
from forex.analysis.strategy import StrategyRegistry  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "historical-data", "XAUUSD_2026")

# timeframe seconds -> file suffix. Only the frames the engine actually reads.
TIMEFRAME_FILES = {
    60: "M1",
    300: "M5",
    900: "M15",
    1800: "M30",
    3600: "H1",
    14400: "H4",
    86400: "D1",
}

SYMBOL = "XAUUSD"
PREFIX = "XAUUSD_OCT01-16_2026"


def load_csv(path: str, symbol: str, tf: int) -> list[dict]:
    """Parse the repo's historical export: `YYYY.MM.DD,HH:MM,O,H,L,C,V` (UTC)."""
    candles: list[dict] = []
    with open(path) as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) < 6:
                continue
            day, clock = parts[0], parts[1]
            try:
                year, month, dom = (int(x) for x in day.split("."))
                hour, minute = (int(x) for x in clock.split(":"))
                open_time = datetime(year, month, dom, hour, minute, tzinfo=timezone.utc)
                candles.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "open_time": open_time,
                        "open": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                        "close": float(parts[5]),
                        "volume": float(parts[6]) if len(parts) > 6 else 0.0,
                    }
                )
            except ValueError:
                continue
    candles.sort(key=lambda c: c["open_time"])
    return candles


def build_engine(cache: HotCache) -> SignalEngine:
    """Wire the full engine set exactly as the backtest/debug path does."""
    from forex.analysis.context import MarketContextEngine
    from forex.analysis.fvg import FVGEngine
    from forex.analysis.liquidity import LiquidityEngine
    from forex.analysis.market_structure import MarketStructureEngine
    from forex.analysis.microstructure import MicrostructureEngine
    from forex.analysis.order_blocks import OrderBlockEngine
    from forex.analysis.poc import POCEngine
    from forex.analysis.retest import RetestEngine
    from forex.analysis.session import SessionEngine
    from forex.analysis.spread import SpreadEngine
    from forex.analysis.volatility import VolatilityEngine
    from forex.analysis.volume_profile import VolumeProfileEngine

    tfs = [60, 300, 900, 14400, 86400]
    structure_engines = {tf: MarketStructureEngine() for tf in tfs}
    liquidity_engines = {tf: LiquidityEngine() for tf in tfs}
    ob_engines = {tf: OrderBlockEngine() for tf in tfs}
    fvg_engines = {tf: FVGEngine() for tf in tfs}
    poc_engines = {tf: POCEngine() for tf in tfs}

    microstructure = MicrostructureEngine()
    spread = SpreadEngine()
    volatility = VolatilityEngine()
    session = SessionEngine()
    volume_profile = VolumeProfileEngine()

    context_engine = MarketContextEngine(
        structure_engines=structure_engines,
        liquidity_engines=liquidity_engines,
        ob_engines=ob_engines,
        fvg_engines=fvg_engines,
        poc_engines=poc_engines,
        microstructure_engine=microstructure,
        volatility_engine=volatility,
        session_engine=session,
        spread_engine=spread,
        volume_profile_engine=volume_profile,
        cache=cache,
    )

    return SignalEngine(
        cache=cache,
        structure_engines=structure_engines,
        liquidity_engines=liquidity_engines,
        ob_engines=ob_engines,
        fvg_engines=fvg_engines,
        poc_engines=poc_engines,
        microstructure_engine=microstructure,
        volatility_engine=volatility,
        session_engine=session,
        spread_engine=spread,
        volume_profile_engine=volume_profile,
        context_engine=context_engine,
        retest_engine=RetestEngine(cache),
        min_confidence=30,
        min_rr=1.5,
    )


def raw_records(engine: SignalEngine, name: str) -> tuple[int, str]:
    """Count detector records for a strategy before the emitter touches them."""
    if not name.startswith("ict_"):
        return (-1, "")
    try:
        cls = StrategyRegistry.get(name)
        return (len(cls()._records(SYMBOL, engine)), "")
    except Exception as exc:  # noqa: BLE001 - report, never abort the scan
        return (-1, f"{type(exc).__name__}: {exc}")


def describe(sig) -> str:
    return (
        f"{sig.direction:<4} conf={sig.confidence_score:5.1f} {sig.strength:<11} "
        f"entry={sig.entry:.2f} sl={sig.stop_loss:.2f} tp={sig.take_profit:.2f} "
        f"rr={sig.risk_reward_ratio:.2f}"
    )


def run_one(engine: SignalEngine, name: str) -> dict:
    """Evaluate one strategy and capture its outcome, never raising."""
    started = time.perf_counter()
    records, rec_err = raw_records(engine, name)
    try:
        signals = engine.evaluate(SYMBOL, name)
        return {
            "name": name,
            "records": records,
            "record_error": rec_err,
            "count": len(signals),
            "signals": signals,
            "error": "",
            "ms": (time.perf_counter() - started) * 1000,
        }
    except Exception as exc:  # noqa: BLE001 - the scan reports, it does not fail
        import traceback

        return {
            "name": name,
            "records": records,
            "record_error": rec_err,
            "count": -1,
            "signals": [],
            "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            "ms": (time.perf_counter() - started) * 1000,
        }


def report(title: str, results: list[dict]) -> int:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print(f"{'strategy':<28}{'recs':>5}{'sigs':>6}{'ms':>8}  status")
    print("-" * 78)
    total = 0
    for r in sorted(results, key=lambda x: x["name"]):
        recs = "-" if r["records"] < 0 else str(r["records"])
        if r["error"]:
            status = "EXCEPTION"
        elif r["record_error"]:
            status = f"record error: {r['record_error'][:40]}"
        elif r["count"]:
            status = "SIGNAL"
        else:
            status = "no signal"
        if r["count"] > 0:
            total += r["count"]
        print(f"{r['name']:<28}{recs:>5}{r['count']:>6}{r['ms']:>8.0f}  {status}")
    print("-" * 78)
    print(f"{'TOTAL':<28}{'':>5}{total:>6}")
    for r in sorted(results, key=lambda x: x["name"]):
        for sig in r["signals"]:
            print(f"    {r['name']:<26} {describe(sig)}")
    for r in sorted(results, key=lambda x: x["name"]):
        if r["error"]:
            print(f"\n    !! {r['name']} raised:\n{r['error']}")
    return total


def main() -> int:
    cache = HotCache()
    for tf, suffix in TIMEFRAME_FILES.items():
        path = os.path.join(DATA_DIR, f"{PREFIX}_{suffix}.csv")
        if not os.path.exists(path):
            print(f"[warn] missing {path}")
            continue
        candles = load_csv(path, SYMBOL, tf)
        cache.set_candles(SYMBOL, tf, candles)
        print(f"[data] {SYMBOL} tf={tf:<6} {len(candles):>6} candles")

    names = sorted(StrategyRegistry.names())
    print(f"[data] {len(names)} registered strategies: {', '.join(names)}")

    print("\n[build] constructing one engine per strategy (independent state)...")
    engines = {name: build_engine(cache) for name in names}

    t0 = time.perf_counter()
    sequential = [run_one(engines[name], name) for name in names]
    sequential_ms = (time.perf_counter() - t0) * 1000
    seq_total = report(f"PASS 1 - SEQUENTIAL  ({sequential_ms:.0f} ms wall)", sequential)

    t0 = time.perf_counter()
    threaded: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(names), thread_name_prefix="strat") as pool:
        futures = {pool.submit(run_one, engines[name], name): name for name in names}
        for fut in as_completed(futures):
            threaded.append(fut.result())
    threaded_ms = (time.perf_counter() - t0) * 1000
    thr_total = report(f"PASS 2 - CONCURRENT  ({threaded_ms:.0f} ms wall)", threaded)

    print(f"\n{'=' * 78}\nVERDICT\n{'=' * 78}")
    print(f"sequential: {seq_total} signals in {sequential_ms:.0f} ms")
    print(f"concurrent: {thr_total} signals in {threaded_ms:.0f} ms")
    seq_by = {r["name"]: r["count"] for r in sequential}
    thr_by = {r["name"]: r["count"] for r in threaded}
    diffs = [n for n in names if seq_by.get(n) != thr_by.get(n)]
    if diffs:
        print(f"[!] concurrency changed the result for: {', '.join(diffs)}")
    else:
        print("[ok] concurrency produced the same counts as sequential")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
