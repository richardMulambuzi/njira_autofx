#!/usr/bin/env python3
"""
Deep debug SNR analysis - trace each step.
"""
import os
import sys
import logging

os.environ["SNR_MALAYSIAN_ANALYZERS"] = "snr,breakout"

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logging.getLogger("forex.backtest.engine").setLevel(logging.WARNING)
logging.getLogger("forex.analysis.cache").setLevel(logging.WARNING)
logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)

sys.path.insert(0, "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api")

from forex.backtest.parser import parse_tf_csv
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.cache import HotCache


def load_data():
    m1_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_M1.csv"
    h4_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_H4.csv"
    
    with open(m1_path) as f:
        m1_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=60)
    
    with open(h4_path) as f:
        h4_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=14400)
    
    return m1_candles, h4_candles


def debug_step_by_step(m1_candles, h4_candles):
    """Trace each step of the SNR analysis."""
    from forex.analysis.signal_engine import SignalEngine, SNR_TOUCH_PCT, SNR_CLEAN_PCT, SNR_REJECTION_OFFSET
    from forex.analysis.engulfing import detect_engulfing
    
    cache = HotCache()
    cache.set_candles("XAUUSD", 60, m1_candles)
    cache.set_candles("XAUUSD", 14400, h4_candles)
    
    # Get raw H4 candles
    raw_h4 = cache.get("XAUUSD", 14400)
    print(f"\n=== H4 CANDLES ({len(raw_h4)}) ===")
    for i, c in enumerate(raw_h4[-10:]):
        print(f"  [{len(raw_h4)-10+i}] {c['open_time']} O={c['open']:.5f} H={c['high']:.5f} L={c['low']:.5f} C={c['close']:.5f} {'BULL' if c['close']>c['open'] else 'BEAR'}")
    
    # Test _snr_levels for support and resistance
    print("\n=== SNR LEVELS (support) ===")
    support_levels = SignalEngine._snr_levels(raw_h4, "support", max_levels=5)
    for lvl in support_levels:
        print(f"  idx={lvl['index']} t1={lvl['t1']:.0f} y1={lvl['y1']:.5f} t2={lvl['t2']:.0f} y2={lvl['y2']:.5f}")
    
    print("\n=== SNR LEVELS (resistance) ===")
    resistance_levels = SignalEngine._snr_levels(raw_h4, "resistance", max_levels=5)
    for lvl in resistance_levels:
        print(f"  idx={lvl['index']} t1={lvl['t1']:.0f} y1={lvl['y1']:.5f} t2={lvl['t2']:.0f} y2={lvl['y2']:.5f}")
    
    # Test _snr_tradable_trendline
    print("\n=== TRADABLE TRENDLINES ===")
    support = SignalEngine._snr_tradable_trendline(raw_h4, "support")
    resistance = SignalEngine._snr_tradable_trendline(raw_h4, "resistance")
    print(f"  Support: {support}")
    print(f"  Resistance: {resistance}")
    
    if support:
        print(f"\n=== CHECK REJECTION (BUY/support) ===")
        rej = SignalEngine._snr_check_rejection(raw_h4, support, "BUY")
        print(f"  Rejection: {rej}")
        if rej:
            print(f"    validation: {rej.get('validation')}")
            print(f"    body_first: {rej.get('body_first')}")
            print(f"    rule3_ok: {rej.get('rule3_ok')}")
            print(f"    rule3_pending: {rej.get('rule3_pending')}")
            print(f"    fresh: {rej.get('fresh')}")
            print(f"    clean: {rej.get('clean')}")
            print(f"    validated (MISS): {rej.get('validated')}")
            # Check the rejection candle
            idx = rej['candle_index']
            c = raw_h4[idx]
            print(f"    Rejection candle [{idx}]: {c['open_time']} O={c['open']:.5f} H={c['high']:.5f} L={c['low']:.5f} C={c['close']:.5f}")
            print(f"    Line value at rejection: {rej['price']:.5f}")
            print(f"    Tolerance: {rej['price'] * SNR_TOUCH_PCT:.5f}")
            print(f"    Low <= line+tol: {c['low']:.5f} <= {rej['price'] + rej['price'] * SNR_TOUCH_PCT:.5f} = {c['low'] <= rej['price'] + rej['price'] * SNR_TOUCH_PCT}")
            print(f"    Close > line: {c['close']:.5f} > {rej['price']:.5f} = {c['close'] > rej['price']}")
            print(f"    Clean (>0.1%): {c['close']:.5f} > {rej['price'] * (1 + SNR_CLEAN_PCT):.5f} = {c['close'] > rej['price'] * (1 + SNR_CLEAN_PCT)}")
    
    if resistance:
        print(f"\n=== CHECK REJECTION (SELL/resistance) ===")
        rej = SignalEngine._snr_check_rejection(raw_h4, resistance, "SELL")
        print(f"  Rejection: {rej}")
        if rej:
            print(f"    validation: {rej.get('validation')}")
            print(f"    body_first: {rej.get('body_first')}")
            print(f"    rule3_ok: {rej.get('rule3_ok')}")
            print(f"    rule3_pending: {rej.get('rule3_pending')}")
            print(f"    fresh: {rej.get('fresh')}")
            print(f"    clean: {rej.get('clean')}")
            print(f"    validated (MISS): {rej.get('validated')}")
            idx = rej['candle_index']
            c = raw_h4[idx]
            print(f"    Rejection candle [{idx}]: {c['open_time']} O={c['open']:.5f} H={c['high']:.5f} L={c['low']:.5f} C={c['close']:.5f}")
            print(f"    Line value at rejection: {rej['price']:.5f}")
            print(f"    Tolerance: {rej['price'] * SNR_TOUCH_PCT:.5f}")
            print(f"    High >= line-tol: {c['high']:.5f} >= {rej['price'] - rej['price'] * SNR_TOUCH_PCT:.5f} = {c['high'] >= rej['price'] - rej['price'] * SNR_TOUCH_PCT}")
            print(f"    Close < line: {c['close']:.5f} < {rej['price']:.5f} = {c['close'] < rej['price']}")
            print(f"    Clean (>0.1%): {c['close']:.5f} < {rej['price'] * (1 - SNR_CLEAN_PCT):.5f} = {c['close'] < rej['price'] * (1 - SNR_CLEAN_PCT)}")
    
    # Now test the book gate
    if support:
        rej = SignalEngine._snr_check_rejection(raw_h4, support, "BUY")
        if rej:
            print(f"\n=== BOOK GATE (BUY/support) ===")
            gate = SignalEngine._snr_book_gate("XAUUSD", rej)
            print(f"  Gate result: '{gate}' (empty = pass)")
    
    if resistance:
        rej = SignalEngine._snr_check_rejection(raw_h4, resistance, "SELL")
        if rej:
            print(f"\n=== BOOK GATE (SELL/resistance) ===")
            gate = SignalEngine._snr_book_gate("XAUUSD", rej)
            print(f"  Gate result: '{gate}' (empty = pass)")
    
    # Test the book validation (c+2) path
    print("\n=== BOOK VALIDATION (c+2) ===")
    from forex.analysis.signal_engine import SNR_BOOK_VALIDATION_OFFSET
    for kind, direction in [("support", "BUY"), ("resistance", "SELL")]:
        levels = SignalEngine._snr_levels(raw_h4, kind)
        print(f"\n  {kind} levels: {len(levels)}")
        for level in levels:
            hit = SignalEngine._snr_book_validation(raw_h4, level, direction)
            if hit:
                print(f"    Level idx={level['index']}: VALIDATED at c+2 (idx={hit['candle_index']})")
                print(f"      price={hit['price']:.5f} clean={hit['clean']}")
                c = raw_h4[hit['candle_index']]
                print(f"      Candle: {c['open_time']} O={c['open']:.5f} H={c['high']:.5f} L={c['low']:.5f} C={c['close']:.5f}")
            else:
                print(f"    Level idx={level['index']}: NO VALIDATION at c+2")
    
    # Test 15M confirmation
    raw_m15 = cache.get("XAUUSD", 900)
    print(f"\n=== M15 CANDLES ({len(raw_m15)}) ===")
    for i, c in enumerate(raw_m15[-10:]):
        print(f"  [{len(raw_m15)-10+i}] {c['open_time']} O={c['open']:.5f} H={c['high']:.5f} L={c['low']:.5f} C={c['close']:.5f}")
    
    if support:
        rej = SignalEngine._snr_check_rejection(raw_h4, support, "BUY")
        if rej:
            print(f"\n=== 15M CONFIRMATION (BUY) ===")
            rej_time = SignalEngine._epoch(rej.get("open_time"))
            conf = SignalEngine._snr_m15_confirmation(raw_m15, rej["trendline"], "BUY", rej_time)
            print(f"  Confirmation: {conf}")
    
    if resistance:
        rej = SignalEngine._snr_check_rejection(raw_h4, resistance, "SELL")
        if rej:
            print(f"\n=== 15M CONFIRMATION (SELL) ===")
            rej_time = SignalEngine._epoch(rej.get("open_time"))
            conf = SignalEngine._snr_m15_confirmation(raw_m15, rej["trendline"], "SELL", rej_time)
            print(f"  Confirmation: {conf}")


def main():
    m1_candles, h4_candles = load_data()
    debug_step_by_step(m1_candles, h4_candles)


if __name__ == "__main__":
    main()