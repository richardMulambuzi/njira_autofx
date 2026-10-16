#!/usr/bin/env python3
"""
Run backtest WITHOUT pre-loaded H4 - let CandleRegistry aggregate from M1.
"""
import os
import sys
import logging

os.environ["SNR_MALAYSIAN_ANALYZERS"] = "snr,breakout"

logging.basicConfig(level=logging.INFO, format='%(message)s')
logging.getLogger("forex.backtest.engine").setLevel(logging.WARNING)
logging.getLogger("forex.analysis.cache").setLevel(logging.WARNING)
logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)

sys.path.insert(0, "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api")

from forex.backtest.parser import parse_tf_csv
from forex.backtest.engine import BacktestEngine
from forex.analysis.signal_engine import SignalEngine, bump_funnel, funnel_snapshot


def load_data():
    m1_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_M1.csv"
    
    with open(m1_path) as f:
        m1_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=60)
    
    return m1_candles


def main():
    m1_candles = load_data()
    
    # Patch to trace SNR
    original_build_snr_draft = SignalEngine._build_snr_draft
    call_count = [0]
    
    def debug_build_snr_draft(self, symbol):
        call_count[0] += 1
        if call_count[0] % 200 == 1:
            h4 = self._cache.get(symbol, 14400)
            m15 = self._cache.get(symbol, 900)
            print(f"\n=== _build_snr_draft call #{call_count[0]} ===")
            print(f"  H4 candles: {len(h4)}, M15 candles: {len(m15)}")
            if h4:
                print(f"  Last H4: {h4[-1]['open_time']} idx={len(h4)-1}")
            
            support = self._snr_tradable_trendline(h4, "support")
            resistance = self._snr_tradable_trendline(h4, "resistance")
            print(f"  Support: {support['index'] if support else None}")
            print(f"  Resistance: {resistance['index'] if resistance else None}")
            
            if support:
                rej = self._snr_check_rejection(h4, support, "BUY")
                if rej:
                    dropped = self._snr_book_gate(symbol, rej)
                    print(f"  BUY rej idx={rej['candle_index']} gate='{dropped}'")
            
            if resistance:
                rej = self._snr_check_rejection(h4, resistance, "SELL")
                if rej:
                    dropped = self._snr_book_gate(symbol, rej)
                    print(f"  SELL rej idx={rej['candle_index']} gate='{dropped}'")
        
        return original_build_snr_draft(self, symbol)
    
    SignalEngine._build_snr_draft = debug_build_snr_draft
    
    # Run backtest WITHOUT higher_tf_candles
    engine = BacktestEngine(strategy_mode="malysian", fast=False)
    
    result = engine.run(
        candles_1m=m1_candles,
        symbol="XAUUSD",
        strategies=["malysian"],
        initial_balance=10000.0,
        risk_per_trade=0.01,
        # NO higher_tf_candles - let CandleRegistry aggregate everything
        base_timeframe=60,
    )
    
    # Print funnel
    print("\n=== FINAL FUNNEL ===")
    funnel = funnel_snapshot()
    for sym, counts in funnel.items():
        print(f"\n{sym}:")
        for stage, count in counts.items():
            if count > 0:
                print(f"  {stage}: {count}")
    
    print(f"\n=== Result ===")
    print(f"Trades: {len(result.trades)}")
    for t in result.trades:
        print(f"  {t['strategy']} {t['direction']} entry={t['entry']:.5f} outcome={t['outcome']} pnl={t['pnl']:.2f}")


if __name__ == "__main__":
    main()