'use client';

import React, { useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { tradeApi } from '@/lib/api';
import { useReverb } from '@/hooks/useReverb';
import type { Tick } from '@/types/ticks';
import {
  Activity,
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  Clock,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  Zap,
} from 'lucide-react';

/* ── Types ─────────────────────────────────────────────────────────── */

type TickDirection = 'up' | 'down' | 'flat';
type TradeStatus = 'idle' | 'submitting' | 'success' | 'blocked' | 'error';

interface ChartPoint { time: string; bid: number; }
interface ScalperTooltipProps { active?: boolean; label?: string; payload?: ReadonlyArray<{ value?: number | string }>; }
interface ScalperDashboardProps { symbol?: string; }

/* ── Helpers ───────────────────────────────────────────────────────── */

const MAX_TICKS = 300;

function formatTickTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour12: false });
}

/* ── Component ─────────────────────────────────────────────────────── */

export default function ScalperDashboard({ symbol = 'EURUSD' }: ScalperDashboardProps) {
  const { client } = useReverb();
  const [ticks, setTicks] = useState<Tick[]>([]);
  const [direction, setDirection] = useState<TickDirection>('flat');
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [connected, setConnected] = useState(false);

  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY');
  const [volume, setVolume] = useState('100');
  const [tradeStatus, setTradeStatus] = useState<TradeStatus>('idle');
  const [tradeMessage, setTradeMessage] = useState<string | null>(null);

  const ticksRef = useRef<Tick[]>([]);

  // Subscribe to Pusher tick events
  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onTick = (data: { symbol: string; bid: number; ask: number; spread: number; timestamp: number }) => {
      if (data?.symbol !== symbol || typeof data?.bid !== 'number') return;
      const tick: Tick = {
        bid: data.bid,
        ask: data.ask,
        spread: data.spread,
        time: new Date(data.timestamp).toISOString(),
      };
      setTicks(prev => {
        const next = [...prev, tick];
        return next.length > MAX_TICKS ? next.slice(-MAX_TICKS) : next;
      });
      ticksRef.current = [...ticksRef.current, tick].slice(-MAX_TICKS);
      // Direction
      const last = ticksRef.current;
      if (last.length >= 2) {
        const prev = last[last.length - 2];
        if (tick.bid > prev.bid) setDirection('up');
        else if (tick.bid < prev.bid) setDirection('down');
        else setDirection('flat');
      }
      setLastUpdated(new Date());
    };
    channel.bind('tick', onTick);
    setConnected(true);
    return () => { channel.unbind('tick', onTick); };
  }, [client, symbol]);

  const latest = ticks[ticks.length - 1] ?? null;
  const chartData: ChartPoint[] = ticks.map(t => ({ time: formatTickTime(t.time), bid: t.bid }));

  const handleExecute = async () => {
    const vol = Number(volume);
    if (!Number.isFinite(vol) || vol < 1 || vol > 1_000_000) {
      setTradeStatus('error');
      setTradeMessage('Volume must be between 1 and 1,000,000.');
      return;
    }
    setTradeStatus('submitting');
    setTradeMessage(null);
    try {
      const res = await tradeApi.execute({ symbol, side, volume: vol });
      const data = res.data;
      if (data.position_id) {
        setTradeStatus('success');
        setTradeMessage(`Position opened · ${data.position_id}`);
      } else if (data.reason) {
        setTradeStatus('blocked');
        setTradeMessage(data.reason);
      } else {
        setTradeStatus('success');
        setTradeMessage(data.message ?? 'Order accepted.');
      }
    } catch (err: unknown) {
      setTradeStatus('error');
      const msg = err instanceof Error ? err.message : 'Execution failed.';
      setTradeMessage(msg);
    }
  };

  const volumeInvalid = !Number.isFinite(Number(volume)) || Number(volume) < 1 || Number(volume) > 1_000_000;

  const directionColor = direction === 'up' ? 'text-emerald-400' : direction === 'down' ? 'text-rose-400' : 'text-slate-300';

  return (
    <section aria-label={`Scalper dashboard for ${symbol}`} className="relative overflow-hidden rounded-xl border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-md shadow-2xl">
      <div className="pointer-events-none absolute -top-28 -right-28 h-72 w-72 rounded-full bg-[hsl(262_80%_60%/0.14)] blur-3xl" />
      <div className="pointer-events-none absolute -bottom-32 -left-24 h-72 w-72 rounded-full bg-[hsl(190_90%_55%/0.08)] blur-3xl" />

      <header className="relative flex items-center justify-between border-b border-slate-800/60 px-5 py-4">
        <div className="flex items-center gap-3">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-[hsl(262_80%_60%/0.35)] bg-[hsl(262_80%_60%/0.12)]">
            <Zap size={14} className="text-[hsl(262_80%_60%)]" />
          </span>
          <div>
            <h3 className="text-[11px] font-black uppercase tracking-[0.25em] text-slate-200">Live Scalper</h3>
            <p className="text-[9px] font-bold uppercase tracking-[0.2em] text-slate-600">{symbol} · Pepperstone Tick Stream</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[9px] font-black uppercase tracking-widest ${connected ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-400' : 'border-rose-500/25 bg-rose-500/10 text-rose-400'}`}>
            <span className="relative flex h-1.5 w-1.5">
              <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 ${connected ? 'bg-emerald-400' : 'bg-rose-400'}`} />
              <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${connected ? 'bg-emerald-500' : 'bg-rose-500'}`} />
            </span>
            {connected ? 'Live' : 'Offline'}
          </span>
          {lastUpdated && (
            <span className="hidden items-center gap-1.5 text-[9px] font-bold uppercase tracking-widest text-slate-600 sm:flex">
              <Clock size={10} />
              {lastUpdated.toLocaleTimeString([], { hour12: false })}
            </span>
          )}
        </div>
      </header>

      <div className="relative p-5">
        {/* Live price */}
        <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
          <div className="flex items-end gap-4">
            <AnimatePresence mode="popLayout">
              <motion.div key={latest?.bid ?? 'empty'} initial={{ opacity: 0.35, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.3, ease: 'easeOut' }} className="flex items-baseline gap-2">
                <span className={`font-mono text-4xl font-black tracking-tighter transition-colors duration-300 ${directionColor}`}>
                  {latest ? latest.bid.toFixed(5) : '—'}
                </span>
                <span className="text-[10px] font-bold uppercase tracking-widest text-slate-600">bid</span>
              </motion.div>
            </AnimatePresence>
            <div className="flex flex-col gap-1 pb-1">
              <span className="font-mono text-sm font-bold text-slate-400">{latest ? latest.ask.toFixed(5) : '—'}</span>
              <span className="text-[9px] font-bold uppercase tracking-widest text-slate-600">ask</span>
            </div>
          </div>
          <div className="flex items-center gap-3 pb-1">
            <span className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[10px] font-black uppercase tracking-widest transition-all duration-300 ${
              direction === 'up' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
              : direction === 'down' ? 'border-rose-500/30 bg-rose-500/10 text-rose-400'
              : 'border-slate-700 bg-slate-800/40 text-slate-400'
            }`}>
              {direction === 'up' ? <ArrowUpRight size={12} /> : direction === 'down' ? <ArrowDownRight size={12} /> : <Activity size={12} />}
              {direction === 'up' ? 'Rising' : direction === 'down' ? 'Falling' : 'Flat'}
            </span>
            <span className="rounded-full border border-[hsl(190_90%_55%/0.3)] bg-[hsl(190_90%_55%/0.08)] px-3 py-1.5 font-mono text-[10px] font-black tracking-widest text-[hsl(190_90%_55%)]">
              {latest ? `${latest.spread.toFixed(1)} pips` : '— pips'}
            </span>
          </div>
        </div>

        {/* Chart */}
        <div className="mb-5 h-56 w-full rounded-lg border border-slate-800/50 bg-[#05080f]/60 p-2">
          {chartData.length > 1 ? (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="scalperBidFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="hsl(262 80% 60%)" stopOpacity={0.32} />
                    <stop offset="100%" stopColor="hsl(262 80% 60%)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="time" hide />
                <YAxis domain={['auto', 'auto']} width={58} tick={{ fill: '#475569', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }} tickFormatter={(v: number) => v.toFixed(4)} axisLine={false} tickLine={false} />
                <Tooltip content={<ScalperTooltip />} cursor={{ stroke: 'hsl(262 80% 60% / 0.35)', strokeWidth: 1 }} />
                <Area type="monotone" dataKey="bid" stroke="hsl(262 80% 60%)" strokeWidth={1.5} fill="url(#scalperBidFill)" isAnimationActive={false} dot={false} activeDot={{ r: 3, fill: 'hsl(190 90% 55%)', stroke: 'none' }} />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-full items-center justify-center text-[10px] font-bold uppercase tracking-widest text-slate-700">
              Waiting for ticks…
            </div>
          )}
        </div>

        {/* Trade panel */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_auto]">
          <div className="rounded-lg border border-slate-800/50 bg-[#05080f]/40 p-4">
            <div className="mb-4 flex items-center justify-between">
              <span className="text-[9px] font-black uppercase tracking-[0.25em] text-slate-600">Execution Panel</span>
              <span className="text-[9px] font-bold uppercase tracking-widest text-slate-600">{symbol}</span>
            </div>

            <div role="group" aria-label="Trade direction" className="mb-4 grid grid-cols-2 gap-2">
              <button type="button" aria-pressed={side === 'BUY'} onClick={() => setSide('BUY')} className={`flex items-center justify-center gap-2 rounded-lg border px-4 py-2.5 text-[10px] font-black uppercase tracking-[0.2em] transition-all duration-300 ${side === 'BUY' ? 'border-emerald-400/40 bg-emerald-500/15 text-emerald-300 shadow-[0_0_20px_rgba(16,185,129,0.15)]' : 'border-slate-800 bg-slate-900/40 text-slate-500 hover:border-slate-700 hover:text-slate-300'}`}>
                <TrendingUp size={12} /> Buy
              </button>
              <button type="button" aria-pressed={side === 'SELL'} onClick={() => setSide('SELL')} className={`flex items-center justify-center gap-2 rounded-lg border px-4 py-2.5 text-[10px] font-black uppercase tracking-[0.2em] transition-all duration-300 ${side === 'SELL' ? 'border-rose-400/40 bg-rose-500/15 text-rose-300 shadow-[0_0_20px_rgba(244,63,94,0.15)]' : 'border-slate-800 bg-slate-900/40 text-slate-500 hover:border-slate-700 hover:text-slate-300'}`}>
                <TrendingDown size={12} /> Sell
              </button>
            </div>

            <label className="mb-1.5 block text-[9px] font-black uppercase tracking-[0.25em] text-slate-600">
              Volume <span className="text-slate-700">(0.01-lot units · 1 – 1,000,000)</span>
            </label>
            <input type="number" inputMode="decimal" min={1} max={1_000_000} step={0.01} value={volume} onChange={e => setVolume(e.target.value)} aria-invalid={volumeInvalid}
              className={`w-full rounded-lg border bg-slate-900/60 px-4 py-2.5 font-mono text-sm font-bold text-slate-200 transition-all duration-300 focus:outline-none ${volumeInvalid ? 'border-rose-500/50 focus:border-rose-400' : 'border-slate-800 focus:border-[hsl(190_90%_55%/0.5)]'}`} />

            <button type="button" onClick={() => void handleExecute()} disabled={tradeStatus === 'submitting' || volumeInvalid}
              className={`mt-4 flex w-full items-center justify-center gap-2 rounded-lg border px-4 py-3 text-[10px] font-black uppercase tracking-[0.25em] transition-all duration-300 ${side === 'BUY' ? 'border-emerald-400/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-40' : 'border-rose-400/40 bg-rose-500/15 text-rose-300 hover:bg-rose-500/25 disabled:opacity-40'}`}>
              {tradeStatus === 'submitting' ? <><Activity size={12} className="animate-pulse" /> Routing…</> : <><Zap size={12} /> Execute {side}</>}
            </button>
          </div>

          {/* Status */}
          <div className="flex w-full flex-col justify-between rounded-lg border border-slate-800/50 bg-[#05080f]/40 p-4 lg:w-64">
            <span className="text-[9px] font-black uppercase tracking-[0.25em] text-slate-600">Order Status</span>
            <AnimatePresence mode="wait">
              <motion.div key={tradeStatus + (tradeMessage ?? '')} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.25, ease: 'easeOut' }} className="flex flex-col gap-2">
                {tradeStatus === 'idle' && <p className="text-[11px] font-medium leading-relaxed text-slate-600">Awaiting execution command.</p>}
                {tradeStatus === 'success' && <p className="flex items-start gap-2 text-[11px] font-bold leading-relaxed text-emerald-400"><ShieldCheck size={14} className="mt-0.5 shrink-0" />{tradeMessage}</p>}
                {tradeStatus === 'blocked' && <p className="flex items-start gap-2 text-[11px] font-bold leading-relaxed text-amber-400"><AlertCircle size={14} className="mt-0.5 shrink-0" />{tradeMessage}</p>}
                {tradeStatus === 'error' && <p className="flex items-start gap-2 text-[11px] font-bold leading-relaxed text-rose-400"><AlertCircle size={14} className="mt-0.5 shrink-0" />{tradeMessage}</p>}
              </motion.div>
            </AnimatePresence>
            <span className="text-[9px] font-bold uppercase tracking-widest text-slate-700">{connected ? 'Stream healthy' : 'Waiting for connection…'}</span>
          </div>
        </div>
      </div>
    </section>
  );
}

/* ── Tooltip ───────────────────────────────────────────────────────── */

function ScalperTooltip({ active, payload, label }: ScalperTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const value = payload[0]?.value;
  return (
    <div className="rounded-lg border border-slate-700/80 bg-[#0a0e17]/95 px-3 py-2 shadow-2xl backdrop-blur-md">
      <p className="font-mono text-[10px] font-bold text-slate-400">{label}</p>
      <p className="font-mono text-xs font-black text-[hsl(262_80%_60%)]">{typeof value === 'number' ? value.toFixed(5) : value}</p>
    </div>
  );
}
