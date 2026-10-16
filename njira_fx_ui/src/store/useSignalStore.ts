import { create } from 'zustand';

export interface SignalData {
  id?: number | string;
  signal_id?: string;
  symbol?: string;
  timeframe?: string;
  final_score?: number;
  direction?: string;
  confidence?: number;
  confidence_score?: number;
  market_regime?: string;
  status?: string;
  state?: string;           // "CONFIRMED" | "WAITING" | "INVALIDATED" | "EXPIRED"
  strategy?: string;
  strength?: string;
  entry?: number | string;
  stop_loss?: number | string;
  take_profit?: number | string;
  entry_zone_low?: number;
  entry_zone_high?: number;
  risk_reward_ratio?: number;
  explanation?: string | string[];
  confidence_evidence?: {
    contradiction?: {
      severity: string;
      recommendation: string;
      reasons: string[];
      net_confidence: number;
      contradiction_count: number;
    };
    [key: string]: unknown;
  };
  breakdown?: Record<string, any>;
  created_at?: string;
  timestamp?: string;
}

interface SignalState {
  signals: SignalData[];
  latestSignal: SignalData | null;
  addSignal: (signal: SignalData) => void;
  setSignals: (signals: SignalData[]) => void;
}

export const useSignalStore = create<SignalState>((set) => ({
  signals: [],
  latestSignal: null,
  addSignal: (signal) =>
    set((state) => ({
      signals: [signal, ...state.signals].slice(0, 100), // Keep last 100
      latestSignal: signal,
    })),
  setSignals: (signals) =>
    set({
      signals,
      latestSignal: signals.length > 0 ? signals[0] : null,
    }),
}));
