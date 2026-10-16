'use client';

import React from 'react';
import TerminalLayout from '@/components/layout/TerminalLayout';
import { Zap } from 'lucide-react';

export default function StrategiesPage() {
  return (
    <TerminalLayout>
      <div className="flex flex-col items-center justify-center h-[70vh] text-slate-600">
        <Zap size={48} className="opacity-20 mb-4 animate-pulse" />
        <h2 className="text-sm font-black uppercase tracking-[0.3em]">Neural Strategy Brain</h2>
        <p className="text-[10px] mt-2 opacity-50 uppercase tracking-widest">Optimizing Ensemble Weights...</p>
      </div>
    </TerminalLayout>
  );
}
