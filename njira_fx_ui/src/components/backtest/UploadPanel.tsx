'use client';

import React, { useRef, useState } from 'react';
import { Upload, Play, FileUp, X } from 'lucide-react';

export const BACKTEST_STRATEGIES = [
  'malysian',
  'Moving_averages',
  'order_block',
  'fvg',
  'liquidity',
  'structure',
] as const;

interface UploadPanelProps {
  uploading: boolean;
  onUpload: (
    file: File,
    higherTfFiles: File[],
    symbol: string,
    strategies: string[],
  ) => void;
}

export default function UploadPanel({ uploading, onUpload }: UploadPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [higherTfFiles, setHigherTfFiles] = useState<File[]>([]);
  const [symbol, setSymbol] = useState('');
  const [strategies, setStrategies] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const higherTfInputRef = useRef<HTMLInputElement>(null);

  const toggleStrategy = (s: string) =>
    setStrategies((prev) =>
      prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s],
    );

  const onHigherTfChange = (files: FileList | null) => {
    if (!files) return;
    setHigherTfFiles((prev) => [...prev, ...Array.from(files)]);
  };

  const removeHigherTf = (idx: number) =>
    setHigherTfFiles((prev) => prev.filter((_, i) => i !== idx));

  const submit = () => {
    if (!file || uploading) return;
    onUpload(file, higherTfFiles, symbol.trim(), strategies);
  };

  return (
    <div className="rounded-none border border-slate-800/60 bg-[#0d131f]/50 backdrop-blur-sm p-5">
      <h3 className="text-[10px] font-black text-slate-500 uppercase tracking-[0.3em] mb-5 flex items-center gap-2">
        <Upload size={14} /> Dataset & Parameters
      </h3>

      <div className="space-y-5">
        <div>
          <span className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">
            Base dataset (e.g. M15)
          </span>
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="mt-2 w-full flex items-center justify-center gap-2 px-4 py-6 rounded-none border border-dashed border-slate-700/70 bg-slate-900/30 hover:border-amber-500/40 hover:bg-amber-500/5 transition-all"
          >
            <FileUp size={16} className="text-amber-500" />
            <span className="text-[10px] font-black uppercase tracking-widest text-slate-300">
              {file ? file.name : 'Select .csv base dataset'}
            </span>
          </button>
          <input
            ref={inputRef}
            type="file"
            accept=".csv"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>

        <div>
          <span className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">
            Higher timeframes (optional, e.g. H1, H4, D1)
          </span>
          <button
            type="button"
            onClick={() => higherTfInputRef.current?.click()}
            className="mt-2 w-full flex items-center justify-center gap-2 px-4 py-4 rounded-none border border-dashed border-slate-700/70 bg-slate-900/30 hover:border-amber-500/40 hover:bg-amber-500/5 transition-all"
          >
            <FileUp size={14} className="text-slate-400" />
            <span className="text-[9px] font-black uppercase tracking-widest text-slate-500">
              Select higher-TF .csv files (multi-select)
            </span>
          </button>
          <input
            ref={higherTfInputRef}
            type="file"
            accept=".csv"
            multiple
            className="hidden"
            onChange={(e) => onHigherTfChange(e.target.files)}
          />
          {higherTfFiles.length > 0 && (
            <ul className="mt-2 space-y-1.5">
              {higherTfFiles.map((f, i) => (
                <li
                  key={`${f.name}-${i}`}
                  className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-none border border-slate-800/60 bg-slate-900/40"
                >
                  <span className="text-[10px] font-mono text-slate-300 truncate">
                    {f.name}
                  </span>
                  <button
                    type="button"
                    onClick={() => removeHigherTf(i)}
                    aria-label={`Remove ${f.name}`}
                    className="text-slate-500 hover:text-rose-400 transition-colors"
                  >
                    <X size={12} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[9px] leading-relaxed text-slate-700 uppercase tracking-wider font-bold">
            Upload the finest timeframe as base; higher timeframes are used
            directly when provided, otherwise aggregated from the base.
          </p>
        </div>

        <div>
          <label className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">
            Symbol (optional)
          </label>
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="XAUUSD"
            className="mt-2 w-full rounded-none bg-slate-900/40 border border-slate-800/60 px-3 py-2 text-[11px] font-mono text-slate-200 placeholder:text-slate-700 focus:border-blue-500/40 focus:outline-none"
          />
        </div>

        <div>
          <span className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">
            Strategies (optional)
          </span>
          <div className="mt-2 flex flex-wrap gap-2">
            {BACKTEST_STRATEGIES.map((s) => {
              const active = strategies.includes(s);
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => toggleStrategy(s)}
                  className={`px-2.5 py-1 rounded-none text-[9px] font-black uppercase tracking-widest border transition-all ${
                    active
                      ? 'bg-blue-600/15 text-blue-300 border-blue-500/40'
                      : 'bg-slate-900/30 text-slate-500 border-slate-800/60 hover:text-slate-300'
                  }`}
                >
                  {s.replace('_', ' ')}
                </button>
              );
            })}
          </div>
        </div>

        <button
          type="button"
          onClick={submit}
          disabled={!file || uploading}
          className={`w-full px-6 py-3 rounded-none font-black text-[10px] uppercase tracking-[0.2em] transition-all flex items-center justify-center gap-2 ${
            !file || uploading
              ? 'bg-slate-800/40 text-slate-600 cursor-not-allowed'
              : 'bg-amber-600 text-white hover:bg-amber-500 shadow-[0_0_18px_rgba(245,158,11,0.25)]'
          }`}
        >
          {uploading ? (
            <>
              <span className="w-28 h-3 skeleton-shimmer rounded-none" />
            </>
          ) : (
            <>
              <Play size={14} /> Run Backtest
            </>
          )}
        </button>
      </div>
    </div>
  );
}
