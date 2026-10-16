'use client';

import React from 'react';
import { History } from 'lucide-react';

interface PastResultsProps {
  items: { id: number; symbol: string; csv_name: string; created_at: string; overall_win_rate: number; best_strategy: string; total_trades: number }[];
  loading: boolean;
  activeId: number | null;
  onSelect: (id: number) => void;
}

export default function PastResults({ items, loading, activeId, onSelect }: PastResultsProps) {
  return (
    <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5">
      <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em] mb-5 flex items-center gap-2">
        <History size={14} /> Recent Runs
      </h3>

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-12 w-full skeleton-shimmer rounded-none" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <p className="text-[10px] text-slate-700 uppercase tracking-widest font-bold">
          No backtests yet
        </p>
      ) : (
        <div className="space-y-2 max-h-[320px] overflow-y-auto custom-scrollbar">
          {items.map((r) => {
            const active = r.id === activeId;
            return (
              <button
                key={r.id}
                type="button"
                onClick={() => onSelect(r.id)}
                className={`w-full text-left rounded-none border px-3 py-2.5 transition-all ${
                  active
                    ? 'border-amber-500/40 bg-amber-500/5'
                    : 'border-slate-800/60 bg-slate-900/30 hover:border-slate-700'
                }`}
              >
                <div className="flex justify-between items-center">
                  <span className="text-[10px] font-black text-slate-200 uppercase tracking-widest">
                    #{r.id} · {r.symbol}
                  </span>
                  <span className="text-[9px] font-mono text-emerald-400">
                    {r.overall_win_rate.toFixed(1)}%
                  </span>
                </div>
                <div className="mt-1 flex justify-between items-center">
                  <span className="text-[9px] text-slate-600 font-bold uppercase tracking-wider truncate max-w-[60%]">
                    {r.csv_name}
                  </span>
                  <span className="text-[9px] text-slate-500 font-mono">
                    {r.total_trades} trades
                  </span>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
