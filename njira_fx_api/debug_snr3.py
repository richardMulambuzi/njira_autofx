#!/usr/bin/env python3
"""
Debug with full backtest pipeline - check CandleRegistry aggregation.
"""
import os
import sys
import logging

os.environ["SNR_MALAYSIAN_ANALYZERS"] = "snr,breakout"

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logging.getLogger("forex.backtest.engine").setLevel(logging.DEBUG)
logging.getLogger("forex.analysis.cache").setLevel(logging.WARNING)
logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)

sys.path.insert(0, "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api")

from forex.backtest.parser import parse_tf_csv
from forex.backtest.engine import BacktestEngine
from forex.analysis.candle_engine import CandleRegistry
from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine


def load_data():
    m1_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_M1.csv"
    h4_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_H4.csv"
    
    with open(m1_path) as f:
        m1_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=60)
    
    with open(h4_path) as f:
        h4_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=14400)
    
    return m1_candles, h4_candles


def test_candle_registry(m1_candles):
    """Test that CandleRegistry properly aggregates M15 from M1."""
    print("=== Testing CandleRegistry M15 aggregation ===")
    registry = CandleRegistry(timeframes=[60, 300, 900, 3600, 14400, 86400])
    
    # Feed M1 candles as ticks (same as backtest engine does)
    for c in m1_candles:
        t0 = int(c["open_time"].timestamp())
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        for price, offset in [(o, 0), (h, 15), (l, 30), (cl, 45)]:
            registry.feed_tick("XAUUSD", price, price, (t0 + offset) * 1000)
    
    # Check M15 candles
    agg = registry.get_aggregator("XAUUSD")
    m15 = agg.get_closed(900) if agg else []
    print(f"M15 candles aggregated: {len(m15)}")
    for c in m15[-5:]:
        print(f"  {c['open_time']} O={c['open']:.5f} H={c['high']:.5f} L={c['low']:.5f} C={c['close']:.5f}")
    
    # Also check other timeframes
    for tf in [60, 300, 900, 3600, 14400]:
        candles = agg.get_closed(tf) if agg else []
        print(f"TF {tf}s: {len(candles)} candles")
    
    return registry


def run_backtest_with_debug(m1_candles, h4_candles):
    """Run backtest with debug output."""
    print("\n=== Running BacktestEngine ===")
    
    # Monkey-patch SignalEngine._evaluate_snr
    original_evaluate_snr = SignalEngine._evaluate_snr
    
    def debug_evaluate_snr(self, symbol):
        print(f"\n[DEBUG] SignalEngine._evaluate_snr called for {symbol}")
        print(f"  Cache M1: {len(self._cache.get(symbol, 60))}")
        print(f"  Cache M5: {len(self._cache.get(symbol, 300))}")
        print(f"  Cache M15: {len(self._cache.get(symbol, 900))}")
        print(f"  Cache H1: {len(self._cache.get(symbol, 3600))}")
        print(f"  Cache H4: {len(self._cache.get(symbol, 14400))}")
        print(f"  Cache D1: {len(self._cache.get(symbol, 86400))}")
        
        # Check structure engines
        for tf, eng in self._structure.items():
            print(f"  Structure {tf}s: {len(eng.get_events(tf, n=100))} events")
        
        result = original_evaluate_snr(self, symbol)
        print(f"  Signals returned: {len(result)}")
        for s in result:
            print(f"    {s.strategy} {s.direction} entry={s.entry:.5f} conf={s.confidence_score:.1f}")
        return result
    
    SignalEngine._evaluate_snr = debug_evaluate_snr
    
    engine = BacktestEngine(strategy_mode="malysian", fast=False)
    
    result = engine.run(
        candles_1m=m1_candles,
        symbol="XAUUSD",
        strategies=["malysian"],
        initial_balance=10000.0,
        risk_per_trade=0.01,
        higher_tf_candles={14400: h4_candles},
        base_timeframe=60,
    )
    
    return result


def main():
    m1_candles, h4_candles = load_data()
    
    # Test CandleRegistry
    registry = test_candle_registry(m1_candles)
    
    # Run backtest
    result = run_backtest_with_debug(m1_candles, h4_candles)
    
    print(f"\n=== Result ===")
    print(f"Trades: {len(result.trades)}")
    for t in result.trades:
        print(f"  {t['strategy']} {t['direction']} entry={t['entry']:.5f} outcome={t['outcome']}")


if __name__ == "__main__":
    main()