'use client';

import React, { useState } from 'react';
import TerminalLayout from '@/components/layout/TerminalLayout';
import ScalperDashboard from '@/components/trading/ScalperDashboard';
import MtfDashboard from '@/components/trading/MtfDashboard';
import ProviderStatusPanel from '@/components/trading/ProviderStatusPanel';
import { useSymbols } from '@/hooks/useSymbols';
import { Cpu, Info, Newspaper } from 'lucide-react';
import { useDashboardData } from '@/hooks/useDashboardData';

export default function DashboardPage() {
  const { available, selected } = useSymbols();
  // Use selected symbols for the tab bar; fall back to available; final fallback EURUSD
  const symbols = selected.length > 0 ? selected : available.length > 0 ? available : ['EURUSD'];

  const [selectedSymbol, setSelectedSymbol] = useState<string>(() => {
    if (typeof window === 'undefined') return 'EURUSD';
    const stored = localStorage.getItem('njira-symbol');
    return stored ?? 'EURUSD';
  });

  const { analysis } = useDashboardData(selectedSymbol, '15m');

  const handleSymbolChange = (s: string) => {
    setSelectedSymbol(s);
    localStorage.setItem('njira-symbol', s);
  };

  return (
    <TerminalLayout>
      <div className="flex flex-col gap-6">

        {/* Intelligence Header */}
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h2 className="text-xl font-black text-white tracking-tighter flex items-center gap-3">
              <span className="w-2 h-8 bg-[hsl(262_80%_60%)] rounded-none"></span>
              MARKET ANALYSIS ENGINE
            </h2>
            <p className="text-[10px] text-slate-500 font-bold uppercase tracking-[0.3em]">Structural Intelligence & Sentiment Layer</p>
          </div>
          <div className="flex flex-wrap bg-slate-900/50 rounded-none p-1 border border-slate-800/60">
            {symbols.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => handleSymbolChange(s)}
                className={`px-3 py-1 text-[9px] font-black tracking-widest rounded-none transition-all duration-300 ${
                  selectedSymbol === s
                    ? 'bg-[hsl(262_80%_60%)] text-white shadow-[0_0_18px_hsl(262_80%_60%/0.45)]'
                    : 'text-slate-500 hover:text-slate-300'
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Master Panel + Structural / Sentiment Sidebar */}
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">

          {/* Master scalper panel */}
          <div className="xl:col-span-8 min-w-0">
            <ScalperDashboard symbol={selectedSymbol} />
          </div>

          {/* Sidebar: Provider, Structural & Sentiment Intelligence */}
          <div className="xl:col-span-4 flex flex-col gap-6">

            {/* Trading Provider Status */}
            <ProviderStatusPanel />

            {/* Structure Monitor */}
            <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5">
              <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em] mb-5 flex items-center gap-2">
                <Cpu size={14} className="text-blue-400" /> Market Structure
              </h3>
              <div className="space-y-4">
                <IndicatorRow label="BOS (Break of Structure)" value={analysis?.structure?.bos ? 'DETECTED' : 'NOT FOUND'} active={analysis?.structure?.bos ?? false} />
                <IndicatorRow label="CHoCH (Character Change)" value={analysis?.structure?.choch ? 'DETECTED' : 'STABLE'} active={analysis?.structure?.choch ?? false} />
                <IndicatorRow label="Order Block" value={analysis?.structure?.order_block_active ? 'MITIGATED' : 'WAITING'} active={analysis?.structure?.order_block_active ?? false} color="text-amber-400" />
                <IndicatorRow label="Regime Type" value={analysis?.regime ?? 'PENDING'} active={true} color="text-blue-400" />
                <IndicatorRow label="Volatility" value={analysis?.volatility ?? 'MEDIUM'} active={true} color="text-indigo-400" />
              </div>
            </div>

            {/* Sentiment Feed */}
            <div className="flex-1 rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5 flex flex-col">
              <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em] mb-5 flex items-center gap-2">
                <Newspaper size={14} className="text-emerald-400" /> Sentiment Intelligence
              </h3>

              <div className="flex items-center gap-4 mb-6 p-4 rounded-none bg-slate-900/40 border border-slate-800/60">
                <div className="flex flex-col">
                  <span className="text-[8px] text-slate-600 uppercase font-black mb-1">Sentiment Score</span>
                  <span className={`text-2xl font-mono font-black ${(analysis?.sentiment?.score ?? 0) > 0.6 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {analysis?.sentiment?.score ?? '0.00'}
                  </span>
                </div>
                <div className="h-10 w-px bg-slate-800" />
                <div className="flex flex-col">
                  <span className="text-[8px] text-slate-600 uppercase font-black mb-1">Global Bias</span>
                  <span className={`text-xs font-black italic ${(analysis?.sentiment?.bias ?? '') === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {analysis?.sentiment?.bias ?? 'NEUTRAL'}
                  </span>
                </div>
              </div>

              <div className="space-y-3 flex-1 overflow-y-auto custom-scrollbar">
                {analysis?.sentiment?.news_drivers?.map((driver: string, idx: number) => (
                  <div key={idx} className="flex gap-3 p-3 rounded-none bg-slate-800/20 border border-slate-800/40 hover:bg-slate-800/40 transition-colors group">
                    <Info size={14} className="text-slate-600 group-hover:text-blue-400 shrink-0 mt-0.5" />
                    <p className="text-[10px] text-slate-400 font-medium leading-relaxed uppercase tracking-tight">{driver}</p>
                  </div>
                ))}
              </div>
            </div>

          </div>

        </div>

        {/* Multi-Timeframe Confluence Engine */}
        <MtfDashboard symbol={selectedSymbol} />

      </div>
    </TerminalLayout>
  );
}

interface IndicatorRowProps {
  label: string;
  value: string;
  active: boolean;
  color?: string;
}

function IndicatorRow({ label, value, active, color = 'text-emerald-400' }: IndicatorRowProps) {
  return (
    <div className="flex justify-between items-center group">
      <span className="text-[10px] font-bold text-slate-600 uppercase tracking-widest group-hover:text-slate-400 transition-colors">{label}</span>
      <span className={`text-[10px] font-black italic ${active ? color : 'text-slate-700'} tracking-tighter transition-all`}>
        {value}
      </span>
    </div>
  );
}
