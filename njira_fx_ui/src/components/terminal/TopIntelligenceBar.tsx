'use client';

import React, { useState, useEffect, useMemo } from 'react';
import { Globe, Clock, Zap, Activity } from 'lucide-react';

export default function TopIntelligenceBar() {
  const [time, setTime] = useState(new Date());

  useEffect(() => {
    const timer = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  const utcHour = time.getUTCHours();

  const sessionStatus = useMemo(() => {
    const sessions = [
      { name: 'Sydney', open: 22, close: 7 },
      { name: 'Tokyo', open: 0, close: 9 },
      { name: 'London', open: 8, close: 17 },
      { name: 'New York', open: 13, close: 22 }
    ];

    return sessions.map(s => {
      let isOpen = false;
      if (s.open < s.close) {
        isOpen = utcHour >= s.open && utcHour < s.close;
      } else {
        isOpen = utcHour >= s.open || utcHour < s.close;
      }
      return { ...s, isOpen };
    });
  }, [utcHour]);

  const activeOverlaps = useMemo(() => {
    const active = sessionStatus.filter(s => s.isOpen).map(s => s.name);
    if (active.includes('Tokyo') && active.includes('London')) return 'Tokyo + London (High Volatility)';
    if (active.includes('London') && active.includes('New York')) return 'London + New York (Peak Liquidity)';
    if (active.includes('Sydney') && active.includes('Tokyo')) return 'Sydney + Tokyo';
    return active.length > 0 ? active.join(' + ') : 'Market Quiet';
  }, [sessionStatus]);

  const formatTime = (date: Date, offset = 0) => {
    const d = new Date(date.getTime() + offset * 3600000);
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };

  return (
    <div className="h-full w-full flex items-center justify-between px-6 text-[10px] font-bold tracking-[0.2em] uppercase">
      
      {/* Market Sessions Monitor (UTC) */}
      <div className="flex items-center gap-8">
        <div className="flex items-center gap-3 pr-6 border-r border-slate-800/60">
          <div className="w-2 h-2 rounded-none bg-blue-500 shadow-[0_0_8px_rgba(59,130,246,0.5)]"></div>
          <span className="text-white text-xs tracking-tighter italic">Terminal v2.4</span>
        </div>

        <div className="flex items-center gap-6">
          {sessionStatus.map(s => (
            <SessionItem key={s.name} label={s.name} status={s.isOpen ? 'OPEN' : 'CLOSED'} color={s.isOpen ? 'text-emerald-400' : 'text-slate-600'} />
          ))}
        </div>

        <div className="flex items-center gap-2 text-blue-400 bg-blue-500/5 px-3 py-1 rounded-none border border-blue-500/10">
          <Zap size={10} className="animate-pulse" />
          <span className="text-[9px]">Overlap: {activeOverlaps}</span>
        </div>
      </div>

      {/* Global Market State */}
      <div className="flex items-center gap-10">
        <div className="flex items-center gap-3">
          <Activity size={12} className="text-amber-500" />
          <div className="flex flex-col leading-none">
            <span className="text-[8px] text-slate-600 mb-0.5 uppercase tracking-widest">Regime</span>
            <span className="text-amber-400 font-black">Trending</span>
          </div>
        </div>

        <div className="flex items-center gap-6 border-l border-slate-800/60 pl-6">
          <TimeItem label="Broker" time={formatTime(time, -2)} />
          <TimeItem label="UTC" time={formatTime(time, 0)} color="text-blue-400" />
          <TimeItem label="Local" time={formatTime(time)} />
        </div>
      </div>

    </div>
  );
}

function SessionItem({ label, status, color }: { label: string, status: string, color: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-slate-500 text-[9px]">{label}</span>
      <span className={`${color} text-[9px]`}>{status}</span>
    </div>
  );
}

function TimeItem({ label, time, color = "text-slate-400" }: { label: string, time: string, color?: string }) {
  return (
    <div className="flex flex-col items-end leading-none">
      <span className="text-[7px] text-slate-600 mb-0.5">{label}</span>
      <span className={`${color} font-mono text-[10px]`}>{time}</span>
    </div>
  );
}
