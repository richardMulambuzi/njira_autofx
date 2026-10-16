'use client';

import React, { useState, useEffect, useMemo } from 'react';
import TerminalLayout from '@/components/layout/TerminalLayout';
import { useAuthStore } from '@/store/useAuthStore';
import { useSymbols } from '@/hooks/useSymbols';
import api from '@/lib/api';
import { Settings, Check, Search, Plus, X, UserCheck } from 'lucide-react';

const STRATEGIES = [
  { id: 'malysian', label: 'Confluence', desc: 'Structure + Liquidity + Order Blocks + POC + Microstructure' },
  { id: 'Moving_averages', label: 'MTF SMA', desc: '10-SMA alignment across 7 timeframes (1M → 1D)' },
] as const;

export default function SettingsPage() {
  const { user } = useAuthStore();
  const email = user?.email ?? '';
  const {
    catalogue, available, selected, loading: symbolsLoading, brokerOnline,
    addSymbol, removeSymbol, saving: symbolsSaving,
  } = useSymbols();

  const [selectedStrategies, setSelectedStrategies] = useState<string[]>(['malysian']);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [symSearch, setSymSearch] = useState('');

  // Load current strategy
  useEffect(() => {
    if (!email) return;
    setLoading(true);
    api.get('/choosen-strategy/', { params: { email } })
      .then(res => {
        const s = res?.data?.strategy;
        if (s && typeof s === 'string') setSelectedStrategies(s.split(',').filter(Boolean));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [email]);

  const toggleStrategy = (id: string) => {
    setSelectedStrategies(prev =>
      prev.includes(id) ? prev.filter(s => s !== id) : [...prev, id]
    );
    setSaved(false);
  };

  const unselected = useMemo(() => {
    const pool = available.filter(s => !selected.includes(s));
    if (!symSearch) return pool;
    const q = symSearch.toUpperCase();
    return pool.filter(s => s.toUpperCase().includes(q));
  }, [available, selected, symSearch]);

  const handleSaveStrategy = async () => {
    if (!email || !selectedStrategies.length) return;
    setSaving(true);
    setError(null);
    try {
      await api.post('/save-strategy/', { email, strategy: selectedStrategies.join(','), trading: 'false' });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      setError('Save failed.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <TerminalLayout>
      <div className="max-w-3xl mx-auto flex flex-col gap-8">
        {/* Header */}
        <div className="flex items-center gap-3">
          <span className="w-2 h-8 bg-[hsl(262_80%_60%)] rounded-none"></span>
          <h2 className="text-xl font-black text-white tracking-tighter flex items-center gap-3">
            <Settings size={18} className="text-violet-400" />
            CONFIGURATION
          </h2>
        </div>

        {/* ── Strategy Selection ── */}
        <section className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5">
          <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em] mb-4">
            Strategy — select one or more
          </h3>
          <p className="text-xs text-slate-400 mb-5">
            Multiple strategies run independently. The strongest signal (highest confidence) wins.
          </p>
          {loading ? (
            <div className="space-y-3">
              <div className="h-14 skeleton-shimmer rounded-none" />
              <div className="h-14 skeleton-shimmer rounded-none" />
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {STRATEGIES.map(s => (
                <button
                  key={s.id}
                  onClick={() => toggleStrategy(s.id)}
                  className={`flex items-center gap-3 p-3 rounded-none border transition-colors text-left ${
                    selectedStrategies.includes(s.id)
                      ? 'border-violet-500/40 bg-violet-500/10'
                      : 'border-slate-800/40 bg-slate-800/10 hover:bg-slate-800/20'
                  }`}
                >
                  <div className={`w-4 h-4 rounded-none border flex items-center justify-center ${
                    selectedStrategies.includes(s.id) ? 'border-violet-400 bg-violet-500/20' : 'border-slate-600'
                  }`}>
                    {selectedStrategies.includes(s.id) && <Check size={10} className="text-violet-400" />}
                  </div>
                  <div>
                    <span className="text-sm font-bold text-white">{s.label}</span>
                    <span className="text-[10px] text-slate-500 ml-2">{s.desc}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </section>

        {/* ── Your Symbols (broker catalogue) ── */}
        <section className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <UserCheck size={14} className="text-emerald-400" />
              <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em]">
                Your Active Symbols
              </h3>
            </div>
            <span className={`flex items-center gap-1 text-[8px] font-bold uppercase tracking-widest px-1.5 py-0.5 rounded-none ${
              brokerOnline
                ? 'text-emerald-500 bg-emerald-500/10'
                : 'text-amber-500 bg-amber-500/10'
            }`}>
              <span className={`w-1.5 h-1.5 rounded-full ${brokerOnline ? 'bg-emerald-400' : 'bg-amber-400'}`} />
              cTrader {brokerOnline ? 'online' : 'offline'}
            </span>
          </div>
          <p className="text-xs text-slate-400 mb-4">
            {brokerOnline
              ? 'Symbols from your cTrader account. Click to add, ✕ to remove.'
              : 'cTrader offline — connect your broker to see available symbols.'}
          </p>

          {/* Selected chips */}
          {symbolsLoading ? (
            <div className="flex flex-wrap gap-2 mb-4">
              {[1,2,3].map(i => <div key={i} className="h-8 w-20 skeleton-shimmer rounded-none" />)}
            </div>
          ) : selected.length === 0 ? (
            <p className="text-xs text-slate-600 mb-4 italic">No symbols selected yet.</p>
          ) : (
            <div className="flex flex-wrap gap-2 mb-4">
              {selected.map(sym => (
                <span key={sym} className="flex items-center gap-1.5 px-3 py-1.5 rounded-none border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 text-xs font-bold tracking-wider">
                  {sym}
                  <button type="button" onClick={() => removeSymbol(sym)} disabled={symbolsSaving}
                    className="ml-1 p-0.5 rounded-none hover:bg-emerald-500/20 transition-colors disabled:opacity-40"
                    aria-label={`Remove ${sym}`}>
                    <X size={10} />
                  </button>
                </span>
              ))}
            </div>
          )}

          {/* Search + available grid */}
          <div className="border-t border-slate-800/40 pt-4">
            <div className="relative mb-3">
              <Search size={12} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-600" />
              <input type="text" value={symSearch} onChange={e => setSymSearch(e.target.value)}
                placeholder="Search symbols to add…"
                className="w-full bg-slate-900/60 border border-slate-800/60 rounded-none pl-8 pr-3 py-2 text-xs font-bold text-slate-200 placeholder:text-slate-700 focus:outline-none focus:border-emerald-500/50 transition-colors" />
            </div>
            {symbolsLoading ? (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {[1,2,3,4,5,6].map(i => <div key={i} className="h-12 skeleton-shimmer rounded-none" />)}
              </div>
            ) : unselected.length === 0 ? (
              <p className="text-xs text-slate-600 italic py-3">
                {symSearch ? 'No matching symbols.' : 'All symbols are in your selection.'}
              </p>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 max-h-64 overflow-y-auto custom-scrollbar">
                {unselected.map(sym => {
                  const meta = catalogue.find(c => c.symbol === sym);
                  return (
                    <button key={sym} type="button" onClick={() => addSymbol(sym)} disabled={symbolsSaving}
                      className="flex items-center justify-between gap-2 p-3 rounded-none border border-slate-800/40 bg-slate-800/10 hover:bg-slate-800/25 hover:border-emerald-500/30 transition-all text-left group disabled:opacity-40">
                      <div className="min-w-0">
                        <div className="text-xs font-bold text-slate-300 group-hover:text-white tracking-wider">{sym}</div>
                        {meta?.name && <div className="text-[10px] text-slate-600 truncate">{meta.name}</div>}
                      </div>
                      <Plus size={12} className="text-slate-600 group-hover:text-emerald-400 shrink-0 transition-colors" />
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        {/* Error */}
        {error && (
          <div className="px-4 py-2 rounded-none bg-rose-500/10 border border-rose-500/20 text-rose-400 text-[10px] font-bold uppercase tracking-wider">
            {error}
          </div>
        )}

        {/* Save Strategy */}
        <button onClick={handleSaveStrategy}
          disabled={loading || saving || !selectedStrategies.length}
          className={`self-start px-6 py-2.5 rounded-none border text-xs font-black uppercase tracking-widest transition-colors ${
            saved
              ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400'
              : 'border-violet-500/40 bg-violet-500/10 text-violet-400 hover:bg-violet-500/20'
          } disabled:opacity-40`}>
          {saving ? 'SAVING...' : saved ? 'SAVED' : 'SAVE STRATEGY'}
        </button>
      </div>
    </TerminalLayout>
  );
}
