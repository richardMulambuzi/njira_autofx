'use client';

import React, { useState, useEffect } from 'react';
import { Signal, List, ChevronRight, Zap } from 'lucide-react';
import { useDashboardData } from '@/hooks/useDashboardData';
import { useReverb } from '@/hooks/useReverb';
import { motion, AnimatePresence } from 'framer-motion';
import type { AnalysisSnapshot, StrategyPerformance, TradingSignal } from '@/types/market';
import VolumeProfilePanel from './VolumeProfilePanel';

export default function RightEnginePanel() {
  const [activeTab, setActiveTab] = useState('signals');
  const { signals, performance } = useDashboardData();
  const { client, isConnected } = useReverb();
  const [liveSignals, setLiveSignals] = useState<TradingSignal[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisSnapshot | null>(null);

  useEffect(() => {
    if (signals.length > 0) setLiveSignals(signals);
  }, [signals]);

  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('trading-signals');
    channel.bind('CONFIRMED_SIGNAL', (event: unknown) => {
      const payload = event as { signal?: TradingSignal } | null;
      if (payload?.signal) {
        setLiveSignals((prev) => [payload.signal as TradingSignal, ...prev].slice(0, 15));
      }
    });
    return () => { channel.unbind_all(); client.unsubscribe('trading-signals'); };
  }, [client]);

  // §45 — realtime observability: "analysis" event on the shared "njira-market" channel.
  // Bind/unbind only this event (do NOT unsubscribe the shared channel — other
  // components depend on it).
  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onAnalysis = (event: unknown) => setAnalysis(event as AnalysisSnapshot);
    channel.bind('analysis', onAnalysis);
    return () => { channel.unbind('analysis', onAnalysis); };
  }, [client]);

  return (
    <div className="h-full flex flex-col overflow-hidden">
        {/* Panel Header */}
        <div className="h-12 border-b border-slate-800/60 flex items-center px-4 justify-between bg-slate-900/40 backdrop-blur-sm">
            <div className="flex gap-6 h-full">
                <PanelTab label="Signals" active={activeTab === 'signals'} onClick={() => setActiveTab('signals')} />
                <PanelTab label="Brain Output" active={activeTab === 'brain'} onClick={() => setActiveTab('brain')} />
                <PanelTab label="Volume Profile" active={activeTab === 'volume'} onClick={() => setActiveTab('volume')} />
            </div>
            <button className="p-1.5 rounded-none hover:bg-slate-800 text-slate-600 hover:text-slate-400 transition-colors">
                <List size={14} />
            </button>
        </div>

        {/* Content Area */}
        <div className="flex-1 overflow-y-auto p-4 custom-scrollbar bg-[#0a0e17]/30">
            {activeTab === 'signals' ? (
                <div className="space-y-2">
                    <AnimatePresence initial={false}>
                        {liveSignals.length > 0 ? (
                            liveSignals.map((signal, idx) => (
                                <SignalMiniCard key={signal.id ?? idx} signal={signal} />
                            ))
                        ) : (
                            <div className="flex flex-col items-center justify-center py-20 text-slate-600 opacity-20">
                                <Signal size={32} />
                                <span className="text-[9px] font-bold uppercase mt-2 tracking-widest">Listening...</span>
                            </div>
                        )}
                    </AnimatePresence>
                </div>
             ) : activeTab === 'brain' ? (
                <div className="space-y-6">
                   <div className="space-y-4">
                        <h4 className="text-[9px] font-black text-slate-600 uppercase tracking-[0.2em] mb-4 flex items-center gap-2">
                            <Zap size={10} /> Strategy Performance
                        </h4>
                        {performance.length > 0 ? performance.map((p: StrategyPerformance, i: number) => {
                            const decided = p.wins + p.losses;
                            const winRate = decided > 0 ? Math.round((p.wins / decided) * 100) : 0;
                            return (
                             <div key={`${p.strategy}-${i}`} className="flex flex-col gap-1 group cursor-default">
                                 <div className="flex justify-between text-[8px] font-black uppercase tracking-[0.15em] text-slate-600 group-hover:text-slate-400 transition-colors">
                                     <span>{p.strategy}</span>
                                     <span>{p.wins}W / {p.losses}L</span>
                                 </div>
                                 <div className="flex items-center gap-2">
                                     <div className="flex-1 h-1 bg-slate-800/40 rounded-none overflow-hidden">
                                         <div className="h-full bg-blue-500" style={{ width: `${winRate}%` }} />
                                     </div>
                                     <span className="text-[10px] text-blue-400 font-mono font-bold">{winRate}%</span>
                                 </div>
                                 <div className="h-[1px] bg-slate-800/40 w-full mt-1 group-hover:bg-slate-700/60 transition-colors" />
                             </div>
                            );
                        }) : (
                            <div className="text-[10px] text-slate-600 py-4">No strategy data yet.</div>
                        )}
                    </div>

                    {/* §45 — realtime observability snapshot */}
                    <AnimatePresence initial={false}>
                    {analysis ? (
                        <motion.div key="analysis" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} className="space-y-3">
                            <h4 className="text-[9px] font-black text-slate-600 uppercase tracking-[0.2em] flex items-center gap-2"><Zap size={10} /> Live Analysis</h4>
                            <div className="grid grid-cols-3 gap-2">
                                <div className="border border-slate-800/60 bg-slate-900/20 p-2"><div className="text-[8px] font-bold uppercase tracking-widest text-slate-600">Price</div><div className="text-[11px] font-mono font-bold text-blue-400">{analysis.current_price != null ? analysis.current_price.toFixed(5) : '—'}</div></div>
                                <div className="border border-slate-800/60 bg-slate-900/20 p-2"><div className="text-[8px] font-bold uppercase tracking-widest text-slate-600">Day High</div><div className="text-[11px] font-mono font-bold text-slate-300">{analysis.day_high != null ? analysis.day_high.toFixed(5) : '—'}</div></div>
                                <div className="border border-slate-800/60 bg-slate-900/20 p-2"><div className="text-[8px] font-bold uppercase tracking-widest text-slate-600">Day Low</div><div className="text-[11px] font-mono font-bold text-slate-300">{analysis.day_low != null ? analysis.day_low.toFixed(5) : '—'}</div></div>
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                                <div className="border border-slate-800/60 bg-slate-900/20 px-2 py-1.5"><div className="text-[8px] font-bold uppercase tracking-widest text-slate-600">Freshness</div><div className={`text-[10px] font-mono font-bold ${analysis.data_freshness == null ? 'text-slate-500' : analysis.data_freshness < 300 ? 'text-emerald-400' : analysis.data_freshness < 600 ? 'text-amber-400' : 'text-rose-400'}`}>{analysis.data_freshness == null ? '—' : `${analysis.data_freshness}s ago`}</div></div>
                                <div className="border border-slate-800/60 bg-slate-900/20 px-2 py-1.5"><div className="text-[8px] font-bold uppercase tracking-widest text-slate-600">Agreement</div><div className="text-[10px] font-mono font-bold"><span className="text-emerald-400">{analysis.strategy_agreement.bullish}▲</span> / <span className="text-rose-400">{analysis.strategy_agreement.bearish}▼</span></div></div>
                            </div>
                            <div className="space-y-2">
                                {analysis.strategy_reports.map((r, i) => {
                                    const dir = (r.direction || 'NEUTRAL').toUpperCase();
                                    const dc = dir === 'BULLISH' ? 'text-emerald-400' : dir === 'BEARISH' ? 'text-rose-400' : 'text-slate-400';
                                    const dim = !r.active || !r.applicable;
                                    return (
                                        <div key={i} className={`border border-slate-800/60 bg-slate-900/20 p-2 ${dim ? 'opacity-50' : ''}`}>
                                            <div className="flex items-center justify-between"><span className="text-[10px] font-black text-slate-200">{r.name}</span><span className={`text-[8px] font-black uppercase tracking-widest ${dc}`}>{dir}</span></div>
                                            <div className="flex items-center justify-between mt-1"><span className="text-[9px] font-mono text-blue-400 font-bold">{typeof r.confidence === 'number' ? `${r.confidence}%` : '—'}</span><span className="text-[8px] font-bold uppercase tracking-widest text-slate-600">{r.confirmations.length}✓ / {r.missing_confirmations.length}✗</span></div>
                                            {r.invalidations.length > 0 && <div className="text-[8px] font-mono text-rose-400 truncate mt-1">⚠ {r.invalidations.slice(0, 2).join(' · ')}</div>}
                                            {dim && <div className="text-[8px] uppercase tracking-widest text-slate-600 mt-1">inactive</div>}
                                        </div>
                                    );
                                })}
                            </div>
                            {analysis.invalidation.length > 0 && <div className="text-[9px] font-mono text-rose-400 truncate border border-rose-500/30 bg-rose-500/5 p-2">{analysis.invalidation[0]}</div>}
                        </motion.div>
                    ) : (
                        <div className="flex flex-col items-center justify-center py-20 text-slate-600 opacity-20"><Zap size={32} /><span className="text-[9px] font-bold uppercase mt-2 tracking-widest">No analysis data yet.</span></div>
                    )}
                    </AnimatePresence>
                 </div>
              ) : (
                <VolumeProfilePanel />
             )}
        </div>

        {/* Terminal Footer Status */}
        <div className="p-4 border-t border-slate-800/60 bg-slate-900/40">
            <div className="flex justify-between items-center">
                <div className="flex flex-col">
                    <span className="text-[8px] font-bold text-slate-600 uppercase tracking-widest mb-1">Signals</span>
                    <span className="text-[10px] text-blue-400 font-mono font-black">{liveSignals.length}</span>
                </div>
                <div className="flex flex-col items-end">
                    <span className="text-[8px] font-bold text-slate-600 uppercase tracking-widest mb-1">Status</span>
                    <div className="flex items-center gap-2">
                        <span className={`text-[10px] font-bold uppercase tracking-tighter ${isConnected ? 'text-emerald-400' : 'text-rose-400'}`}>
                            {isConnected ? 'LIVE' : 'OFFLINE'}
                        </span>
                        <div className={`w-1.5 h-1.5 rounded-none ${isConnected ? 'bg-emerald-500 animate-pulse' : 'bg-rose-500'}`}></div>
                    </div>
                </div>
            </div>
        </div>
    </div>
  );
}

function PanelTab({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
    return (
        <button
            onClick={onClick}
            className={`text-[9px] font-black uppercase tracking-[0.2em] transition-all relative h-full flex items-center px-2 ${
                active ? 'text-white' : 'text-slate-500 hover:text-slate-300'
            }`}
        >
            {label}
            {active && (
                <motion.div
                    layoutId="activeTab"
                    className="absolute bottom-0 left-0 right-0 h-0.5 bg-blue-500 shadow-[0_-4px_10px_rgba(59,130,246,0.5)] rounded-none"
                />
            )}
        </button>
    )
}

function SignalMiniCard({ signal }: { signal: TradingSignal }) {
    const isBuy = signal.direction === 'BUY' || signal.direction === 'STRONG_BUY';
    const confidence = typeof signal.confidence === 'number' ? signal.confidence : null;
    return (
        <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            className="p-3 rounded-none border border-slate-800/60 bg-slate-900/20 hover:border-blue-500/30 transition-all cursor-pointer group"
        >
            <div className="flex justify-between items-start mb-2">
                <div className="flex items-center gap-2">
                    <div className={`w-1 h-3 rounded-none ${isBuy ? 'bg-emerald-500' : 'bg-rose-500'}`} />
                    <span className="text-[11px] font-black text-slate-200">{signal.symbol ?? '—'}</span>
                </div>
                <span className="text-[9px] text-slate-600 font-mono">{new Date(signal.created_at || Date.now()).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span>
            </div>
            <div className="flex items-center justify-between">
                <span className={`text-[9px] font-black tracking-widest uppercase ${isBuy ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {signal.direction ?? '—'}
                </span>
                <div className="flex items-center gap-2">
                    <span className="text-[9px] text-blue-400 font-bold">{confidence !== null ? `${confidence}%` : '—'}</span>
                    <ChevronRight size={10} className="text-slate-700 group-hover:text-blue-500 group-hover:translate-x-1 transition-all" />
                </div>
            </div>
        </motion.div>
    )
}
