'use client';

import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

export default function BootScreen({ onComplete }: { onComplete: () => void }) {
  const [logs, setLogs] = useState<string[]>([]);
  const [mounted, setMounted] = useState(false);
  const bootSequence = [
    "[INIT] Kernel Version 2.4.0-FX-INSTITUTIONAL",
    "[INIT] Market Data Engine........ OK",
    "[INIT] Strategy Engine........... OK",
    "[INIT] Sentiment Engine.......... OK",
    "[INIT] Risk Engine............... OK",
    "[INIT] WebSocket Server.......... CONNECTED",
    "[INIT] Broker Connection......... PEPPERSTONE LINKED",
    "[INIT] Synchronizing Candles..... 100%",
    "[INIT] Building Market Structure. 100%",
    "[INIT] INITIAL MARKET SCAN COMPLETE",
    "[READY] TERMINAL LIVE MODE"
  ];

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    let index = 0;
    const interval = setInterval(() => {
      if (index < bootSequence.length) {
        setLogs(prev => [...prev, bootSequence[index]]);
        index++;
      } else {
        clearInterval(interval);
        setTimeout(onComplete, 800);
      }
    }, 250);
    return () => clearInterval(interval);
  }, [onComplete]);

  return (
    <div className="fixed inset-0 bg-[#05080f] z-[9999] flex flex-col items-center justify-center font-mono">
      <div className="w-full max-w-xl p-8 border border-slate-800 bg-slate-900/20 backdrop-blur-sm relative overflow-hidden">
        {/* Decorative Grid */}
        <div className="absolute inset-0 opacity-5 pointer-events-none" style={{ backgroundImage: 'linear-gradient(#475569 1px, transparent 1px), linear-gradient(90deg, #475569 1px, transparent 1px)', backgroundSize: '20px 20px' }}></div>
        
        <div className="relative z-10">
            <div className="mb-10 flex justify-between items-end">
                <div>
                    <h1 className="text-2xl font-black tracking-[0.4em] text-white italic">AUTO FX MW</h1>
                    <p className="text-[10px] text-blue-500 font-bold uppercase tracking-widest mt-1">Institutional Quant Terminal</p>
                </div>
                <div className="text-right">
                    <span className="text-[10px] text-slate-600 font-bold">BOOT SEQUENCE v2.4</span>
                </div>
            </div>

            <div className="h-1.5 w-full bg-slate-800 mb-8 overflow-hidden">
                <motion.div 
                    initial={{ width: 0 }}
                    animate={{ width: '100%' }}
                    transition={{ duration: 3 }}
                    className="h-full bg-blue-600 shadow-[0_0_15px_rgba(37,99,235,0.8)]"
                />
            </div>

            <div className="space-y-1.5 text-[10px] text-slate-400 h-[220px] overflow-hidden flex flex-col justify-end">
                {logs.map((log, i) => (
                    <motion.div 
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        key={i} 
                        className="flex gap-4"
                    >
                        <span className="text-slate-600 font-mono">[{mounted ? new Date().toLocaleTimeString([], { hour12: false }) : '--:--:--'}]</span>
                        <span className={`font-mono ${typeof log === 'string' && (log.includes('OK') || log.includes('CONNECTED') || log.includes('LIVE') || log.includes('COMPLETE')) ? 'text-emerald-400 font-bold' : ''}`}>
                            {log}
                        </span>
                    </motion.div>
                ))}
                {mounted && logs.length < bootSequence.length && (
                    <div className="flex gap-4">
                        <span className="text-slate-600 font-mono">[{new Date().toLocaleTimeString([], { hour12: false })}]</span>
                        <div className="w-2 h-4 bg-blue-500 animate-pulse"></div>
                    </div>
                )}
            </div>
        </div>
      </div>

      <div className="mt-10 text-center">
         <p className="text-slate-600 text-[10px] font-bold uppercase tracking-[0.3em]">Authorized Institutional Access Only</p>
         <div className="flex gap-4 mt-4 justify-center grayscale opacity-30">
            <div className="w-8 h-8 rounded bg-white text-black flex items-center justify-center font-black italic text-xs">MW</div>
            <div className="w-8 h-8 rounded bg-blue-600 flex items-center justify-center">
                <div className="w-4 h-4 border-2 border-white rounded-full border-t-transparent animate-spin"></div>
            </div>
         </div>
      </div>
    </div>
  );
}
