import { useQuery } from '@tanstack/react-query';
import { signalApi, marketApi } from '@/lib/api';
import { useAuthStore } from '@/store/useAuthStore';
import { useMarketStore } from '@/store/useMarketStore';
import { useEffect, useState } from 'react';
import { useReverb } from '@/hooks/useReverb';
import type { AnalyzeResponse } from '@/types/market';

export const useDashboardData = (symbol = 'R_75', timeframe = '15m') => {
  const { user } = useAuthStore();
  const { setHistoricalCandles } = useMarketStore();
  const { client } = useReverb();

  // Live analysis from Pusher — no HTTP polling
  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null);

  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onAnalysis = (data: AnalyzeResponse) => {
      if (data?.symbol) setAnalysis(data);
    };
    channel.bind('analysis', onAnalysis);
    return () => { channel.unbind('analysis', onAnalysis); };
  }, [client]);

  // Fetch Live Signals (initial seed; live updates come via Pusher in components)
  const signalsQuery = useQuery({
    queryKey: ['signals', 'live'],
    queryFn: () => signalApi.live(),
  });

  // Fetch Strategy Performance
  const performanceQuery = useQuery({
    queryKey: ['strategies', 'performance'],
    queryFn: () => signalApi.performance(),
  });

  // Fetch Market Candles (initial seed only)
  const candlesQuery = useQuery({
    queryKey: ['market', 'candles', symbol, timeframe],
    queryFn: () => marketApi.candles(symbol, timeframe),
    staleTime: 60000,
  });

  // Update market store when candles arrive
  useEffect(() => {
    if (candlesQuery.data?.data?.candles) {
      setHistoricalCandles(candlesQuery.data.data.candles);
    }
  }, [candlesQuery.data, setHistoricalCandles]);

  return {
    signals: signalsQuery.data?.data?.signals || [],
    analysis,
    performance: performanceQuery.data?.data?.strategies || [],
    candles: candlesQuery.data?.data?.candles || [],
    isLoading: signalsQuery.isLoading || candlesQuery.isLoading,
    isError: signalsQuery.isError || candlesQuery.isError,
  };
};
