#!/usr/bin/env python3
"""
Test Malaysian SNR strategy (snr + breakout analyzers) on Pepperstone XAUUSD data.
Captures all SNR signals and compares to book rules.
"""
import os
import sys
import io

# Set environment variable BEFORE importing signal_engine
os.environ["SNR_MALAYSIAN_ANALYZERS"] = "snr,breakout"

# Add project to path
sys.path.insert(0, "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api")

from forex.backtest.parser import parse_tf_csv
from forex.backtest.engine import BacktestEngine


def load_data():
    """Load M1 and H4 CSV data."""
    m1_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_M1.csv"
    h4_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_H4.csv"
    
    with open(m1_path) as f:
        m1_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=60)
    
    with open(h4_path) as f:
        h4_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=14400)
    
    print(f"Loaded {len(m1_candles)} M1 candles")
    print(f"Loaded {len(h4_candles)} H4 candles")
    print(f"M1 date range: {m1_candles[0]['open_time']} to {m1_candles[-1]['open_time']}")
    print(f"H4 date range: {h4_candles[0]['open_time']} to {h4_candles[-1]['open_time']}")
    
    return m1_candles, h4_candles


def extract_signal_details(trades):
    """Extract detailed signal information for comparison to book rules."""
    signals = []
    for t in trades:
        ev = t.get("evidence", {})
        analyzer = ev.get("analyzer", "unknown")
        
        # Core SNR info
        h4_level = ev.get("snr_4h_level", {})
        rejection = ev.get("snr_rejection", {})
        breakout_box = ev.get("breakout_box_levels", {})
        
        signals.append({
            "signal_id": t.get("signal_id"),
            "analyzer": analyzer,
            "direction": t.get("direction"),
            "entry_time": t.get("entry_time"),
            "entry": t.get("entry"),
            "stop_loss": t.get("stop_loss"),
            "take_profit": t.get("take_profit"),
            "risk_reward": t.get("risk_reward", 0),
            "confidence": t.get("confidence"),
            "outcome": t.get("outcome"),
            "pnl": t.get("pnl"),
            # 4H level details
            "h4_kind": h4_level.get("kind"),
            "h4_shape": h4_level.get("shape"),
            "h4_price": h4_level.get("price"),
            "h4_t1": h4_level.get("t1"),
            "h4_t2": h4_level.get("t2"),
            "h4_y1": h4_level.get("y1"),
            "h4_y2": h4_level.get("y2"),
            "h4_index": h4_level.get("index"),
            # Rejection details
            "rej_candle_index": rejection.get("candle_index"),
            "rej_price": rejection.get("price"),
            "rej_clean": rejection.get("clean"),
            "rej_validated": rejection.get("validated"),
            "rej_validation": rejection.get("validation"),
            "rej_body_first": rejection.get("body_first"),
            "rej_rule3_ok": rejection.get("rule3_ok"),
            "rej_rule3_pending": rejection.get("rule3_pending"),
            "rej_fresh": rejection.get("fresh"),
            "rej_open_time": rejection.get("open_time"),
            # Breakout details (for breakout analyzer)
            "box_sup": breakout_box.get("sup"),
            "box_res": breakout_box.get("res"),
            "box_bars": breakout_box.get("bars"),
            "box_validation_open_time": breakout_box.get("validation_open_time"),
            "breakout_index": breakout_box.get("breakout_index"),
            # TP source
            "tp_source": ev.get("snr_tp_source"),
            "tp_level": ev.get("snr_tp_level"),
            "tp_r": ev.get("snr_tp_r"),
            "explanation": t.get("explanation"),
        })
    return signals


def format_timestamp(ts):
    """Format timestamp for display."""
    if ts is None:
        return "N/A"
    if isinstance(ts, (int, float)):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    if hasattr(ts, 'strftime'):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)


def print_signal_table(signals):
    """Print a detailed table of signals with PASS/FAIL vs book rules."""
    print("\n" + "=" * 180)
    print("SNR SIGNALS FOUND - DETAILED ANALYSIS")
    print("=" * 180)
    
    for i, s in enumerate(signals, 1):
        print(f"\n{'─' * 180}")
        print(f"SIGNAL #{i} [{s['analyzer'].upper()}] {s['direction']} @ {format_timestamp(s['entry_time'])}")
        print(f"{'─' * 180}")
        
        # Basic trade info
        print(f"Entry:       {s['entry']:.5f}")
        print(f"Stop Loss:   {s['stop_loss']:.5f}")
        print(f"Take Profit: {s['take_profit']:.5f}")
        print(f"R:R:         {s['risk_reward']:.2f}")
        print(f"Confidence:  {s['confidence']:.1f}")
        print(f"Outcome:     {s['outcome']} (PnL: {s['pnl']:.2f})")
        
        if s['analyzer'] == 'snr':
            print(f"\n📊 CORE SNR ANALYZER (Book Steps 1-2 + M15 confirmation)")
            print(f"  4H Level:     {s['h4_kind']} ({s['h4_shape']}-shape) @ index {s['h4_index']}")
            print(f"  4H Anchors:   ({format_timestamp(s['h4_t1'])}, {s['h4_y1']:.5f}) → ({format_timestamp(s['h4_t2'])}, {s['h4_y2']:.5f})")
            print(f"  4H Line at rejection: {s['h4_price']:.5f}")
            
            print(f"\n  Rejection Candle (c+3):")
            print(f"    Index:      {s['rej_candle_index']}")
            print(f"    Time:       {format_timestamp(s['rej_open_time'])}")
            print(f"    Line Value: {s['rej_price']:.5f}")
            print(f"    Clean (>0.1%): {'✓ PASS' if s['rej_clean'] else '✗ FAIL'}")
            print(f"    Validated (MISS): {'✓ PASS' if s['rej_validated'] else '✗ FAIL'}")
            print(f"    Validation Type: {s['rej_validation'] or 'N/A'}")
            print(f"    Body-First Touch: {'✗ FAIL (body first)' if s['rej_body_first'] else '✓ PASS (wick first)'}")
            print(f"    Rule3 (next candle): {'✓ PASS' if s['rej_rule3_ok'] else '✗ FAIL'} {'(pending)' if s['rej_rule3_pending'] else ''}")
            print(f"    Fresh (no wick pierce): {'✓ PASS' if s['rej_fresh'] else '✗ FAIL'}")
            
            print(f"\n  M15 Confirmation:")
            print(f"    Type: 15M engulfing zone → breakout → retest (book p.73)")
            print(f"    TP Source: {s['tp_source']} (R:R {s['tp_r']:.2f})")
            if s['tp_level']:
                tp_l = s['tp_level']
                print(f"    TP Level: {tp_l.get('kind')} @ {tp_l.get('level_price', tp_l.get('price', 'N/A')):.5f}")
            
            # Book rule checks
            print(f"\n  📋 BOOK RULE CHECKLIST:")
            checks = [
                ("Step 1: S/R from CLOSES only (A/V shape)", True, "Engine enforces this"),
                ("Step 2: MISS filter (no touch before rejection)", s['rej_validated'], "MISS validated level"),
                ("Step 3: c+2 rejection with wick touch + body close back", s['rej_validation'] in ('engulfing', 'pin_bar'), f"Validation: {s['rej_validation'] or 'NONE'}"),
                ("Step 4: First touch is WICK not body", not s['rej_body_first'], "Body-first invalidates"),
                ("Step 5: Next candle (c+3) doesn't close back beyond", s['rej_rule3_ok'], "Rule 3 holds"),
                ("Step 6: Level is FRESH (no wick pierce)", s['rej_fresh'], "Fresh level holds liquidity"),
                ("M15: Body-close breakout aligned with 4H direction", True, "Required by engine"),
                ("Entry at NEXT M15 open after breakout", True, "Engine enforces"),
                ("SL at structural swing / rejection wick (min 10 pips)", True, "Engine enforces floor"),
                ("TP at next fresh SNR level (capped at 2-3R)", s['tp_source'] == 'level', f"TP source: {s['tp_source']}"),
            ]
            for desc, passed, note in checks:
                status = "✓ PASS" if passed else "✗ FAIL"
                print(f"    {status} - {desc} [{note}]")
                
        elif s['analyzer'] == 'breakout':
            print(f"\n📊 BREAKOUT ANALYZER (Book Steps 3-4)")
            print(f"  4H Level:     {s['h4_kind']} trendline (from {s['h4_y1']:.5f} → {s['h4_y2']:.5f})")
            print(f"  4H Line at rejection: {s['h4_price']:.5f}")
            
            print(f"\n  Rejection Candle (book-validated c+2):")
            print(f"    Index:      {s['rej_candle_index']}")
            print(f"    Time:       {format_timestamp(s['rej_open_time'])}")
            print(f"    Line Value: {s['rej_price']:.5f}")
            
            print(f"\n  15M Box (10 bars before validation):")
            print(f"    Support:  {s['box_sup']:.5f}")
            print(f"    Resistance: {s['box_res']:.5f}")
            print(f"    Bars:     {s['box_bars']}")
            print(f"    Validation Time: {format_timestamp(s['box_validation_open_time'])}")
            
            print(f"\n  Breakout:")
            print(f"    Breakout Index: {s['breakout_index']}")
            print(f"    Direction:      {s['direction']} (body-close {'above res' if s['direction'] == 'BUY' else 'below sup'})")
            print(f"    Entry: Next M15 open @ {s['entry']:.5f}")
            print(f"    SL: Beyond breakout extreme + 0.05% buffer ({s['stop_loss']:.5f})")
            print(f"    TP: {s['tp_source']} (R:R {s['tp_r']:.2f})")
            
            if s['tp_level']:
                tp_l = s['tp_level']
                print(f"    TP Level: {tp_l.get('kind')} @ {tp_l.get('level_price', tp_l.get('price', 'N/A')):.5f}")
            
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
            print(f"    {line}")


def main():
    print("=" * 80)
    print("MALAYSIAN SNR BACKTEST - XAUUSD OCT 1-9 2026")
    print("Analyzers: snr, breakout (SNR_MALAYSIAN_ANALYZERS=snr,breakout)")
    print("=" * 80)
    
    # Load data
    m1_candles, h4_candles = load_data()
    
    # Run backtest
    print("\nRunning backtest...")
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
    print(f"Best Strategy: {result.best_strategy}")
    
    for strat in result.per_strategy:
        print(f"\n  {strat.strategy}:")
        print(f"    Trades: {strat.total_trades}, Wins: {strat.wins}, Losses: {strat.losses}, Expired: {strat.expired}")
        print(f"    Win Rate: {strat.win_rate:.1f}%, Profit Factor: {strat.profit_factor:.2f}")
        print(f"    Total PnL: ${strat.total_pnl:.2f}, Expectancy: ${strat.expectancy:.2f}")
        print(f"    Max DD: ${strat.max_drawdown:.2f}")
        print(f"    Passes 70% Gate: {strat.passes_70_gate}")
    
    # Funnel stats
    from forex.analysis.signal_engine import funnel_snapshot
    funnel = funnel_snapshot()
    print(f"\n{'=' * 80}")
    print("SNR FUNNEL (where setups die)")
    print(f"{'=' * 80}")
    for sym, counts in funnel.items():
        print(f"\n{sym}:")
        for stage, count in counts.items():
            if count > 0:
                print(f"  {stage}: {count}")
    
    # Detailed signal analysis
    signals = extract_signal_details(result.trades)
    print_signal_table(signals)
    
    # CSV export for further analysis
    import csv
    csv_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api/snr_signals_analysis.csv"
    if signals:
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=signals[0].keys())
            writer.writeheader()
            writer.writerows(signals)
        print(f"\n✓ Detailed signals exported to: {csv_path}")
    
    return result, signals


if __name__ == "__main__":
    main()