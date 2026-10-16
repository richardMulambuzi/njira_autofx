'use client';

import React from 'react';
import TerminalLayout from '@/components/layout/TerminalLayout';
import MarketChart from '@/components/trading/MarketChart';

export default function MarketsPage() {
  return (
    <TerminalLayout>
      <div className="p-4">
        <MarketChart />
      </div>
    </TerminalLayout>
  );
}
