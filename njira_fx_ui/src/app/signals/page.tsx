'use client';

import React, { useState, useEffect } from 'react';
import TerminalLayout from '@/components/layout/TerminalLayout';
import { 
  Signal, Cpu, TrendingUp, TrendingDown, 
  Target, ShieldAlert, BookOpen, Clock, 
  Filter, ArrowUpRight, ArrowDownRight, Info
} from 'lucide-react';
import { useDashboardData } from '@/hooks/useDashboardData';
import { motion, AnimatePresence } from 'framer-motion';

export default function SignalsPage() {
  const { signals, isLoading } = useDashboardData();
  const [filter, setFilter] = useState('ALL');

  const filteredSignals = filter === 'ALL'
    ? signals
    : signals.filter((s: any) => {
        if (filter === 'BUY') return s.direction === 'BUY' || s.direction === 'STRONG_BUY';
        if (filter === 'SELL') return s.direction === 'SELL' || s.direction === 'STRONG_SELL';
        return s.direction === filter;
      });

  return (
    <TerminalLayout>
      <div className="flex flex-col gap-6">
        
        {/* Header & Filters */}
        <div className="flex justify-between items-end">
            <div className="flex flex-col gap-1">
                <h2 className="text-xl font-black text-white tracking-tighter flex items-center gap-3">
                    <span className="w-2 h-8 bg-emerald-600 rounded-none"></span>
                    SIGNAL GENERATION ENGINE
                </h2>
                <p className="text-[10px] text-slate-500 font-bold uppercase tracking-[0.3em]">Institutional Execution-Ready Blueprints</p>
            </div>
            <div className="flex items-center gap-3">
                <div className="flex bg-slate-900/50 rounded-none p-1 border border-slate-800/60">
                    <FilterButton active={filter === 'ALL'} label="ALL" onClick={() => setFilter('ALL')} />
                    <FilterButton active={filter === 'BUY'} label="BUY" onClick={() => setFilter('BUY')} color="text-emerald-400" />
                    <FilterButton active={filter === 'SELL'} label="SELL" onClick={() => setFilter('SELL')} color="text-rose-400" />
                </div>
            </div>
        </div>

        {/* Signal Blueprint Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3 gap-6">
          <AnimatePresence>
            {filteredSignals.length > 0 ? (
                filteredSignals.map((signal: any, idx: number) => (
                    <SignalBlueprintCard key={signal.id || idx} signal={signal} />
                ))
            ) : (
                <div className="col-span-full py-20 flex flex-col items-center justify-center text-slate-700 opacity-20">
                    <Signal size={64} />
                    <span className="text-sm font-black uppercase tracking-[0.5em] mt-4">Awaiting Institutional Confluence...</span>
                </div>
            )}
          </AnimatePresence>
        </div>

      </div>
    </TerminalLayout>
  );
}

function SignalBlueprintCard({ signal }: { signal: any }) {
  const isBuy = signal.direction === 'BUY' || signal.direction === 'STRONG_BUY';
  
  return (
    <motion.div 
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.95 }}
      className="rounded-none border border-slate-800/60 bg-[#0d131f]/60 backdrop-blur-md overflow-hidden flex flex-col shadow-2xl group hover:border-blue-500/30 transition-all duration-500"
    >
      {/* Card Header: Identity & Score */}
      <div className="p-5 border-b border-slate-800/60 flex justify-between items-start bg-slate-900/40">
        <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
                <span className={`text-[10px] font-black px-2 py-0.5 rounded-none tracking-[0.1em] ${isBuy ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/20 text-rose-400 border border-rose-500/20'}`}>
                    {signal.direction}
                </span>
                <h3 className="text-lg font-black text-white tracking-tighter">{signal.symbol}</h3>
            </div>
            <div className="flex items-center gap-2 text-[9px] text-slate-500 font-bold uppercase tracking-widest">
                <Clock size={10} />
                <span>{new Date(signal.timestamp || signal.created_at).toLocaleTimeString()} UTC</span>
            </div>
        </div>
        <div className="flex flex-col items-end">
            <span className="text-[9px] text-slate-600 uppercase font-black mb-1 tracking-widest">Confluence Score</span>
            <div className="text-2xl font-mono font-black text-blue-400 italic">
                {signal.final_score ?? signal.confidence ?? '—'}
            </div>
        </div>
      </div>

      {/* Risk Blueprint Section */}
      <div className="p-5 grid grid-cols-3 gap-4 border-b border-slate-800/40 bg-[#05080f]/30">
         <RiskValue label="ENTRY" value={signal.entry} color="text-slate-200" />
         <RiskValue label="STOP LOSS" value={signal.stop_loss} color="text-rose-500" />
         <RiskValue label="TAKE PROFIT" value={signal.take_profit} color="text-emerald-500" />
      </div>

      {/* Intelligence Layer: Strategy Breakdown */}
      <div className="p-5 grid grid-cols-2 gap-6 border-b border-slate-800/40">
          <div className="flex flex-col gap-3">
              <span className="text-[9px] text-slate-600 uppercase font-black tracking-widest flex items-center gap-2">
                  <Cpu size={10} /> Strategy Weights
              </span>
              <div className="space-y-2">
                  {Object.entries(signal.strategy_breakdown || {}).map(([name, weight]: any) => (
                      <div key={name} className="flex justify-between items-center group/row">
                          <span className="text-[9px] font-bold text-slate-500 uppercase tracking-tight group-hover/row:text-slate-300 transition-colors">{name}</span>
                          <span className="text-[10px] text-blue-400 font-mono font-bold italic">{weight}%</span>
                      </div>
                  ))}
              </div>
          </div>
          <div className="flex flex-col gap-3">
              <span className="text-[9px] text-slate-600 uppercase font-black tracking-widest flex items-center gap-2">
                  <TrendingUp size={10} /> Sentiment Align
              </span>
              <div className="p-3 rounded-none bg-slate-900/60 border border-slate-800/60 flex flex-col items-center justify-center gap-1">
                  <span className={`text-xs font-black italic ${(signal.sentiment_alignment?.bias ?? '') === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {signal.sentiment_alignment?.bias ?? 'N/A'}
                  </span>
                  <div className="w-full h-1 bg-slate-800 rounded-none overflow-hidden mt-1">
                      <div
                        className={`h-full ${(signal.sentiment_alignment?.score ?? 0) > 0.5 ? 'bg-emerald-500' : 'bg-rose-500'}`}
                        style={{ width: `${(signal.sentiment_alignment?.score ?? 0) * 100}%` }}
                      />
                  </div>
                  <span className="text-[9px] text-slate-500 font-bold mt-1">{(signal.sentiment_alignment?.score ?? 0).toFixed(2)} Bias Score</span>
              </div>
          </div>
      </div>

      {/* Explainable AI (XAI) Layer */}
      <div className="p-5 flex-1 bg-slate-900/10">
          <span className="text-[9px] text-slate-600 uppercase font-black tracking-widest flex items-center gap-2 mb-4">
              <BookOpen size={10} /> Why this signal? (XAI)
          </span>
          <div className="space-y-3">
            {signal.explanation?.map((reason: string, i: number) => (
                <div key={i} className="flex gap-3">
                    <div className="w-1 h-1 rounded-full bg-blue-500 mt-1.5 shrink-0" />
                    <p className="text-[10px] text-slate-400 font-medium leading-relaxed italic">{reason}</p>
                </div>
            )) || <p className="text-[10px] text-slate-600 italic">No structural data available.</p>}
          </div>
      </div>

      {/* Card Footer: Market Context */}
      <div className="p-4 bg-slate-900/60 border-t border-slate-800/60 flex justify-between items-center">
          <div className="flex gap-4 items-center">
            <ContextItem label="Session" value={signal.market_context?.session} />
            <ContextItem label="Regime" value={signal.market_context?.regime} />
            <ContextItem label="Volatility" value={signal.market_context?.volatility} />
          </div>
          <button className="text-[10px] font-black text-blue-400 hover:text-blue-300 uppercase tracking-widest flex items-center gap-2">
              Blueprint Details <ArrowUpRight size={12} />
          </button>
      </div>
    </motion.div>
  );
}

function RiskValue({ label, value, color }: any) {
    return (
        <div className="flex flex-col gap-1">
            <span className="text-[8px] text-slate-600 uppercase font-black tracking-[0.2em]">{label}</span>
            <span className={`text-[11px] font-mono font-black ${color}`}>{value || '0.00000'}</span>
        </div>
    );
}

function ContextItem({ label, value }: any) {
    return (
        <div className="flex flex-col">
            <span className="text-[7px] text-slate-600 uppercase font-bold tracking-widest leading-none mb-0.5">{label}</span>
            <span className="text-[9px] text-slate-300 font-black uppercase tracking-tighter">{value || 'N/A'}</span>
        </div>
    );
}

function FilterButton({ active, label, onClick, color = 'text-slate-500' }: any) {
    return (
        <button 
            onClick={onClick}
            className={`px-4 py-1 text-[9px] font-black tracking-widest rounded ${active ? 'bg-slate-800 text-white shadow-lg' : `${color} hover:text-slate-200`}`}
        >
            {label}
        </button>
    );
}
