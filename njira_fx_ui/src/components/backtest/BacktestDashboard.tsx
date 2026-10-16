'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { History, Database } from 'lucide-react';
import { backtestApi, type BacktestResult, type BacktestSummary } from '@/lib/api';
import UploadPanel from './UploadPanel';
import PastResults from './PastResults';
import ResultsView from './ResultsView';

/** Format a seconds count as a compact "Xm Ys" / "Ys" string for the ETA. */
function formatEta(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}m ${r}s` : `${r}s`;
}

export default function BacktestDashboard() {
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [past, setPast] = useState<BacktestSummary[]>([]);
  const [uploading, setUploading] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [loadingList, setLoadingList] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [progress, setProgress] = useState<{
    percent: number;
    signals_so_far: number;
    processed: number;
    total: number;
    elapsedSec: number;
    etaSec: number;
  } | null>(null);

  useEffect(() => {
    backtestApi
      .list()
      .then((res) => setPast(res.data))
      .catch(() => setPast([]))
      .finally(() => setLoadingList(false));
  }, []);

  const handleUpload = useCallback(
    async (
      file: File,
      higherTfFiles: File[],
      symbol: string,
      strategies: string[],
    ) => {
      setUploading(true);
      setError(null);
      setProgress(null);
      const startedAt = Date.now();
      try {
        const res = await backtestApi.uploadStream(
          file,
          higherTfFiles,
          symbol || undefined,
          strategies,
          (p) => {
            const elapsedSec = (Date.now() - startedAt) / 1000;
            const pct = p.percent > 0 ? p.percent : 1;
            const etaSec = pct >= 100 ? 0 : (elapsedSec / pct) * (100 - pct);
            setProgress({
              percent: p.percent,
              signals_so_far: p.signals_so_far,
              processed: p.processed,
              total: p.total,
              elapsedSec,
              etaSec,
            });
          },
        );
        setResult(res);
        setActiveId(res.id);
        setPast((prev) => [
          {
            id: res.id,
            symbol: res.symbol,
            csv_name: res.csv_name,
            created_at: new Date().toISOString(),
            overall_win_rate: res.overall_win_rate,
            best_strategy: res.best_strategy,
            total_trades: res.trades.length,
          },
          ...prev.filter((p) => p.id !== res.id),
        ]);
      } catch {
        setError('Backtest run failed. Verify the CSV format and your session, then retry.');
      } finally {
        setUploading(false);
        setProgress(null);
      }
    },
    [],
  );

  const handleSelect = useCallback(async (id: number) => {
    setLoadingDetail(true);
    setError(null);
    setActiveId(id);
    try {
      const res = await backtestApi.detail(id);
      setResult(res.data);
    } catch {
      setError('Could not load that backtest result.');
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex justify-between items-end mb-2">
        <div className="flex flex-col gap-1">
          <h2 className="text-xl font-black text-white tracking-tighter flex items-center gap-3">
            <span className="w-2 h-8 bg-amber-600 rounded-none" />
            STRATEGY BACKTEST ENGINE
          </h2>
          <p className="text-[10px] text-slate-500 font-bold uppercase tracking-[0.3em]">
            Historical Data Simulation & Performance Verification
          </p>
        </div>
        {result && (
          <span className="text-[10px] font-black uppercase tracking-[0.2em] text-emerald-400 flex items-center gap-2">
            <Database size={14} /> Run #{result.id} · {result.symbol}
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        <div className="xl:col-span-4 flex flex-col gap-6">
          <UploadPanel uploading={uploading} onUpload={handleUpload} />
          <PastResults
            items={past}
            loading={loadingList}
            activeId={activeId}
            onSelect={handleSelect}
          />
        </div>

        <div className="xl:col-span-8">
          {error && (
            <motion.div
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              className="mb-4 px-4 py-3 rounded-none border border-rose-500/30 bg-rose-500/10 text-[10px] font-bold uppercase tracking-widest text-rose-300"
            >
              {error}
            </motion.div>
          )}
          {progress && (
            <motion.div
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              className="mb-4 px-5 py-4 rounded-none border border-amber-500/20 bg-slate-900/60 backdrop-blur-md"
            >
              <div className="flex items-center justify-between mb-2">
                <span className="text-[10px] font-black uppercase tracking-[0.25em] text-amber-400">
                  Replaying {progress.processed.toLocaleString()} /{' '}
                  {progress.total.toLocaleString()} candles
                </span>
                <span className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">
                  {progress.percent.toFixed(1)}%
                </span>
              </div>
              <div className="h-1.5 w-full bg-slate-800 rounded-none overflow-hidden">
                <motion.div
                  className="h-full bg-gradient-to-r from-amber-600 via-amber-500 to-emerald-500"
                  animate={{ width: `${Math.max(2, progress.percent)}%` }}
                  transition={{ ease: 'easeOut', duration: 0.3 }}
                />
              </div>
              <div className="flex items-center justify-between mt-2 text-[10px] font-bold uppercase tracking-widest">
                <span className="text-emerald-400">
                  {progress.signals_so_far} signal{progress.signals_so_far === 1 ? '' : 's'} so far
                </span>
                <span className="text-slate-500">
                  {progress.etaSec > 0
                    ? `~${formatEta(progress.etaSec)} remaining`
                    : 'finalizing…'}
                </span>
              </div>
            </motion.div>
          )}
          <ResultsView result={result} loading={uploading || loadingDetail} />
        </div>
      </div>
    </div>
  );
}
