'use client';

import React from 'react';
import TerminalLayout from '@/components/layout/TerminalLayout';
import { Shield } from 'lucide-react';

export default function SystemPage() {
  return (
    <TerminalLayout>
      <div className="flex flex-col items-center justify-center h-[70vh] text-slate-600">
        <Shield size={48} className="opacity-20 mb-4 animate-pulse" />
        <h2 className="text-sm font-black uppercase tracking-[0.3em]">Infrastructure Control</h2>
        <p className="text-[10px] mt-2 opacity-50 uppercase tracking-widest">Verifying Neural Node Integrity...</p>
      </div>
    </TerminalLayout>
  );
}
