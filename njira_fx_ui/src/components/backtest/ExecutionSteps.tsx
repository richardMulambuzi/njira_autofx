'use client';

import React from 'react';

interface ExecutionStepsProps {
  steps: string[];
}

/** Vertical step timeline — renders the signal's execution trace ("where the
 *  signal was taken and why") as a numbered, connected sequence. */
export default function ExecutionSteps({ steps }: ExecutionStepsProps) {
  if (!steps || steps.length === 0) {
    return (
      <p className="text-[10px] text-slate-600 uppercase tracking-widest font-bold">
        No execution steps recorded
      </p>
    );
  }

  return (
    <ol className="relative border-l border-slate-700/60 pl-5 space-y-4">
      {steps.map((step, i) => (
        <li key={i} className="relative">
          <span className="absolute -left-[27px] top-0.5 w-4 h-4 rounded-none bg-[#0d131f] border border-amber-500/50 flex items-center justify-center">
            <span className="text-[8px] font-black text-amber-400 font-mono">{i + 1}</span>
          </span>
          <p className="text-[11px] leading-relaxed text-slate-300 font-mono">{step}</p>
        </li>
      ))}
    </ol>
  );
}
