'use client';

import React, { useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useSignalStore, SignalData } from '@/store/useSignalStore';
import { signalApi } from '@/lib/api';
import { useReverb } from '@/hooks/useReverb';

export default function LiveSignalFeed() {
  const { signals, setSignals, addSignal } = useSignalStore();
  const { client } = useReverb();

  // Initial fetch
  useEffect(() => {
    signalApi.live().then((res) => {
      if (res.data?.signals) setSignals(res.data.signals);
    }).catch(() => {});
  }, [setSignals]);

  // Live subscription — backend broadcasts on 'njira-market' channel
  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onSignal = (data: SignalData) => {
      if (data?.direction && data?.symbol) addSignal(data);
    };
    channel.bind('signal', onSignal);
    return () => { channel.unbind('signal', onSignal); client.unsubscribe('njira-market'); };
  }, [client, addSignal]);

  return (
    <div className="flex flex-col h-full">
      <div className="h-12 border-b border-slate-800/60 flex items-center px-4 bg-slate-900/50 flex-shrink-0">
        <h3 className="font-medium text-slate-200 text-sm tracking-wide flex items-center gap-2">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2 w-2 bg-rose-500"></span>
          </span>
          Live Signal Feed
          <span className="text-xs text-slate-500 ml-auto">{signals.length} signals</span>
        </h3>
      </div>
      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-2 custom-scrollbar">
        <AnimatePresence mode="popLayout">
          {signals.length === 0 ? (
            <div className="text-center text-slate-600 text-sm py-8">
              Waiting for signals...
            </div>
          ) : (
            signals.map((signal, index) => (
              <motion.div
                key={signal.id ?? signal.created_at ?? index}
                initial={{ opacity: 0, x: 20, scale: 0.95 }}
                animate={{ opacity: 1, x: 0, scale: 1 }}
                exit={{ opacity: 0, x: -20 }}
                transition={{ duration: 0.3, ease: 'easeOut' }}
              >
                <SignalCard signal={signal} />
              </motion.div>
            ))
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}

function SignalCard({ signal }: { signal: SignalData }) {
  const dirColor = signal.direction === 'BUY'
    ? 'emerald' : signal.direction === 'SELL'
    ? 'rose' : 'slate';

  // Use new `state` field first, fall back to old `status` mapping
  const rawState = signal.state ?? signal.status ?? '';
  const statusLabel = rawState === 'CONFIRMED' ? 'CONFIRMED'
    : rawState === 'WAITING' ? 'WAITING'
    : rawState === 'INVALIDATED' ? 'INVALIDATED'
    : rawState === 'EXPIRED' ? 'EXPIRED'
    : rawState === 'CONFIRMED_SIGNAL' ? 'CONFIRMED'
    : rawState === 'REJECTED_CONFLICT' ? 'REJECTED'
    : 'PENDING';

  const statusBg = statusLabel === 'CONFIRMED'
    ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
    : statusLabel === 'WAITING'
    ? 'bg-amber-500/10 border-amber-500/20 text-amber-400'
    : statusLabel === 'INVALIDATED' || statusLabel === 'REJECTED'
    ? 'bg-rose-500/10 border-rose-500/20 text-rose-400'
    : statusLabel === 'EXPIRED'
    ? 'bg-slate-500/10 border-slate-500/20 text-slate-500'
    : 'bg-slate-500/10 border-slate-500/20 text-slate-400';

  const contradiction = signal.confidence_evidence?.contradiction;
  const confScore = signal.confidence_score ?? signal.confidence ?? signal.final_score ?? 0;

  return (
    <div className="rounded-lg border border-slate-800/40 bg-slate-800/20 p-3 hover:bg-slate-800/40 transition-colors cursor-pointer group">
      <div className="flex justify-between items-center mb-2">
        <div className="flex items-center gap-2">
          <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded tracking-wider bg-${dirColor}-500/20 text-${dirColor}-400 border border-${dirColor}-500/20`}>
            {signal.direction}
          </span>
          <span className="font-bold text-sm text-slate-200">{signal.symbol}</span>
          <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/20">{signal.timeframe}</span>
          {signal.strategy && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-400 border border-violet-500/20 uppercase tracking-wider">
              {signal.strategy === 'malysian' ? 'CONFLUENCE' : signal.strategy === 'moving_averages' ? 'MTF SMA' : signal.strategy}
            </span>
          )}
          {signal.strength && (
            <span className={`text-[9px] px-1.5 py-0.5 rounded uppercase tracking-wider border ${
              signal.strength === 'STRONG' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
              : signal.strength === 'NORMAL' ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
              : 'bg-slate-500/10 text-slate-400 border-slate-500/20'
            }`}>
              {signal.strength}
            </span>
          )}
        </div>
        <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded tracking-wider border ${statusBg}`}>
          {statusLabel}
        </span>
      </div>
      <div className="flex justify-between items-end">
        <div className="flex flex-col gap-1">
          <span className="text-[10px] text-slate-500">Regime: {signal.market_regime}</span>
          <span className="text-xs text-slate-400 font-mono">
            {signal.created_at ? new Date(signal.created_at).toLocaleTimeString() : '—'}
          </span>
          {signal.risk_reward_ratio != null && signal.risk_reward_ratio > 0 && (
            <span className="text-[10px] text-slate-500">R:R {signal.risk_reward_ratio.toFixed(1)}</span>
          )}
        </div>
        <div className="flex flex-col items-end gap-1">
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-slate-500">Score</span>
            <span className="text-sm font-bold text-white">{confScore}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="w-16 h-1.5 bg-slate-800 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full ${confScore > 80 ? 'bg-emerald-400' : confScore > 50 ? 'bg-amber-400' : 'bg-slate-400'}`}
                style={{ width: `${Math.min(confScore, 100)}%` }}
              ></div>
            </div>
            <span className="text-xs font-medium text-slate-300">{confScore}%</span>
          </div>
        </div>
      </div>
      {/* Contradiction warning for WAITING signals */}
      {statusLabel === 'WAITING' && contradiction && (
        <div className="mt-2 pt-2 border-t border-amber-500/20 bg-amber-500/5 -mx-3 -mb-3 px-3 pb-2 rounded-b-lg">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/20 uppercase tracking-wider">
              {contradiction.severity}
            </span>
            <span className="text-[9px] text-amber-400/70 font-medium">
              net {contradiction.net_confidence?.toFixed(0) ?? '—'}
            </span>
          </div>
          {contradiction.reasons?.slice(0, 2).map((reason: string, i: number) => (
            <p key={i} className="text-[9px] text-amber-400/60 leading-relaxed mt-0.5">{reason}</p>
          ))}
        </div>
      )}
    </div>
  );
}
