/**
 * Pepperstone tick pipeline contract types — mirror the Django backend
 * contract exactly. Field names are the wire format (snake_case) as
 * returned by the API.
 *
 * POST /api/ticks/       {symbol, bid, ask, spread, digits, timestamp?} → {status}
 * GET  /api/chart-data/  ?symbol=EURUSD&limit=500 → ChartDataResponse
 * GET  /api/mtf-status/  ?symbol=EURUSD → MtfStatusResponse
 * POST /api/trade/       {symbol, side, volume} → TradeResponse
 */

/** A single tick bar as returned by the chart-data endpoint. */
export interface Tick {
  bid: number;
  ask: number;
  spread: number;
  time: string;
}

/** GET /api/chart-data/ — historical tick bars for a symbol. */
export interface ChartDataResponse {
  status: string;
  symbol: string;
  count: number;
  data: Tick[];
}

/** Confluence state for a single timeframe in the MTF status response. */
export interface TimeframeInfo {
  aligned: boolean;
  current_value: number;
  chart_history: { time: string; close: number }[];
}

/** GET /api/mtf-status/ — multi-timeframe confluence snapshot. */
export interface MtfStatusResponse {
  status: string;
  symbol: string;
  masterSignal: 'BUY' | 'SELL' | 'HOLD';
  timeframes: Record<string, TimeframeInfo>;
}

/** POST /api/trade/ — request body for executing a trade. */
export interface TradeRequest {
  symbol: string;
  side: 'BUY' | 'SELL';
  volume: number;
}

/** POST /api/trade/ — response; risk-blocked trades carry `reason`. */
export interface TradeResponse {
  status: string;
  position_id?: string;
  reason?: string;
  message?: string;
}

/** POST /api/ticks/ — raw tick payload from the Pepperstone feed. */
export interface IngestTickPayload {
  symbol: string;
  bid: number;
  ask: number;
  spread: number;
  digits: number;
  timestamp?: string;
}

/* ── Trading Provider (broker abstraction) contract ──────────────────
 * Mirrors the Django provider app exactly (snake_case wire format).
 *
 * GET  /api/provider/status/         → ProviderStatusResponse
 * GET  /api/provider/account/        → ProviderAccountResponse
 * GET  /api/provider/positions/      → ProviderPositionsResponse
 * GET  /api/provider/orders/         → ProviderOrdersResponse
 * GET  /api/provider/trade-history/  → ProviderTradeHistoryResponse
 * POST /api/provider/switch/         {provider} → ProviderSwitchResponse
 *
 * The frontend is provider-agnostic: it only renders whatever provider
 * the backend reports (pepperstone | mock).
 */

export type ProviderName = 'pepperstone' | 'mock';
export type TradingEnvironment = 'demo' | 'paper' | 'live';

/** Live health snapshot of the active broker connection. */
export interface ProviderHealth {
  provider: string;
  environment: string;
  authentication_status: string;
  connection_status: string;
  api_available: boolean;
  last_successful_request: string | null;
  last_market_data_update: string | null;
  account_sync_status: string;
  last_error: string | null;
}

/** Configuration summary for a single configured provider. */
export interface ProviderConfigSummary {
  name: string;
  enabled: boolean;
  mode: string;
  credentials_valid: boolean;
  validation_errors: string[];
  requires: string[];
}

/** GET /api/provider/status/ — provider health + safety snapshot. */
export interface ProviderStatusResponse {
  status: string;
  data: {
    provider: string;
    environment: string;
    health: ProviderHealth;
    config: Record<string, ProviderConfigSummary> & {
      active_provider: string;
      mode: string;
    };
    safety_warnings: string[];
    credentials_valid: boolean;
  };
}

/** Broker account identity as reported by the provider. */
export interface ProviderAccount {
  provider: string;
  account_id: string;
  account_type: string;
  currency: string;
  status: string;
  name: string;
  leverage: number;
}

/** Broker account balance/equity snapshot. */
export interface ProviderBalance {
  provider: string;
  account_id: string;
  balance: number;
  equity: number;
  margin: number;
  free_margin: number;
  currency: string;
  timestamp: string;
}

/** GET /api/provider/account/ — account identity + balance. */
export interface ProviderAccountResponse {
  status: string;
  data: { account: ProviderAccount; balance: ProviderBalance };
}

/** An open/closed position mirrored from the broker. */
export interface BrokerPosition {
  provider: string;
  external_position_id: string;
  account_id: string;
  symbol: string;
  side: 'buy' | 'sell';
  quantity: number;
  entry_price: number;
  current_price: number;
  status: string;
  opened_at: string;
  closed_at: string | null;
  realized_pnl: number;
  stop_loss: number | null;
  take_profit: number | null;
}

/** A broker order (working, filled, or cancelled). */
export interface BrokerOrder {
  provider: string;
  external_order_id: string;
  account_id: string;
  symbol: string;
  side: 'buy' | 'sell';
  order_type: string;
  quantity: number;
  filled_quantity: number;
  price: number | null;
  status: string;
  created_at: string;
  updated_at: string;
  stop_loss: number | null;
  take_profit: number | null;
}

/** A completed trade in the broker trade history. */
export interface BrokerTrade {
  provider: string;
  external_trade_id: string;
  account_id: string;
  symbol: string;
  side: 'buy' | 'sell';
  quantity: number;
  entry_price: number;
  exit_price: number | null;
  realized_pnl: number;
  status: string;
  opened_at: string;
  closed_at: string | null;
}

/** GET /api/provider/positions/ */
export interface ProviderPositionsResponse {
  status: string;
  data: { positions: BrokerPosition[] };
}

/** GET /api/provider/orders/ */
export interface ProviderOrdersResponse {
  status: string;
  data: { orders: BrokerOrder[] };
}

/** GET /api/provider/trade-history/ */
export interface ProviderTradeHistoryResponse {
  status: string;
  data: { trades: BrokerTrade[] };
}

/** POST /api/provider/switch/ — request body (staff only). */
export interface ProviderSwitchRequest {
  provider: ProviderName;
}

/** POST /api/provider/switch/ — response. */
export interface ProviderSwitchResponse {
  status: string;
  data: { provider: string; health: ProviderHealth };
}