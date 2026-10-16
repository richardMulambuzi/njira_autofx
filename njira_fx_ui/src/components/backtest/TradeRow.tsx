'use client';

import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ChevronDown, TrendingUp, TrendingDown } from 'lucide-react';
import type { BacktestTrade } from '@/lib/api';
import ExecutionSteps from './ExecutionSteps';

function fmt(n: number): string {
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function TradeRow({ trade, index }: { trade: BacktestTrade; index: number }) {
  const [open, setOpen] = useState(false);
  const win = trade.win;
  const buy = trade.direction === 'BUY';

  return (
    <div className="rounded-none border border-slate-800/60 bg-slate-900/30 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-3 px-3 py-2.5 hover:bg-slate-800/30 transition-colors text-left"
      >
        <span className="text-[9px] font-mono text-slate-600 w-6">{index + 1}</span>
        <span
          className={`px-2 py-0.5 rounded-none text-[9px] font-black uppercase tracking-widest ${
            buy ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'
          }`}
        >
          {trade.direction}
        </span>
        <StrengthBadge strength={trade.strength} />
        <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex-1 truncate">
          {trade.strategy.replace('_', ' ')}
        </span>
        {trade.combined && trade.source_strategies.length > 0 && (
          <span className="px-1.5 py-0.5 rounded-none bg-violet-500/15 text-violet-300 text-[8px] font-black uppercase tracking-widest hidden sm:block">
            COMBINED · {trade.source_strategies.join('+').replace(/_/g, ' ')}
          </span>
        )}
        <span className="text-[10px] font-mono text-slate-500 hidden sm:block">
          {fmt(trade.entry)}
        </span>
        <span
          className={`text-[11px] font-mono font-black flex items-center gap-1 ${
            win ? 'text-emerald-400' : 'text-rose-400'
          }`}
        >
          {win ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
          {win ? '+' : ''}
          {fmt(trade.pnl)}
        </span>
        <ChevronDown
          size={14}
          className={`text-slate-600 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 pt-1 border-t border-slate-800/60 space-y-4">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-3">
                <Stat label="Entry" value={fmt(trade.entry)} />
                <Stat label="Stop Loss" value={fmt(trade.stop_loss)} tone="rose" />
                <Stat label="Take Profit" value={fmt(trade.take_profit)} tone="emerald" />
                <Stat label="Exit" value={fmt(trade.exit)} />
                <Stat label="Confidence" value={`${trade.confidence.toFixed(1)}%`} />
                <Stat label="Result" value={trade.outcome ? trade.outcome.toUpperCase() : win ? 'WIN' : 'LOSS'} tone={win ? 'emerald' : 'rose'} />
                <Stat label="Strength" value={trade.strength} tone={strengthTone(trade.strength)} />
                <Stat label="PnL" value={`${win ? '+' : ''}${fmt(trade.pnl)}`} tone={win ? 'emerald' : 'rose'} />
              </div>

              <div className="flex flex-col gap-1.5">
                <Stat label="Signal Entry Time" value={trade.entry_time ? new Date(trade.entry_time).toLocaleString() : '—'} />
                <Stat label="Exit Time" value={new Date(trade.exit_time).toLocaleString()} />
                {trade.combined && trade.source_strategies.length > 0 && (
                  <div className="flex flex-col gap-1">
                    <span className="text-[8px] text-slate-600 uppercase font-black tracking-widest mb-1">
                      Combined Attribution
                    </span>
                    <span className="text-[10px] font-mono font-black text-violet-300">
                      {trade.source_strategies.join(' + ').replace(/_/g, ' ')}
                    </span>
                  </div>
                )}
              </div>

              <div>
                <span className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">
                  Rationale
                </span>
                <p className="mt-1 text-[11px] leading-relaxed text-slate-400">{trade.explanation}</p>
              </div>

              <div>
                <span className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">
                  Execution Trace
                </span>
                <div className="mt-3">
                  <ExecutionSteps steps={trade.execution_steps} />
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

const STRENGTH_STYLES: Record<string, { badge: string; stat: string }> = {
  RECOMMENDED: { badge: 'bg-emerald-500/15 text-emerald-400', stat: 'text-emerald-400' },
  STRONG: { badge: 'bg-teal-500/15 text-teal-300', stat: 'text-teal-300' },
  NORMAL: { badge: 'bg-blue-500/15 text-blue-300', stat: 'text-blue-300' },
  WEAK: { badge: 'bg-amber-500/15 text-amber-400', stat: 'text-amber-400' },
  RISKY: { badge: 'bg-rose-500/15 text-rose-400', stat: 'text-rose-400' },
};

function strengthTone(strength: string): 'emerald' | 'rose' | 'amber' | 'blue' | 'teal' | undefined {
  switch (strength) {
    case 'RECOMMENDED':
      return 'emerald';
    case 'STRONG':
      return 'teal';
    case 'NORMAL':
      return 'blue';
    case 'WEAK':
      return 'amber';
    case 'RISKY':
      return 'rose';
    default:
      return undefined;
  }
}

function StrengthBadge({ strength }: { strength: string }) {
  const style = STRENGTH_STYLES[strength] ?? STRENGTH_STYLES.NORMAL;
  return (
    <span
      className={`px-2 py-0.5 rounded-none text-[9px] font-black uppercase tracking-widest ${style.badge}`}
    >
      {strength}
    </span>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'emerald' | 'rose' | 'amber' | 'blue' | 'teal';
}) {
  const toneClass =
    tone === 'emerald'
      ? 'text-emerald-400'
      : tone === 'rose'
        ? 'text-rose-400'
        : tone === 'amber'
          ? 'text-amber-400'
          : tone === 'blue'
            ? 'text-blue-300'
            : tone === 'teal'
              ? 'text-teal-300'
              : 'text-slate-200';
  return (
    <div className="flex flex-col">
      <span className="text-[8px] text-slate-600 uppercase font-black tracking-widest mb-1">
        {label}
      </span>
      <span className={`text-[10px] font-mono font-black ${toneClass}`}>{value}</span>
    </div>
  );
}
