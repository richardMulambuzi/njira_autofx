#!/usr/bin/env python3
"""
Debug SNR backtest - add verbose logging to understand why no signals.
"""
import os
import sys
import logging

# Set environment variable BEFORE importing signal_engine
os.environ["SNR_MALAYSIAN_ANALYZERS"] = "snr,breakout"

# Enable debug logging
logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s - %(message)s')
# Reduce noise from other loggers
logging.getLogger("forex.backtest.engine").setLevel(logging.INFO)
logging.getLogger("forex.analysis.cache").setLevel(logging.WARNING)

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


def debug_snr_analysis(m1_candles, h4_candles):
    """Run the SNR analysis manually to see what's happening."""
    from forex.analysis.signal_engine import SignalEngine
    from forex.analysis.cache import HotCache
    from forex.analysis.candle_engine import CandleRegistry
    
    # Build the same pipeline as BacktestEngine
    cache = HotCache()
    cache.set_candles("XAUUSD", 60, m1_candles)
    cache.set_candles("XAUUSD", 14400, h4_candles)
    
    # Create a minimal signal engine
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
    from forex.analysis.signal_engine import SignalEngine, enabled_malaysian_analyzers
    
    structure_engines = {60: MarketStructureEngine(), 300: MarketStructureEngine(), 900: MarketStructureEngine(), 14400: MarketStructureEngine(), 86400: MarketStructureEngine()}
    liquidity_engines = {60: LiquidityEngine(), 300: LiquidityEngine(), 900: LiquidityEngine(), 14400: LiquidityEngine(), 86400: LiquidityEngine()}
    ob_engines = {60: OrderBlockEngine(), 300: OrderBlockEngine(), 900: OrderBlockEngine(), 14400: OrderBlockEngine(), 86400: OrderBlockEngine()}
    fvg_engines = {60: FVGEngine(), 300: FVGEngine(), 900: FVGEngine(), 14400: FVGEngine(), 86400: FVGEngine()}
    poc_engines = {60: POCEngine(), 300: POCEngine(), 900: POCEngine(), 14400: POCEngine(), 86400: POCEngine()}
    
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
    
    print(f"Enabled analyzers: {enabled_malaysian_analyzers()}")
    
    # Manually run the SNR evaluation
    print("\n--- Running _evaluate_snr ---")
    signals = signal_engine._evaluate_snr("XAUUSD")
    print(f"Signals returned: {len(signals)}")
    for s in signals:
        print(f"  {s.strategy} {s.direction} entry={s.entry:.5f} sl={s.stop_loss:.5f} tp={s.take_profit:.5f} conf={s.confidence_score:.1f}")
        print(f"    evidence keys: {list(s.confidence_evidence.keys())}")
    
    # Also check the candidates directly
    print("\n--- Running _malaysian_candidates ---")
    candidates = signal_engine._malaysian_candidates("XAUUSD")
    print(f"Candidates: {len(candidates)}")
    for c in candidates:
        print(f"  {c.evidence.get('analyzer')} {c.direction} entry={c.entry:.5f} conf={c.confidence:.1f}")
    
    # Check the SNR draft specifically
    print("\n--- Running _build_snr_draft ---")
    snr_draft = signal_engine._build_snr_draft("XAUUSD")
    if snr_draft:
        print(f"  SNR draft: {snr_draft.direction} entry={snr_draft.entry:.5f} conf={snr_draft.confidence:.1f}")
    else:
        print("  SNR draft: None")
    
    # Check the SNR book draft
    print("\n--- Running _analyze_snr_book ---")
    book_draft = signal_engine._analyze_snr_book("XAUUSD")
    if book_draft:
        print(f"  Book draft: {book_draft.direction} entry={book_draft.entry:.5f} conf={book_draft.confidence:.1f}")
    else:
        print("  Book draft: None")
    
    # Check breakout
    print("\n--- Running analyze_breakout ---")
    from forex.analysis.snr_breakout import analyze_breakout
    breakout_draft = analyze_breakout(signal_engine, "XAUUSD")
    if breakout_draft:
        print(f"  Breakout draft: {breakout_draft.direction} entry={breakout_draft.entry:.5f} conf={breakout_draft.confidence:.1f}")
    else:
        print("  Breakout draft: None")


def main():
    m1_candles, h4_candles = load_data()
    debug_snr_analysis(m1_candles, h4_candles)


if __name__ == "__main__":
    main()