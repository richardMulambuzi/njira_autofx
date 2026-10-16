'use client';

import React from 'react';
import TerminalLayout from '@/components/layout/TerminalLayout';
import BacktestDashboard from '@/components/backtest/BacktestDashboard';

export default function BacktestPage() {
  return (
    <TerminalLayout>
      <BacktestDashboard />
    </TerminalLayout>
  );
}
