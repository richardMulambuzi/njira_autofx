'use client';

import React, { useEffect, useState } from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';
import { useReverb } from '@/hooks/useReverb';
import { useSymbols } from '@/hooks/useSymbols';

interface Tick {
  symbol: string;
  bid: number;
  ask: number;
  ts: number;
}

export default function BottomMarketStream() {
  const { client } = useReverb();
  const { available } = useSymbols();
  const symbols = available.length > 0 ? available : ['EURUSD'];
  const [ticks, setTicks] = useState<Record<string, Tick>>({});
  const [prev, setPrev] = useState<Record<string, number>>({});

  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onTick = (data: Tick) => {
      if (!data?.symbol || !data?.bid) return;
      setPrev(p => ({ ...p, [data.symbol]: p[data.symbol] ?? data.bid }));
      setTicks(t => ({ ...t, [data.symbol]: data }));
    };
    channel.bind('tick', onTick);
    return () => { channel.unbind('tick', onTick); };
  }, [client]);

  return (
    <div className="h-full w-full flex items-center px-6 gap-8 overflow-hidden bg-[#05080f]">
      {/* Price Ticker */}
      <div className="flex-1 overflow-hidden flex items-center gap-10 whitespace-nowrap py-1">
        {symbols.map(sym => {
          const t = ticks[sym];
          const price = t?.bid;
          const oldPrice = prev[sym];
          const up = price != null && oldPrice != null ? price >= oldPrice : true;
          const changePct = price != null && oldPrice != null && oldPrice !== 0
            ? ((price - oldPrice) / oldPrice * 100).toFixed(2)
            : null;
          return (
            <div key={sym} className="flex items-center gap-2 font-mono text-[10px] hover:bg-slate-800/30 px-2 py-1 rounded-none transition-colors cursor-pointer group">
              <span className="text-slate-500 font-bold group-hover:text-slate-300">{sym}</span>
              <span className="text-slate-200">{price != null ? (sym.includes('JPY') || sym === 'XAUUSD' || sym === 'XTIUSD' || price > 100 ? price.toFixed(2) : price.toFixed(5)) : '—'}</span>
              {changePct && (
                <div className={`flex items-center gap-1 ${up ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {up ? <TrendingUp size={10} /> : <TrendingDown size={10} />}
                  <span>{up ? '+' : ''}{changePct}%</span>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* System Metrics */}
      <div className="hidden lg:flex items-center gap-6 border-l border-slate-800/60 pl-6 flex-shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-[8px] text-slate-600 uppercase font-bold tracking-widest">Live</span>
          <div className="relative flex h-2 w-2">
            <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${client ? 'bg-emerald-400' : 'bg-rose-400'} opacity-75`}></span>
            <span className={`relative inline-flex rounded-full h-2 w-2 ${client ? 'bg-emerald-500' : 'bg-rose-500'}`}></span>
          </div>
        </div>
      </div>
    </div>
  );
}
