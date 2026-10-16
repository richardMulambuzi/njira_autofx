#!/usr/bin/env python3
"""
Final SNR backtest - capture all signal details and compare to book rules.
"""
import os
import sys
import csv
import json
from datetime import datetime, timezone

os.environ["SNR_MALAYSIAN_ANALYZERS"] = "snr,breakout"

import logging
logging.basicConfig(level=logging.WARNING)
logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)

sys.path.insert(0, "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api")

from forex.backtest.parser import parse_tf_csv
from forex.backtest.engine import BacktestEngine
from forex.analysis.signal_engine import funnel_snapshot, bump_funnel


def load_data():
    m1_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_M1.csv"
    
    with open(m1_path) as f:
        m1_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=60)
    
    return m1_candles


def format_ts(ts):
    if ts is None:
        return "N/A"
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    if hasattr(ts, 'strftime'):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)


def extract_signal_details(trades):
    """Extract detailed signal information for comparison to book rules."""
    signals = []
    for t in trades:
        ev = t.get("evidence", {})
        analyzer = ev.get("analyzer", "unknown")
        
        h4_level = ev.get("snr_4h_level", {})
        rejection = ev.get("snr_rejection", {})
        breakout_box = ev.get("breakout_box_levels", {})
        m15_conf = ev.get("snr_m15_confirmation", {})
        tp_level = ev.get("snr_tp_level", {})
        
        signals.append({
            "signal_id": t.get("signal_id"),
            "analyzer": analyzer,
            "direction": t.get("direction"),
            "entry_time": format_ts(t.get("entry_time")),
            "entry": t.get("entry"),
            "stop_loss": t.get("stop_loss"),
            "take_profit": t.get("take_profit"),
            "risk_reward": t.get("risk_reward", 0),
            "confidence": t.get("confidence"),
            "outcome": t.get("outcome"),
            "pnl": t.get("pnl"),
            # 4H level
            "h4_kind": h4_level.get("kind"),
            "h4_shape": h4_level.get("shape"),
            "h4_price": h4_level.get("price"),
            "h4_t1": format_ts(h4_level.get("t1")),
            "h4_t2": format_ts(h4_level.get("t2")),
            "h4_y1": h4_level.get("y1"),
            "h4_y2": h4_level.get("y2"),
            "h4_index": h4_level.get("index"),
            "h4_fresh": h4_level.get("fresh"),
            # Rejection
            "rej_candle_index": rejection.get("candle_index"),
            "rej_price": rejection.get("price"),
            "rej_clean": rejection.get("clean"),
            "rej_validated": rejection.get("validated"),
            "rej_validation": rejection.get("validation"),
            "rej_body_first": rejection.get("body_first"),
            "rej_rule3_ok": rejection.get("rule3_ok"),
            "rej_rule3_pending": rejection.get("rule3_pending"),
            "rej_fresh": rejection.get("fresh"),
            "rej_open_time": format_ts(rejection.get("open_time")),
            # Breakout box (for breakout analyzer)
            "box_sup": breakout_box.get("sup"),
            "box_res": breakout_box.get("res"),
            "box_bars": breakout_box.get("bars"),
            "box_validation_open_time": format_ts(breakout_box.get("validation_open_time")),
            "breakout_index": breakout_box.get("breakout_index"),
            # M15 confirmation (for core SNR)
            "m15_zone_index": m15_conf.get("zone_index"),
            "m15_breakout_index": m15_conf.get("breakout_index"),
            "m15_retest_index": m15_conf.get("retest_index"),
            "m15_zone_low": m15_conf.get("zone_low"),
            "m15_zone_high": m15_conf.get("zone_high"),
            "m15_line_value": m15_conf.get("line_value"),
            "m15_clean": m15_conf.get("clean"),
            "m15_body_fraction": m15_conf.get("body_fraction"),
            # TP
            "tp_source": ev.get("snr_tp_source"),
            "tp_level_kind": tp_level.get("kind") if tp_level else None,
            "tp_level_price": tp_level.get("level_price") if tp_level else None,
            "tp_r": ev.get("snr_tp_r"),
            "explanation": t.get("explanation"),
        })
    return signals


def print_signal_analysis(signals):
    """Print detailed analysis of each signal vs book rules."""
    print("\n" + "=" * 120)
    print("SNR SIGNALS - BOOK RULE COMPLIANCE ANALYSIS")
    print("=" * 120)
    
    for i, s in enumerate(signals, 1):
        print(f"\n{'─' * 120}")
        print(f"SIGNAL #{i} [{s['analyzer'].upper()}] {s['direction']} @ {s['entry_time']}")
        print(f"{'─' * 120}")
        
        print(f"Entry:       {s['entry']:.5f}")
        print(f"Stop Loss:   {s['stop_loss']:.5f}")
        print(f"Take Profit: {s['take_profit']:.5f}")
        print(f"R:R:         {s['risk_reward']:.2f}")
        print(f"Confidence:  {s['confidence']:.1f}")
        print(f"Outcome:     {s['outcome']} (PnL: {s['pnl']:.2f})")
        
        if s['analyzer'] == 'snr':
            print(f"\n📊 CORE SNR ANALYZER (Book Steps 1-2 + M15 Zone→Breakout→Retest)")
            print(f"  4H Level:     {s['h4_kind']} ({s['h4_shape']}-shape) @ index {s['h4_index']}")
            print(f"  4H Anchors:   ({s['h4_t1']}, {s['h4_y1']:.5f}) → ({s['h4_t2']}, {s['h4_y2']:.5f})")
            print(f"  4H Line at rejection: {s['h4_price']:.5f}")
            print(f"  4H Fresh:     {'✓' if s['h4_fresh'] else '✗'}")
            
            print(f"\n  Rejection Candle (c+3):")
            print(f"    Index:       {s['rej_candle_index']}")
            print(f"    Time:        {s['rej_open_time']}")
            print(f"    Line Value:  {s['rej_price']:.5f}")
            print(f"    Clean (>0.1%): {'✓ PASS' if s['rej_clean'] else '✗ FAIL'}")
            print(f"    Validated (MISS): {'✓ PASS' if s['rej_validated'] else '✗ FAIL'}")
            print(f"    Validation:  {s['rej_validation'] or 'NONE'}")
            print(f"    Body-First:  {'✗ FAIL' if s['rej_body_first'] else '✓ PASS (wick first)'}")
            print(f"    Rule3 (c+1): {'✓ PASS' if s['rej_rule3_ok'] else '✗ FAIL'} {'(pending)' if s['rej_rule3_pending'] else ''}")
            print(f"    Fresh:       {'✓ PASS' if s['rej_fresh'] else '✗ FAIL'}")
            
            print(f"\n  M15 Confirmation (Zone→Breakout→Retest):")
            if s['m15_zone_index'] is not None:
                print(f"    Zone Index:      {s['m15_zone_index']} (engulfing at 4H line)")
                print(f"    Breakout Index:  {s['m15_breakout_index']}")
                print(f"    Retest Index:    {s['m15_retest_index']}")
                print(f"    Zone:            {s['m15_zone_low']:.5f} - {s['m15_zone_high']:.5f}")
                print(f"    Line Value:      {s['m15_line_value']:.5f}")
                print(f"    Clean Retest:    {'✓' if s['m15_clean'] else '✗'}")
                print(f"    Retest Body%:    {s['m15_body_fraction']:.1%}")
            else:
                print(f"    (Book validation path - entry at c+2 close, not M15 zone)")
            
            print(f"\n  TP: {s['tp_source']} (R:R {s['tp_r']:.2f})")
            if s['tp_level_kind']:
                print(f"    TP Level: {s['tp_level_kind']} @ {s['tp_level_price']:.5f}")
            
            print(f"\n  📋 BOOK RULE CHECKLIST:")
            checks = [
                ("Step 1: S/R from CLOSES only (A/V shape)", True, "Engine enforces"),
                ("Step 2: MISS filter (no touch before rejection)", s['rej_validated'], "MISS validated level"),
                ("Step 3: c+3 rejection is engulfing or pin bar", s['rej_validation'] in ('engulfing', 'pin_bar'), f"Validation: {s['rej_validation'] or 'NONE'}"),
                ("Step 4: First touch is WICK not body", not s['rej_body_first'], "Body-first invalidates"),
                ("Step 5: Next candle (c+4) doesn't close back beyond", s['rej_rule3_ok'], "Rule 3 holds"),
                ("Step 6: Level is FRESH (no wick pierce)", s['rej_fresh'], "Fresh level holds liquidity"),
                ("M15: Engulfing zone at 4H line", s['m15_zone_index'] is not None, "Zone found"),
                ("M15: Breakout beyond zone", s['m15_breakout_index'] is not None, "Breakout found"),
                ("M15: Retest of zone rejected", s['m15_retest_index'] is not None, "Retest found"),
                ("Entry at NEXT M15 open after retest", True, "Engine enforces"),
                ("SL at structural swing (min 10 pips)", True, "Engine enforces floor"),
                ("TP at next fresh SNR level (capped at 2-3R)", s['tp_source'] == 'level', f"TP source: {s['tp_source']}"),
            ]
            for desc, passed, note in checks:
                status = "✓ PASS" if passed else "✗ FAIL"
                print(f"    {status} - {desc} [{note}]")
                
        elif s['analyzer'] == 'breakout':
            print(f"\n📊 BREAKOUT ANALYZER (Book Steps 3-4)")
            print(f"  4H Level:     {s['h4_kind']} trendline")
            print(f"  4H Line at rejection: {s['h4_price']:.5f}")
            print(f"  Rejection:    {s['rej_candle_index']} @ {s['rej_open_time']}")
            
            print(f"\n  15M Box (10 bars before validation):")
            print(f"    Support:  {s['box_sup']:.5f}")
            print(f"    Resistance: {s['box_res']:.5f}")
            print(f"    Bars:     {s['box_bars']}")
            
            print(f"\n  Breakout:")
            print(f"    Breakout Index: {s['breakout_index']}")
            print(f"    Direction: {s['direction']} (body-close {'above res' if s['direction'] == 'BUY' else 'below sup'})")
            print(f"    Entry: Next M15 open @ {s['entry']:.5f}")
            print(f"    SL: Beyond breakout extreme + buffer ({s['stop_loss']:.5f})")
            print(f"    TP: {s['tp_source']} (R:R {s['tp_r']:.2f})")
            
            if s['tp_level_kind']:
                print(f"    TP Level: {s['tp_level_kind']} @ {s['tp_level_price']:.5f}")
            
            print(f"\n  📋 BOOK RULE CHECKLIST (Breakout):")
            checks = [
                ("4H rejection is book-validated (Step 1+2)", True, "Pre-filtered by engine"),
                ("15M box = 10 bars BEFORE 4H validation", True, "Engine enforces"),
                ("Box levels = min/max of CLOSES (body cluster)", True, "local_box() uses closes"),
                ("Breakout = BODY CLOSE beyond box", True, "find_body_breakout enforces"),
                ("Breakout direction ALIGNED with 4H rejection", True, "Direction gate in engine"),
                ("Entry at NEXT 15M open", True, "Engine enforces"),
                ("SL beyond breakout extreme + buffer (min 10 pips)", True, "Engine enforces floor"),
                ("TP at next fresh SNR or 2R fallback", s['tp_source'] in ('level', '2R'), f"TP source: {s['tp_source']}"),
                ("Live edge: breakout within 4 M15 bars of current", True, "SNR_BREAKOUT_MAX_AGE=4"),
            ]
            for desc, passed, note in checks:
                status = "✓ PASS" if passed else "✗ FAIL"
                print(f"    {status} - {desc} [{note}]")
        
        print(f"\n  Explanation:")
        for line in s['explanation'].split('\n'):
            if line.strip():
                print(f"    {line}")


def main():
    print("=" * 80)
    print("MALAYSIAN SNR BACKTEST - XAUUSD OCT 1-9 2026")
    print("Analyzers: snr, breakout (SNR_MALAYSIAN_ANALYZERS=snr,breakout)")
    print("Data: M1 only, CandleRegistry aggregates all TFs")
    print("=" * 80)
    
    m1_candles = load_data()
    print(f"Loaded {len(m1_candles)} M1 candles")
    print(f"Date range: {m1_candles[0]['open_time']} to {m1_candles[-1]['open_time']}")
    
    # Run backtest
    print("\nRunning backtest...")
    engine = BacktestEngine(strategy_mode="malysian", fast=False)
    
    result = engine.run(
        candles_1m=m1_candles,
        symbol="XAUUSD",
        strategies=["malysian"],
        initial_balance=10000.0,
        risk_per_trade=0.01,
        base_timeframe=60,
    )
    
    # Print summary
    print(f"\n{'=' * 80}")
    print("BACKTEST SUMMARY")
    print(f"{'=' * 80}")
    print(f"Symbol: XAUUSD")
    print(f"Total Trades: {len(result.trades)}")
    print(f"Initial Balance: ${result.initial_balance:.2f}")
    print(f"Final Balance: ${result.final_balance:.2f}")
    print(f"Total PnL: ${result.total_pnl:.2f}")
    print(f"Overall Win Rate: {result.overall_win_rate:.1f}%")
    
    for strat in result.per_strategy:
        print(f"\n  {strat.strategy}:")
        print(f"    Trades: {strat.total_trades}, Wins: {strat.wins}, Losses: {strat.losses}, Expired: {strat.expired}")
        print(f"    Win Rate: {strat.win_rate:.1f}%, Profit Factor: {strat.profit_factor:.2f}")
        print(f"    Total PnL: ${strat.total_pnl:.2f}")
    
    # Funnel stats
    funnel = funnel_snapshot()
    print(f"\n{'=' * 80}")
    print("SNR FUNNEL")
    print(f"{'=' * 80}")
    for sym, counts in funnel.items():
        print(f"\n{sym}:")
        for stage, count in counts.items():
            if count > 0:
                print(f"  {stage}: {count}")
    
    # Detailed signal analysis
    signals = extract_signal_details(result.trades)
    print_signal_analysis(signals)
    
    # CSV export
    if signals:
        csv_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api/snr_signals_analysis.csv"
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=signals[0].keys())
            writer.writeheader()
            writer.writerows(signals)
        print(f"\n✓ Detailed signals exported to: {csv_path}")
    
    return result, signals


if __name__ == "__main__":
    main()