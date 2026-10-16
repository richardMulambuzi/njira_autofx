'use client';

import React from 'react';
import { motion } from 'framer-motion';
import { BarChart3, History } from 'lucide-react';
import type { BacktestResult } from '@/lib/api';
import TradeRow from './TradeRow';

interface ResultsViewProps {
  result: BacktestResult | null;
  loading: boolean;
}

function fmt(n: number): string {
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Map a timeframe in seconds to a human label. */
function formatTimeframe(seconds: number): string {
  const label: Record<number, string> = {
    60: 'M1',
    300: 'M5',
    900: 'M15',
    1800: 'M30',
    3600: 'H1',
    14400: 'H4',
    86400: 'D1',
    604800: 'W1',
  };
  return label[seconds] ?? `${seconds}s`;
}

export default function ResultsView({ result, loading }: ResultsViewProps) {
  if (loading) return <LoadingSkeleton />;
  if (!result) return <IdleState />;

  const bestPassing = [...result.per_strategy]
    .filter((s) => s.passes_70_gate)
    .sort((a, b) => b.win_rate - a.win_rate)[0];
  const highlight = bestPassing?.strategy ?? result.best_strategy;

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="flex flex-col gap-6"
    >
      {/* Summary strip */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-px bg-slate-800/40 rounded-none border border-slate-800/60 overflow-hidden">
        <SummaryCell label="Symbol" value={result.symbol} />
        <SummaryCell label="Initial → Final" value={`${fmt(result.initial_balance)} → ${fmt(result.final_balance)}`} />
        <SummaryCell
          label="Total PnL"
          value={`${result.total_pnl >= 0 ? '+' : ''}${fmt(result.total_pnl)}`}
          tone={result.total_pnl >= 0 ? 'emerald' : 'rose'}
        />
        <SummaryCell label="Overall Win Rate" value={`${result.overall_win_rate.toFixed(1)}%`} />
        <SummaryCell label="Best Strategy" value={result.best_strategy.replace('_', ' ')} tone="amber" />
      </div>

      {/* Timeframes strip */}
      {result.timeframes_used.length > 0 && (
        <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm px-4 py-3 flex flex-wrap items-center gap-x-5 gap-y-2">
          <span className="text-[9px] font-black text-slate-500 uppercase tracking-[0.3em]">
            Timeframes
          </span>
          {result.timeframes_used.map((tf) => (
            <span key={tf.timeframe} className="flex items-center gap-2">
              <span className="text-[11px] font-mono font-black text-slate-200">
                {formatTimeframe(tf.timeframe)}
              </span>
              <span
                className={`px-1.5 py-0.5 rounded-none text-[8px] font-black uppercase tracking-widest ${
                  tf.source === 'uploaded'
                    ? 'bg-emerald-500/15 text-emerald-400'
                    : 'bg-slate-800/60 text-slate-400'
                }`}
              >
                {tf.source}
              </span>
            </span>
          ))}
        </div>
      )}

      {/* Per-strategy table */}
      <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5">
        <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em] mb-5 flex items-center gap-2">
          <BarChart3 size={14} /> Per-Strategy Performance
        </h3>
        <div className="overflow-x-auto custom-scrollbar">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="text-[9px] font-black uppercase tracking-widest text-slate-600">
                <th className="py-2 pr-4">Strategy</th>
                <th className="py-2 pr-4 text-right">Trades</th>
                <th className="py-2 pr-4 text-right">W / L</th>
                <th className="py-2 pr-4 text-right">Win Rate</th>
                <th className="py-2 pr-4 text-right">Profit F.</th>
                <th className="py-2 pr-4 text-right">PnL</th>
                <th className="py-2 pr-4 text-right">Expect.</th>
              </tr>
            </thead>
            <tbody>
              {result.per_strategy.map((s) => {
                const isBest = s.strategy === highlight;
                return (
                  <tr
                    key={s.strategy}
                    className={`border-t border-slate-800/60 text-[11px] ${
                      isBest ? 'bg-amber-500/5' : ''
                    }`}
                  >
                    <td className="py-2.5 pr-4">
                      <div className="flex items-center gap-2">
                        <span className="font-black text-slate-200 uppercase tracking-wider">
                          {s.strategy.replace('_', ' ')}
                        </span>
                        {isBest && (
                          <span className="px-1.5 py-0.5 rounded-none bg-amber-600/20 text-amber-400 text-[8px] font-black uppercase tracking-widest">
                            Best
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-slate-300">
                      {s.total_trades}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-slate-400">
                      <span className="text-emerald-400">{s.wins}</span>
                      <span className="text-slate-600"> / </span>
                      <span className="text-rose-400">{s.losses}</span>
                    </td>
                    <td className="py-2.5 pr-4 text-right">
                      <div className="flex flex-col items-end gap-1">
                        <span className="font-mono font-black text-slate-200">
                          {s.win_rate.toFixed(1)}%
                        </span>
                        <GateBadge passes={s.passes_70_gate} />
                      </div>
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-slate-300">
                      {s.profit_factor.toFixed(2)}
                    </td>
                    <td
                      className={`py-2.5 pr-4 text-right font-mono font-black ${
                        s.total_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'
                      }`}
                    >
                      {s.total_pnl >= 0 ? '+' : ''}
                      {fmt(s.total_pnl)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-slate-300">
                      {s.expectancy.toFixed(1)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Trades with execution trace */}
      <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5">
        <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em] mb-5 flex items-center gap-2">
          <History size={14} /> Trade Execution Trace ({result.trades.length})
        </h3>
        {result.trades.length === 0 ? (
          <p className="text-[10px] text-slate-700 uppercase tracking-widest font-bold">
            No trades generated
          </p>
        ) : (
          <div className="space-y-2">
            {result.trades.map((t, i) => (
              <TradeRow key={i} trade={t} index={i} />
            ))}
          </div>
        )}
      </div>
    </motion.div>
  );
}

function GateBadge({ passes }: { passes: boolean }) {
  return (
    <span
      className={`px-1.5 py-0.5 rounded-none text-[8px] font-black uppercase tracking-widest ${
        passes ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'
      }`}
    >
      {passes ? 'PASS ≥70%' : 'BELOW 70%'}
    </span>
  );
}

function SummaryCell({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'emerald' | 'rose' | 'amber';
}) {
  const toneClass =
    tone === 'emerald'
      ? 'text-emerald-400'
      : tone === 'rose'
        ? 'text-rose-400'
        : tone === 'amber'
          ? 'text-amber-400'
          : 'text-slate-100';
  return (
    <div className="bg-[#0d131f]/60 px-4 py-3 flex flex-col gap-1">
      <span className="text-[8px] text-slate-600 uppercase font-black tracking-widest">
        {label}
      </span>
      <span className={`text-[11px] font-mono font-black ${toneClass}`}>{value}</span>
    </div>
  );
}

function IdleState() {
  return (
    <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm h-[600px] flex flex-col items-center justify-center text-slate-700 opacity-30">
      <History size={64} />
      <span className="text-sm font-black uppercase tracking-[0.5em] mt-4 text-center">
        Engine Idle
        <br />
        <span className="text-[10px]">Upload a dataset to run a backtest</span>
      </span>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-px bg-slate-800/40 rounded-none border border-slate-800/60 overflow-hidden">
        {[1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="bg-[#0d131f]/60 px-4 py-3">
            <div className="h-2 w-12 skeleton-shimmer rounded-none mb-2" />
            <div className="h-3 w-16 skeleton-shimmer rounded-none" />
          </div>
        ))}
      </div>
      <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 p-5 space-y-3">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-10 w-full skeleton-shimmer rounded-none" />
        ))}
      </div>
      <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 p-5 space-y-3">
        {[1, 2, 3, 4].map((i) => (
          <div key={i} className="h-12 w-full skeleton-shimmer rounded-none" />
        ))}
      </div>
    </div>
  );
}
