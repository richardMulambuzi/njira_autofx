'use client';

import { useMemo } from 'react';
import { useMarketEvents } from '@/hooks/useMarketEvents';
import type { VolumeProfileEvent } from '@/types/market';

interface VolumeProfilePanelProps {
  symbol?: string;
  className?: string;
}

type Bin = {
  price: number;
  activity: number;
  bid_count: number;
  ask_count: number;
  delta: number;
  relative_activity: number;
  percentage: number;
};

const MAX_BINS = 30;

function priceDecimals(sym?: string): number {
  if (!sym) return 5;
  if (sym.includes('JPY')) return 2;
  if (sym === 'XAUUSD') return 3;
  return 5;
}

function fmtPrice(value: number, sym?: string): string {
  return value.toFixed(priceDecimals(sym));
}

function metricColor(v: number): string {
  if (v < 0.4) return 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10';
  if (v <= 0.7) return 'text-amber-400 border-amber-500/30 bg-amber-500/10';
  return 'text-rose-400 border-rose-500/30 bg-rose-500/10';
}

function migrationGlyph(m?: string): { glyph: string; label: string; cls: string } {
  if (m === 'migrating_up') return { glyph: '↑', label: 'migrating up', cls: 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10' };
  if (m === 'migrating_down') return { glyph: '↓', label: 'migrating down', cls: 'text-rose-400 border-rose-500/30 bg-rose-500/10' };
  return { glyph: '→', label: 'stable', cls: 'text-slate-400 border-slate-500/30 bg-slate-500/10' };
}

export default function VolumeProfilePanel({ symbol, className = '' }: VolumeProfilePanelProps) {
  const { volumeProfile, isConnected } = useMarketEvents();
  const vp = volumeProfile;
  const live = Boolean(vp && (vp.developing || vp.data_complete) && (!symbol || vp.symbol === symbol));

  const bins = useMemo<Bin[]>(() => {
    if (!vp) return [];
    const arr = Object.values(vp.bins) as Bin[];
    arr.sort((a, b) => b.price - a.price); // high → low (Y axis top→bottom)
    if (arr.length <= MAX_BINS) return arr;
    // slice the middle MAX_BINS around the POC
    const pocIdx = arr.findIndex((b) => b.price === vp.poc);
    const center = pocIdx < 0 ? Math.floor(arr.length / 2) : pocIdx;
    const start = Math.max(0, center - Math.floor(MAX_BINS / 2));
    return arr.slice(start, start + MAX_BINS);
  }, [vp]);

  const hvnSet = useMemo(() => new Set(vp?.hvn_levels ?? []), [vp]);
  const lvnSet = useMemo(() => new Set(vp?.lvn_levels ?? []), [vp]);

  return (
    <section
      className={`relative overflow-hidden rounded-2xl border border-white/10 bg-white/5 backdrop-blur-md ${className}`}
      aria-label="Volume Profile"
    >
      {/* Ambient radial glows */}
      <div className="pointer-events-none absolute -top-24 -left-24 h-48 w-48 rounded-full bg-cyan-500/10 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-24 -right-24 h-48 w-48 rounded-full bg-violet-500/10 blur-3xl" />

      <div className="relative p-4 flex flex-col gap-4">
        {/* 1. Header */}
        <header className="flex items-center justify-between">
          <h2 className="text-sm font-semibold tracking-wide text-slate-100">Volume Profile</h2>
          <div className="flex items-center gap-2">
            {vp?.methodology && (
              <span className="rounded-full border border-violet-400/30 bg-violet-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-violet-300">
                {vp.methodology}
              </span>
            )}
            <span className="flex items-center gap-1.5 text-[10px] font-medium text-slate-400">
              <span className="relative flex h-2 w-2">
                {live && (
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                )}
                <span
                  className={`relative inline-flex h-2 w-2 rounded-full transition-colors duration-500 ${
                    live ? 'bg-emerald-500' : 'bg-slate-600'
                  }`}
                />
              </span>
              {live ? 'live' : 'idle'}
            </span>
          </div>
        </header>

        {!vp ? (
          <EmptyState />
        ) : (
          <>
            {/* 2. Key stats grid */}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <StatCard label="POC" value={fmtPrice(vp.poc, symbol)} highlight />
              <StatCard label="VAH" value={fmtPrice(vp.vah, symbol)} />
              <StatCard label="VAL" value={fmtPrice(vp.val, symbol)} />
              <StatCard label="Activity" value={vp.total_activity.toLocaleString()} />
            </div>

            {/* 3. Horizontal profile histogram */}
            <div className="flex flex-col gap-1" role="img" aria-label="Volume profile histogram">
              {bins.map((b) => {
                const isPoc = b.price === vp.poc;
                const inValueArea = b.price <= vp.vah && b.price >= vp.val;
                const aboveVah = b.price > vp.vah;
                const isHvn = hvnSet.has(b.price);
                const isLvn = lvnSet.has(b.price);

                let barColor: string;
                if (isPoc) barColor = 'linear-gradient(90deg, hsl(190 90% 60%), hsl(270 90% 65%))';
                else if (inValueArea) barColor = 'linear-gradient(90deg, hsl(190 80% 55%), hsl(265 80% 62%))';
                else if (aboveVah) barColor = 'hsl(350 80% 60%)';
                else barColor = 'hsl(160 70% 50%)';

                const widthPct = Math.max(2, Math.min(100, b.relative_activity * 100));
                const opacity = isLvn ? 0.45 : 1;

                return (
                  <div key={b.price} className="group flex items-center gap-2">
                    <span className="w-20 shrink-0 text-right font-mono text-[10px] text-slate-400 group-hover:text-slate-200 transition-colors duration-300">
                      {fmtPrice(b.price, symbol)}
                    </span>
                    <div className="relative h-4 flex-1 overflow-hidden rounded-sm bg-white/5">
                      <div
                        className="h-full rounded-sm transition-all duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] group-hover:brightness-125"
                        style={{
                          width: `${widthPct}%`,
                          background: barColor,
                          opacity,
                          boxShadow: isPoc ? '0 0 12px 1px hsl(265 90% 65% / 0.7)' : isHvn ? '0 0 8px 0px hsl(190 90% 60% / 0.5)' : undefined,
                        }}
                      />
                    </div>
                    {isPoc && (
                      <span className="shrink-0 rounded-full border border-violet-300/60 px-1 text-[8px] font-bold text-violet-200 shadow-[0_0_8px_hsl(265_90%_65%/0.6)]">
                        POC
                      </span>
                    )}
                    {!isPoc && isHvn && (
                      <span className="shrink-0 text-[9px] font-bold text-cyan-300/80">H</span>
                    )}
                    {!isPoc && isLvn && (
                      <span className="shrink-0 text-[9px] font-bold text-slate-500">L</span>
                    )}
                  </div>
                );
              })}
            </div>

            {/* 4. Analytics row */}
            <div className="flex flex-wrap items-center gap-2">
              {vp.shape && (
                <Chip label="shape" value={vp.shape.replace('_', ' ')} cls="text-cyan-300 border-cyan-500/30 bg-cyan-500/10" />
              )}
              {vp.entropy != null && (
                <Chip label="entropy" value={vp.entropy.toFixed(2)} cls={metricColor(vp.entropy)} />
              )}
              {vp.reliability != null && (
                <Chip label="reliability" value={vp.reliability.toFixed(2)} cls={metricColor(vp.reliability)} />
              )}
              {vp.value_migration && (() => {
                const m = migrationGlyph(vp.value_migration);
                return (
                  <Chip label="value" value={`${m.glyph} ${m.label}`} cls={m.cls} />
                );
              })()}
            </div>
          </>
        )}
      </div>
    </section>
  );
}

function StatCard({ label, value, highlight = false }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div
      className={`rounded-xl border p-2.5 transition-colors duration-300 hover:border-white/20 ${
        highlight ? 'border-violet-400/40 bg-violet-500/10' : 'border-white/10 bg-white/5'
      }`}
    >
      <div className="text-[9px] font-semibold uppercase tracking-widest text-slate-400">{label}</div>
      <div className={`mt-0.5 font-mono text-sm ${highlight ? 'text-violet-200' : 'text-slate-100'}`}>{value}</div>
    </div>
  );
}

function Chip({ label, value, cls }: { label: string; value: string; cls: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${cls}`}>
      <span className="opacity-60 uppercase tracking-wider">{label}</span>
      <span className="font-mono">{value}</span>
    </span>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col gap-2 py-2" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex items-center gap-2">
          <div className="h-3 w-20 shrink-0 animate-pulse rounded bg-white/10" />
          <div className="h-4 flex-1 animate-pulse rounded-sm bg-gradient-to-r from-white/5 via-white/15 to-white/5" />
        </div>
      ))}
      <p className="pt-2 text-center text-[10px] text-slate-500">Awaiting volume profile data…</p>
    </div>
  );
}
