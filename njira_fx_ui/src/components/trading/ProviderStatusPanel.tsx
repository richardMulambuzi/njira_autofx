'use client';

import React, { useEffect, useState } from 'react';
import { AlertTriangle, Radio, ShieldAlert } from 'lucide-react';
import { useReverb } from '@/hooks/useReverb';

/* ── Types ─────────────────────────────────────────────────────────── */

interface ProviderHealth {
  authentication_status: string;
  connection_status: string;
  last_market_data_update: string | null;
  account_sync_status: string;
  api_available: boolean;
  last_error: string | null;
}

interface ProviderStatusData {
  provider: string;
  environment: string;
  health: ProviderHealth | null;
  credentials_valid: boolean;
  safety_warnings: string[];
}

type ProviderStatusKind = 'connected' | 'disconnected' | 'authenticating' | 'auth_failed';
type MarketDataState = 'live' | 'stale' | 'no_data';

const STATUS_STYLES: Record<ProviderStatusKind, { label: string; dot: string; text: string }> = {
  connected: { label: 'Connected', dot: 'bg-emerald-400', text: 'text-emerald-400' },
  disconnected: { label: 'Disconnected', dot: 'bg-rose-400', text: 'text-rose-400' },
  authenticating: { label: 'Authenticating', dot: 'bg-amber-400', text: 'text-amber-400' },
  auth_failed: { label: 'Auth Failed', dot: 'bg-rose-500', text: 'text-rose-400' },
};

const MARKET_STYLES: Record<MarketDataState, { label: string; cls: string }> = {
  live: { label: 'Live', cls: 'text-emerald-400' },
  stale: { label: 'Stale', cls: 'text-amber-400' },
  no_data: { label: 'No Data', cls: 'text-slate-500' },
};

function deriveStatus(h: ProviderHealth): ProviderStatusKind {
  const a = h.authentication_status.toLowerCase();
  const c = h.connection_status.toLowerCase();
  if (a === 'waiting_for_authentication' || a === 'pending' || a === 'authenticating') return 'authenticating';
  if (a === 'failed' || a === 'authentication_failed' || a === 'invalid') return 'auth_failed';
  if (c === 'connected') return 'connected';
  if (c === 'connecting') return 'authenticating';
  return h.api_available ? 'connected' : 'disconnected';
}

function marketState(ts: string | null): MarketDataState {
  if (!ts) return 'no_data';
  const age = Date.now() - new Date(ts).getTime();
  return Number.isNaN(age) ? 'no_data' : age < 60_000 ? 'live' : 'stale';
}

/* ── Component ─────────────────────────────────────────────────────── */

export default function ProviderStatusPanel() {
  const { client } = useReverb();
  const [data, setData] = useState<ProviderStatusData | null>(null);

  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onStatus = (d: ProviderStatusData) => { if (d?.provider) setData(d); };
    channel.bind('provider_status', onStatus);
    return () => { channel.unbind('provider_status', onStatus); };
  }, [client]);

  // Skeleton while waiting for first Pusher event
  if (!data) {
    return (
      <section aria-label="Trading provider status" className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-md p-5">
        <div className="skeleton-shimmer mb-5 h-4 w-44 rounded" />
        <div className="space-y-3">
          <div className="skeleton-shimmer h-4 w-full rounded" />
          <div className="skeleton-shimmer h-4 w-3/4 rounded" />
          <div className="skeleton-shimmer h-4 w-5/6 rounded" />
        </div>
      </section>
    );
  }

  const health = data.health;
  const status = health ? STATUS_STYLES[deriveStatus(health)] : STATUS_STYLES.disconnected;
  const market = health ? MARKET_STYLES[marketState(health.last_market_data_update)] : MARKET_STYLES.no_data;
  const waitingForAuth = health?.authentication_status.toLowerCase() === 'waiting_for_authentication';

  return (
    <section aria-label="Trading provider status" className="relative overflow-hidden rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-md p-5">
      <div className="pointer-events-none absolute -top-20 -right-20 h-48 w-48 rounded-full bg-[hsl(262_80%_60%/0.1)] blur-3xl" />

      <header className="relative mb-5 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-[10px] font-black text-slate-500 uppercase tracking-[0.3em]">
          <span className="relative flex h-2 w-2">
            <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${status.dot}`} />
            <span className={`relative inline-flex h-2 w-2 rounded-full ${status.dot}`} />
          </span>
          Trading Provider
        </h3>
        <span className="text-[9px] font-bold uppercase tracking-widest text-slate-600">LIVE</span>
      </header>

      <div className="relative mb-4 flex items-center justify-between">
        <div className="flex min-w-0 items-center gap-2">
          <Radio size={14} className="shrink-0 text-[hsl(262_80%_60%)]" />
          <span className="truncate text-sm font-black uppercase tracking-tighter text-white">{data.provider}</span>
        </div>
        <EnvBadge env={data.environment} />
      </div>

      {waitingForAuth && (
        <div className="relative mb-4 flex items-start gap-2 rounded-none border border-amber-400/30 bg-amber-500/10 p-3">
          <AlertTriangle size={13} className="mt-0.5 shrink-0 text-amber-400" />
          <p className="text-[10px] font-bold uppercase tracking-widest text-amber-300">Waiting for authentication</p>
        </div>
      )}

      <dl className="relative space-y-3">
        <Row label="Status" value={status.label} dot={status.dot} valueClass={status.text} />
        <Row label="Market Data" value={market.label} valueClass={market.cls} />
        {health && (
          <>
            <Row label="API" value={health.api_available ? 'Available' : 'Unavailable'} dot={health.api_available ? 'bg-emerald-400' : 'bg-rose-400'} valueClass={health.api_available ? 'text-emerald-400' : 'text-rose-400'} />
            <Row label="Credentials" value={data.credentials_valid ? 'Valid' : 'Invalid'} dot={data.credentials_valid ? 'bg-emerald-400' : 'bg-rose-400'} valueClass={data.credentials_valid ? 'text-emerald-400' : 'text-rose-400'} />
          </>
        )}
      </dl>

      {data.safety_warnings.length > 0 && (
        <div className="relative mt-4 space-y-2 border-t border-slate-800/60 pt-4">
          <h4 className="flex items-center gap-2 text-[9px] font-black uppercase tracking-[0.3em] text-amber-400/80">
            <AlertTriangle size={11} /> Warnings
          </h4>
          {data.safety_warnings.map((w, i) => (
            <p key={i} className="text-[10px] font-medium leading-relaxed text-amber-300/80">{w}</p>
          ))}
        </div>
      )}
    </section>
  );
}

function Row({ label, value, dot, valueClass = 'text-slate-300' }: { label: string; value: string; dot?: string; valueClass?: string }) {
  return (
    <div className="group flex items-center justify-between gap-3">
      <dt className="text-[10px] font-bold uppercase tracking-widest text-slate-600 group-hover:text-slate-400 transition-colors">{label}</dt>
      <dd className={`flex items-center gap-2 text-[10px] font-black uppercase tracking-widest ${valueClass}`}>
        {dot && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />}
        {value}
      </dd>
    </div>
  );
}

function EnvBadge({ env }: { env: string }) {
  const cls = env === 'demo' ? 'border-emerald-400/30 bg-emerald-500/10 text-emerald-400'
    : env === 'live' ? 'border-rose-400/30 bg-rose-500/10 text-rose-400'
    : 'border-slate-700 bg-slate-800/50 text-slate-400';
  return <span className={`shrink-0 rounded-none border px-2 py-0.5 text-[9px] font-black uppercase tracking-[0.25em] ${cls}`}>{env}</span>;
}
