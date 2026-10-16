#!/usr/bin/env python3
"""
Deep debug - trace SNR candidate generation during replay.
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
    h4_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_H4.csv"
    
    with open(m1_path) as f:
        m1_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=60)
    
    with open(h4_path) as f:
        h4_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=14400)
    
    return m1_candles, h4_candles


def main():
    m1_candles, h4_candles = load_data()
    
    # Patch _build_snr_draft to trace each step
    original_build_snr_draft = SignalEngine._build_snr_draft
    
    call_count = [0]
    
    def debug_build_snr_draft(self, symbol):
        call_count[0] += 1
        if call_count[0] % 100 == 1:  # Log first call and every 100th
            h4 = self._cache.get(symbol, 14400)
            m15 = self._cache.get(symbol, 900)
            print(f"\n=== _build_snr_draft call #{call_count[0]} ===")
            print(f"  H4 candles: {len(h4)}, M15 candles: {len(m15)}")
            if h4:
                print(f"  Last H4: {h4[-1]['open_time']} idx={len(h4)-1}")
            
            support = self._snr_tradable_trendline(h4, "support")
            resistance = self._snr_tradable_trendline(h4, "resistance")
            print(f"  Support trendline: {support['index'] if support else None}")
            print(f"  Resistance trendline: {resistance['index'] if resistance else None}")
            
            if support:
                rej = self._snr_check_rejection(h4, support, "BUY")
                print(f"  BUY rejection: {rej is not None}")
                if rej:
                    print(f"    idx={rej['candle_index']} validation={rej.get('validation')} body_first={rej.get('body_first')} rule3_ok={rej.get('rule3_ok')} fresh={rej.get('fresh')}")
                    dropped = self._snr_book_gate(symbol, rej)
                    print(f"    book_gate: '{dropped}'")
            
            if resistance:
                rej = self._snr_check_rejection(h4, resistance, "SELL")
                print(f"  SELL rejection: {rej is not None}")
                if rej:
                    print(f"    idx={rej['candle_index']} validation={rej.get('validation')} body_first={rej.get('body_first')} rule3_ok={rej.get('rule3_ok')} fresh={rej.get('fresh')}")
                    dropped = self._snr_book_gate(symbol, rej)
                    print(f"    book_gate: '{dropped}'")
        
        return original_build_snr_draft(self, symbol)
    
    SignalEngine._build_snr_draft = debug_build_snr_draft
    
    # Also patch _build_snr_setup to trace 15M confirmation
    original_build_snr_setup = SignalEngine._build_snr_setup
    
    def debug_build_snr_setup(self, m15, rej, symbol=""):
        result = original_build_snr_setup(self, m15, rej, symbol)
        if result is None and call_count[0] % 100 == 1:
            print(f"    _build_snr_setup returned None")
            # Check 15M confirmation
            h4_level = rej["trendline"]
            rej_time = self._epoch(rej.get("open_time")) or 0.0
            conf = self._snr_m15_confirmation(m15, h4_level, rej["direction"], rej_time)
            print(f"    15M confirmation: {conf is not None}")
            if conf:
                print(f"      entry_index={conf['entry_index']} retest_index={conf['retest_index']}")
                print(f"      M15 len={len(m15)} SNR_BREAKOUT_MAX_AGE={self.SNR_BREAKOUT_MAX_AGE}")
                print(f"      Live edge check: {conf['entry_index']} < {len(m15)} - {self.SNR_BREAKOUT_MAX_AGE} = {conf['entry_index'] < len(m15) - self.SNR_BREAKOUT_MAX_AGE}")
        return result
    
    SignalEngine._build_snr_setup = debug_build_snr_setup
    
    # Patch _snr_book_gate to log every rejection
    original_snr_book_gate = SignalEngine._snr_book_gate
    
    def debug_snr_book_gate(self, symbol, rej):
        result = original_snr_book_gate(self, symbol, rej)
        if result:
            print(f"      [DROPPED] {rej['direction']} at idx={rej['candle_index']}: {result}")
        return result
    
    SignalEngine._snr_book_gate = debug_snr_book_gate
    
    # Run backtest
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


if __name__ == "__main__":
    main()