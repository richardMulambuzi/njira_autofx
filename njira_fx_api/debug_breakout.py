#!/usr/bin/env python3
"""
Debug why breakout analyzer produces no signals.
"""
import os
import sys
import logging

os.environ["SNR_MALAYSIAN_ANALYZERS"] = "snr,breakout"

logging.basicConfig(level=logging.WARNING)
logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)

sys.path.insert(0, "/Users/richardmlambuzi/Documents/njira_auto_fx/njira_fx_api")

from forex.backtest.parser import parse_tf_csv
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.cache import HotCache
from forex.analysis.snr_breakout import analyze_breakout


def load_data():
    m1_path = "/Users/richardmlambuzi/Documents/njira_auto_fx/historical-data/XAGUSD_2026/XAUUSD_OCT01-09_2026_M1.csv"
    
    with open(m1_path) as f:
        m1_candles = parse_tf_csv(f, symbol="XAUUSD", timeframe=60)
    
    return m1_candles


def debug_breakout(m1_candles):
    """Debug the breakout analyzer step by step."""
    # Build the same pipeline as backtest
    cache = HotCache()
    cache.set_candles("XAUUSD", 60, m1_candles)
    
    # Create signal engine with minimal dependencies
    from forex.analysis.market_structure import MarketStructureEngine
    from forex.analysis.liquidity import LiquidityEngine
    from forex.analysis.order_blocks import OrderBlockEngine
    from forex.analysis.fvg import FVGEngine
    from forex.analysis.poc import POCEngine
    from forex.analysis.microstructure import MicrostructureEngine
    from forex.analysis.spread import SpreadEngine
    from forex.analysis.volatility import VolatilityEngine
    from forex.analysis.session import SessionEngine
    from forex.analysis.volume_profile import VolumeProfileEngine
    from forex.analysis.context import MarketContextEngine
    from forex.analysis.retest import RetestEngine
    
    structure_engines = {tf: MarketStructureEngine() for tf in [60, 300, 900, 14400, 86400]}
    liquidity_engines = {tf: LiquidityEngine() for tf in [60, 300, 900, 14400, 86400]}
    ob_engines = {tf: OrderBlockEngine() for tf in [60, 300, 900, 14400, 86400]}
    fvg_engines = {tf: FVGEngine() for tf in [60, 300, 900, 14400, 86400]}
    poc_engines = {tf: POCEngine() for tf in [60, 300, 900, 14400, 86400]}
    
    microstructure = MicrostructureEngine()
    spread_engine = SpreadEngine()
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
        spread_engine=spread_engine,
        volume_profile_engine=volume_profile,
        cache=cache,
    )
    
    retest_engine = RetestEngine(cache)
    
    signal_engine = SignalEngine(
        cache=cache,
        structure_engines=structure_engines,
        liquidity_engines=liquidity_engines,
        ob_engines=ob_engines,
        fvg_engines=fvg_engines,
        poc_engines=poc_engines,
        microstructure_engine=microstructure,
        volatility_engine=volatility,
        session_engine=session,
        spread_engine=spread_engine,
        volume_profile_engine=volume_profile,
        context_engine=context_engine,
        retest_engine=retest_engine,
        min_confidence=30,
        min_rr=1.5,
    )
    
    # Now run the CandleRegistry aggregation to populate M15 and H4
    from forex.analysis.candle_engine import CandleRegistry
    registry = CandleRegistry(timeframes=[60, 300, 900, 3600, 14400, 86400])
    for c in m1_candles:
        t0 = int(c["open_time"].timestamp())
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        for price, offset in [(o, 0), (h, 15), (l, 30), (cl, 45)]:
            registry.feed_tick("XAUUSD", price, price, (t0 + offset) * 1000)
    
    # Copy aggregated candles to cache
    agg = registry.get_aggregator("XAUUSD")
    for tf in [900, 14400]:
        cache.set_candles("XAUUSD", tf, agg.get_closed(tf))
    
    print(f"H4 candles in cache: {len(cache.get('XAUUSD', 14400))}")
    print(f"M15 candles in cache: {len(cache.get('XAUUSD', 900))}")
    
    # Now test analyze_breakout
    print("\n=== Testing analyze_breakout ===")
    draft = analyze_breakout(signal_engine, "XAUUSD")
    if draft:
        print(f"Breakout draft: {draft.direction} entry={draft.entry:.5f} conf={draft.confidence:.1f}")
        print(f"Evidence: {list(draft.evidence.keys())}")
    else:
        print("Breakout draft: None")
        # Debug step by step
        from forex.analysis.signal_engine import SNR_REJECTION_OFFSET, SNR_H4_RECENT_BARS, SNR_BOOK_VALIDATION_OFFSET
        
        raw_h4 = cache.get("XAUUSD", 14400)
        raw_m15 = cache.get("XAUUSD", 900)
        
        print(f"\nH4 candles: {len(raw_h4)}")
        print(f"M15 candles: {len(raw_m15)}")
        
        for kind in ("support", "resistance"):
            levels = signal_engine._snr_levels(raw_h4, kind, max_levels=5)
            print(f"\n{kind} levels: {len(levels)}")
            for lvl in levels:
                print(f"  idx={lvl['index']} t1={lvl['t1']} y1={lvl['y1']:.5f} t2={lvl['t2']} y2={lvl['y2']:.5f}")
                
                # Check if tradable
                last = len(raw_h4) - 1
                is_live = (lvl["index"] + SNR_REJECTION_OFFSET <= last and 
                          lvl["index"] + SNR_REJECTION_OFFSET >= last - SNR_H4_RECENT_BARS + 1)
                print(f"    Live (c+3 in recent): {is_live}")
                
                if is_live:
                    # Check rejection
                    direction = "BUY" if kind == "support" else "SELL"
                    rej = signal_engine._snr_check_rejection(raw_h4, lvl, direction)
                    if rej:
                        print(f"    Rejection: idx={rej['candle_index']} validation={rej.get('validation')} fresh={rej.get('fresh')}")
                        # Check book gate
                        dropped = signal_engine._snr_book_gate("XAUUSD", rej)
                        print(f"    Book gate: '{dropped}'")
                        
                        if not dropped:
                            # Check trendline
                            trendline = signal_engine._snr_tradable_trendline(raw_h4, kind)
                            print(f"    Tradable trendline: {trendline is not None}")
                            if trendline:
                                # Check book validation at c+2
                                val = signal_engine._snr_book_validation(raw_h4, lvl, direction)
                                print(f"    Book validation (c+2): {val is not None}")
                                if val:
                                    print(f"      idx={val['candle_index']} clean={val['clean']}")
                    
        # Now check the breakout logic specifically
        print("\n=== Breakout specific checks ===")
        for kind in ("support", "resistance"):
            levels = signal_engine._snr_levels(raw_h4, kind, max_levels=5)
            for lvl in levels:
                last = len(raw_h4) - 1
                is_live = (lvl["index"] + SNR_REJECTION_OFFSET <= last and 
                          lvl["index"] + SNR_REJECTION_OFFSET >= last - SNR_H4_RECENT_BARS + 1)
                if not is_live:
                    continue
                    
                direction = "BUY" if kind == "support" else "SELL"
                trendline = signal_engine._snr_tradable_trendline(raw_h4, kind)
                if not trendline:
                    continue
                    
                rej = signal_engine._snr_check_rejection(raw_h4, lvl, direction)
                if not rej:
                    continue
                    
                dropped = signal_engine._snr_book_gate("XAUUSD", rej)
                if dropped:
                    print(f"  {kind} {direction}: dropped by book gate: {dropped}")
                    continue
                    
                print(f"  {kind} {direction}: PASSED book gate, checking breakout...")
                # This is what analyze_breakout does
                val = signal_engine._snr_book_validation(raw_h4, lvl, direction)
                if not val:
                    print(f"    No book validation at c+2")
                    continue
                    
                print(f"    Book validation at c+2: idx={val['candle_index']}")
                # Now check 15M box
                m15 = signal_engine._module_candles(raw_m15)
                rej_time = signal_engine._epoch(val.get("open_time"))
                val_index = next((i for i, c in enumerate(m15) if signal_engine._epoch(c.get("open_time")) >= rej_time), None)
                
                if val_index is None:
                    print(f"    No M15 candle at/after validation time")
                    continue
                print(f"    Validation M15 index: {val_index}")
                
                if val_index < 10:
                    print(f"    Not enough M15 bars before validation ({val_index} < 10)")
                    continue
                    
                from forex.analysis.snr_breakout import local_box, find_body_breakout, BREAKOUT_BOX_BARS
                sup, res = local_box([float(c["close"]) for c in m15[val_index - BREAKOUT_BOX_BARS:val_index]])
                print(f"    Box: sup={sup:.5f} res={res:.5f}")
                
                bo = find_body_breakout(m15, val_index, sup, res, direction)
                if bo is None:
                    print(f"    No body breakout found")
                    continue
                print(f"    Breakout at M15 index: {bo}")
                
                if bo + 1 >= len(m15):
                    print(f"    No next candle for entry")
                    continue
                    
                # Check live edge
                from forex.analysis.signal_engine import SNR_BREAKOUT_MAX_AGE
                if bo + 1 < len(m15) - SNR_BREAKOUT_MAX_AGE:
                    print(f"    Breakout too old (live edge check failed)")
                    continue
                    
                print(f"    *** BREAKOUT SIGNAL WOULD FIRE ***")
                print(f"      Entry: {float(m15[bo+1]['open']):.5f}")
                print(f"      Direction: {direction}")


def main():
    m1_candles = load_data()
    debug_breakout(m1_candles)


if __name__ == "__main__":
    main()