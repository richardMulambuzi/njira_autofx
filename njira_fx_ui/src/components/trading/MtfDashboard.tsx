'use client';

import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Line, LineChart, ResponsiveContainer, YAxis } from 'recharts';
import { useReverb } from '@/hooks/useReverb';
import type { MtfStatusResponse, TimeframeInfo } from '@/types/ticks';
import { Activity, AlertCircle, Layers } from 'lucide-react';

/* ── Types ─────────────────────────────────────────────────────────── */

type MasterSignal = MtfStatusResponse['masterSignal'];

interface SparkPoint { index: number; close: number; }

interface MtfDashboardProps {
  symbol?: string;
}

interface SignalStyle {
  label: string;
  text: string;
  border: string;
  glow: string;
  badge: string;
  dot: string;
  sub: string;
}

/* ── Constants ─────────────────────────────────────────────────────── */

const TIME_FRAMES = ['1M', '5M', '15M', '30M', '1H', '4H', '1D'] as const;
const SMA_PERIOD = 20;

const SIGNAL_STYLES: Record<MasterSignal, SignalStyle> = {
  BUY: { label: 'BUY', text: 'text-emerald-300', border: 'border-emerald-400/40', glow: 'shadow-[0_0_70px_rgba(16,185,129,0.28)]', badge: 'border-emerald-400/30 bg-emerald-500/10 text-emerald-400', dot: 'bg-emerald-400', sub: 'Long confluence confirmed across aligned timeframes.' },
  SELL: { label: 'SELL', text: 'text-rose-300', border: 'border-rose-400/40', glow: 'shadow-[0_0_70px_rgba(244,63,94,0.28)]', badge: 'border-rose-400/30 bg-rose-500/10 text-rose-400', dot: 'bg-rose-400', sub: 'Short confluence confirmed across aligned timeframes.' },
  HOLD: { label: 'HOLD', text: 'text-slate-300', border: 'border-slate-700/70', glow: 'shadow-[0_0_40px_rgba(148,163,184,0.12)]', badge: 'border-slate-700 bg-slate-800/50 text-slate-400', dot: 'bg-slate-500', sub: 'No dominant confluence — stand aside until alignment forms.' },
};

/* ── Helpers ───────────────────────────────────────────────────────── */

function computeSma(history: TimeframeInfo['chart_history'] | undefined, period = SMA_PERIOD): number | null {
  const points = Array.isArray(history) ? history : [];
  const window = points.slice(-period);
  let sum = 0, count = 0;
  for (const p of window) {
    const c = p?.close;
    if (typeof c !== 'number' || !Number.isFinite(c)) continue;
    sum += c; count++;
  }
  return count > 0 ? sum / count : null;
}

/* ── Component ─────────────────────────────────────────────────────── */

export default function MtfDashboard({ symbol = 'EURUSD' }: MtfDashboardProps) {
  const { client } = useReverb();
  const [mtf, setMtf] = useState<MtfStatusResponse | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onMtf = (data: MtfStatusResponse) => {
      if (data?.symbol === symbol && data?.timeframes) {
        setMtf(data);
        setLastUpdated(new Date());
      }
    };
    channel.bind('mtf_status', onMtf);
    return () => { channel.unbind('mtf_status', onMtf); };
  }, [client, symbol]);

  // Skeleton while waiting for first Pusher event
  if (!mtf) {
    return (
      <section aria-label={`Multi-timeframe confluence for ${symbol}`} className="relative overflow-hidden rounded-xl border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-md p-5 shadow-2xl">
        <div className="absolute -top-24 -left-24 h-64 w-64 rounded-full bg-[hsl(190_90%_55%/0.1)] blur-3xl" />
        <div className="mb-5 flex items-center justify-between">
          <div className="skeleton-shimmer h-4 w-44 rounded" />
          <div className="skeleton-shimmer h-4 w-24 rounded" />
        </div>
        <div className="skeleton-shimmer mb-5 h-24 w-full rounded" />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7">
          {TIME_FRAMES.map(tf => <div key={tf} className="skeleton-shimmer h-36 rounded-lg" />)}
        </div>
      </section>
    );
  }

  const masterSignal: MasterSignal = mtf.masterSignal ?? 'HOLD';
  const style = SIGNAL_STYLES[masterSignal];

  return (
    <section aria-label={`Multi-timeframe confluence for ${symbol}`} className="relative overflow-hidden rounded-xl border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-md shadow-2xl">
      <div className="pointer-events-none absolute -top-28 -left-28 h-72 w-72 rounded-full bg-[hsl(190_90%_55%/0.1)] blur-3xl" />
      <div className="pointer-events-none absolute -bottom-32 -right-24 h-72 w-72 rounded-full bg-[hsl(262_80%_60%/0.12)] blur-3xl" />

      <header className="relative flex items-center justify-between border-b border-slate-800/60 px-5 py-4">
        <div className="flex items-center gap-3">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-[hsl(190_90%_55%/0.35)] bg-[hsl(190_90%_55%/0.1)]">
            <Layers size={14} className="text-[hsl(190_90%_55%)]" />
          </span>
          <div>
            <h3 className="text-[11px] font-black uppercase tracking-[0.25em] text-slate-200">Multi-Timeframe Confluence</h3>
            <p className="text-[9px] font-bold uppercase tracking-[0.2em] text-slate-600">{symbol} · 7-Frame Alignment Engine</p>
          </div>
        </div>
        {lastUpdated && (
          <span className="hidden items-center gap-1.5 text-[9px] font-bold uppercase tracking-widest text-slate-600 sm:flex">
            <Activity size={10} />
            {lastUpdated.toLocaleTimeString([], { hour12: false })}
          </span>
        )}
      </header>

      <div className="relative p-5">
        <motion.div key={masterSignal} initial={{ opacity: 0, scale: 0.97 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
          className={`relative mb-5 flex flex-col items-center justify-center gap-2 overflow-hidden rounded-xl border px-6 py-6 ${style.border} ${style.glow}`}>
          <span className={`flex items-center gap-2 rounded-full border px-3 py-1 text-[9px] font-black uppercase tracking-[0.3em] ${style.badge}`}>
            <span className="relative flex h-1.5 w-1.5">
              <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 ${style.dot}`} />
              <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${style.dot}`} />
            </span>
            Master Signal
          </span>
          <span className={`font-mono text-5xl font-black italic tracking-tighter ${style.text}`}>{style.label}</span>
          <p className="text-[10px] font-medium uppercase tracking-[0.2em] text-slate-500">{style.sub}</p>
        </motion.div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7">
          {TIME_FRAMES.map((tf, idx) => (
            <TfCard key={tf} name={tf} info={mtf.timeframes?.[tf]} index={idx} />
          ))}
        </div>
      </div>
    </section>
  );
}

/* ── Timeframe card ────────────────────────────────────────────────── */

function TfCard({ name, info, index }: { name: string; info?: TimeframeInfo; index: number }) {
  const aligned = info?.aligned ?? false;
  const sma = computeSma(info?.chart_history);
  const rawHistory = info?.chart_history;
  const sparkData: SparkPoint[] = (Array.isArray(rawHistory) ? rawHistory : [])
    .filter(p => typeof p?.close === 'number' && Number.isFinite(p.close))
    .map((p, i) => ({ index: i, close: p.close }));

  return (
    <motion.article
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, delay: index * 0.05, ease: [0.22, 1, 0.36, 1] }}
      className="group relative flex flex-col overflow-hidden rounded-lg border border-slate-800/60 bg-[#05080f]/40 p-3 transition-all duration-300 hover:border-slate-700 hover:bg-[#0a0e17]/60"
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="font-mono text-[11px] font-black tracking-widest text-slate-300">{name}</span>
        <span className={`flex items-center gap-1.5 text-[8px] font-black uppercase tracking-widest ${aligned ? 'text-emerald-400' : 'text-rose-400'}`}>
          <span className="relative flex h-1.5 w-1.5">
            <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${aligned ? 'bg-emerald-400' : 'bg-rose-400'}`} />
            <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${aligned ? 'bg-emerald-400' : 'bg-rose-400'}`} />
          </span>
          {aligned ? 'Aligned' : 'Diverged'}
        </span>
      </div>
      <div className="mb-1 flex items-baseline gap-1.5">
        <span className="font-mono text-sm font-black tracking-tighter text-slate-100">
          {info && typeof info.current_value === 'number' && Number.isFinite(info.current_value) ? info.current_value.toFixed(5) : '—'}
        </span>
        <span className="text-[8px] font-bold uppercase tracking-widest text-slate-600">close</span>
      </div>
      <div className="mb-2 flex items-baseline gap-1.5">
        <span className="font-mono text-[10px] font-bold text-slate-400">{sma !== null ? sma.toFixed(5) : '—'}</span>
        <span className="text-[8px] font-bold uppercase tracking-widest text-slate-600">SMA({SMA_PERIOD})</span>
      </div>
      <div className="mt-auto h-12 w-full">
        {sparkData.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={sparkData} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
              <YAxis domain={['auto', 'auto']} hide />
              <Line type="monotone" dataKey="close" stroke={aligned ? 'hsl(160 84% 50%)' : 'hsl(350 84% 60%)'} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-[8px] font-bold uppercase tracking-widest text-slate-700">No history</div>
        )}
      </div>
    </motion.article>
  );
}
