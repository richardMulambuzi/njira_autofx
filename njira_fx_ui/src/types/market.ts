/**
 * Market / signals / strategy contract types — mirror the Django backend
 * FINAL contract exactly (snake_case wire format as returned by the API).
 *
 * GET /api/market/candles/?symbol=&timeframe=&limit= → CandlesEnvelope
 * GET /api/market/analyze/<symbol>/                  → AnalyzeResponse
 * GET /api/signals/live/                             → SignalsEnvelope
 * GET /api/signals/history/?limit=                   → SignalsEnvelope
 * GET /api/strategies/performance/                   → StrategiesPerformanceEnvelope
 * GET /api/symbols/                                  → SymbolsEnvelope
 */

/** A single OHLC candle as returned by /api/market/candles/. */
export interface Candle {
  time: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
}

/** GET /api/market/candles/ — `candles` is empty when no data exists. */
export interface CandlesEnvelope {
  symbol: string;
  timeframe: string;
  candles: Candle[];
}

/** GET /api/market/analyze/<symbol>/ — moving-average trend snapshot. */
export interface AnalyzeResponse {
  symbol: string;
  last_close: number;
  sma20: number;
  sma50: number;
  trend: string;
  generated_at: string;
  /** Future fields — dashboard reads these when backend enriches the response. */
  structure?: { bos?: boolean; choch?: boolean; order_block_active?: boolean };
  regime?: string;
  volatility?: string;
  sentiment?: { score?: number; bias?: string; news_drivers?: string[] };
  session?: {
    current_session: string;
    is_overlap: boolean;
    price_position: number;
    session_high: number;
    session_low: number;
  };
}

/**
 * A confirmed signal item. Only the envelope (`{ signals: [...] }`) is FINAL
 * contract today; the optional fields below describe what the existing UI
 * already renders, and the index signature carries backend-defined extras
 * (final_score, strategy_breakdown, market_context, …) without fabricating
 * their shapes.
 */
export interface TradingSignal {
  id?: number | string;
  symbol?: string;
  direction?: string;
  confidence?: number;
  created_at?: string;
  timestamp?: string;
  entry?: number | string;
  stop_loss?: number | string;
  take_profit?: number | string;
  explanation?: string[];
  [field: string]: unknown;
}

/** Envelope shared by GET /api/signals/live/ and GET /api/signals/history/. */
export interface SignalsEnvelope {
  signals: TradingSignal[];
}

/** Per-strategy win/loss metrics from /api/strategies/performance/. */
export interface StrategyPerformance {
  strategy: string;
  signals: number;
  wins: number;
  losses: number;
}

/** GET /api/strategies/performance/ */
export interface StrategiesPerformanceEnvelope {
  strategies: StrategyPerformance[];
}

/** Metadata descriptor for a tradable asset from /api/symbols/. */
export interface SymbolAsset {
  symbol: string;
  /** Backend-defined descriptors — numeric shape not finalized upstream yet. */
  spread?: unknown;
  volatility?: unknown;
  best_time?: string;
  name?: string;
  description?: string;
}

/** GET /api/symbols/ — tradable instrument catalogue. */
export interface SymbolsEnvelope {
  symbols: string[];
  assets: SymbolAsset[];
}

// ── Intelligence event types (Phase 5 — Pusher "njira-market" channel) ─

/** Structure event broadcast on "structure" — swing breaks, BOS, CHoCH, MSS. */
export interface StructureEvent {
  type: "structure";
  event: string; // "BOS_BULLISH" | "BOS_BEARISH" | "CHOCH_BULLISH" | etc.
  timeframe: number;
  price: number;
  ref_price: number;
}

/** Liquidity event broadcast on "liquidity" — pool detection and sweeps. */
export interface LiquidityEvent {
  type: "liquidity";
  event: string; // "POOL_DETECTED" | "SWEEP_DETECTED"
  pool_type: string; // "EQUAL_HIGH" | "SWING_LOW" | etc.
  price: number;
  touches?: number;
  confirmation?: string;
}

/** Order block event broadcast on "order_block" — candidate detection and lifecycle. */
export interface OrderBlockEvent {
  type: "order_block";
  event: string; // "CANDIDATE_DETECTED" | "VALID" | "MITIGATED" | etc.
  direction: string; // "BULLISH_OB" | "BEARISH_OB"
  state: string;
  high: number;
  low: number;
  quality_score: number;
  timeframe: number;
}

/** Signal event broadcast on "signal" — confirmed trading signals. */
export interface SignalEvent {
  signal_id: string;
  symbol: string;
  direction: string; // "BUY" | "SELL"
  state: string;
  created_at: string;
  entry: number;
  stop_loss: number;
  take_profit: number;
  entry_zone_low: number;
  entry_zone_high: number;
  confidence_score: number;
  confidence_evidence: Record<string, number>;
  explanation: string;
  strategy: string;
  timeframe: string;
  market_regime: string;
  risk_reward_ratio: number;
}

/** Candle event broadcast on "candle" — closed candle data. */
export interface CandleEvent {
  symbol: string;
  timeframe: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume_ticks: number;
  open_time: string;
  close_time: string | null;
  is_closed: boolean;
  gap_filled: boolean;
}

/** FVG event broadcast on "fvg" — Fair Value Gap detection. */
export interface FVGEvent {
  type: "fvg";
  event: string;
  direction: string;
  status: string;
  upper_boundary: number;
  lower_boundary: number;
  filled_percentage: number;
  timeframe: number;
}

/** Volume profile broadcast on "volume_profile" — developing profile state. */
export interface VolumeProfileEvent {
  symbol: string;
  methodology: string;
  window_start: string | null;
  window_end: string | null;
  total_activity: number;
  poc: number;
  poc_activity: number;
  vah: number;
  val: number;
  profile_high: number;
  profile_low: number;
  hvn_levels: number[];
  lvn_levels: number[];
  developing: boolean;
  data_complete: boolean;
  calculated_at: string | null;
  shape?: string;
  entropy?: number;
  reliability?: number;
  value_migration?: string;
  bins: Record<string, {
    price: number;
    activity: number;
    bid_count: number;
    ask_count: number;
    delta: number;
    relative_activity: number;
    percentage: number;
  }>;
}

/** Live tick event broadcast on "tick" — throttled price update. */
export interface TickEvent {
  symbol: string;
  bid: number;
  ask: number;
  spread: number;
  digits: number;
  timestamp_ms: number;
}

/** Provider health status broadcast on "provider_status". */
export interface ProviderStatusEvent {
  provider: string;
  environment: string;
  health: Record<string, unknown>;
  credentials_valid: boolean;
  safety_warnings: string[];
}

/** Multi-timeframe alignment broadcast on "mtf_status". */
export interface MtfStatusEvent {
  status: string;
  symbol: string;
  masterSignal: string;
  timeframes: Record<string, unknown>;
}

// ── Analysis snapshot (Pusher "njira-market" channel, "analysis" event) ─

/** A single strategy's assessment within an analysis snapshot. */
export interface StrategyReport {
  name: string;
  active: boolean;
  applicable: boolean;
  direction: string; // "BULLISH" | "BEARISH" | "NEUTRAL"
  preconditions: string[];
  confirmations: string[];
  missing_confirmations: string[];
  invalidations: string[];
  confidence: number;
  candidate_setup: unknown | null;
  reason: string;
}

/** Full "analysis" snapshot broadcast on "njira-market" channel, "analysis" event. */
export interface AnalysisSnapshot {
  symbol: string;
  structure: { bos: boolean; choch: boolean; order_block_active: boolean };
  regime: string;
  volatility: string;
  sma20: number;
  sma50: number;
  trend: string;
  last_close: number;
  session: Record<string, unknown>;
  current_price: number | null;
  day_high: number | null;
  day_low: number | null;
  data_freshness: number | null;
  strategy_agreement: { bullish: number; bearish: number; neutral: number; total: number };
  strategy_reports: StrategyReport[];
  invalidation: string[];
}
