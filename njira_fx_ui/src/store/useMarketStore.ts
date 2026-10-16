import { create } from 'zustand';

export interface CandleData {
  time: string | number; // TradingView format (Unix timestamp or 'YYYY-MM-DD')
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

interface MarketState {
  latestCandle: CandleData | null;
  historicalCandles: CandleData[];
  setHistoricalCandles: (candles: CandleData[]) => void;
  updateLatestCandle: (candle: CandleData) => void;
}

export const useMarketStore = create<MarketState>((set) => ({
  latestCandle: null,
  historicalCandles: [],
  setHistoricalCandles: (candles) => set({ historicalCandles: candles }),
  updateLatestCandle: (candle) => set({ latestCandle: candle }),
}));
