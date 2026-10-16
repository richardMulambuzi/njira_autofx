import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios';
import { useAuthStore } from '@/store/useAuthStore';
import type {
  AuthResponse,
  AuthUser,
  LoginCredentials,
  LogoutResponse,
  RefreshResponse,
  RegisterData,
} from '@/types/auth';
import type {
  ChartDataResponse,
  IngestTickPayload,
  MtfStatusResponse,
  ProviderAccountResponse,
  ProviderName,
  ProviderOrdersResponse,
  ProviderPositionsResponse,
  ProviderStatusResponse,
  ProviderSwitchResponse,
  ProviderTradeHistoryResponse,
  TradeRequest,
  TradeResponse,
} from '@/types/ticks';
import type {
  AnalyzeResponse,
  CandlesEnvelope,
  SignalsEnvelope,
  StrategiesPerformanceEnvelope,
  SymbolsEnvelope,
} from '@/types/market';

// ── Base URL resolution ────────────────────────────────────────────────
// Django serves at the ROOT (no /api prefix). Strip any legacy /api or
// /api/vN suffix so the client always talks to the bare origin.
//
// SECURITY (HIGH #8): JWT Bearer tokens ride on every request, so the API
// origin must NEVER be plaintext http:// outside local development.
// resolveApiBase() upgrades any non-localhost http:// origin to https://
// and throws a clear error in production builds if the config still points
// at http:// — a misconfiguration is caught at build time, not masked at
// runtime. Plaintext http:// is allowed ONLY for localhost/127.0.0.1/[::1].
const DEFAULT_API_BASE = 'https://indemw.tech:8101';

/** Hosts exempt from the HTTPS upgrade — local development only. */
const LOCALHOST_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

function resolveApiBase(): string {
  const raw =
    process.env.NEXT_PUBLIC_API_URL ||
    process.env.NEXT_PUBLIC_API_BASE_URL ||
    DEFAULT_API_BASE;

  const cleaned = raw
    .trim()
    .replace(/\/+$/, '')
    .replace(/\/api\/v\d+$/, '')
    .replace(/\/api$/, '');

  let url: URL;
  try {
    url = new URL(cleaned);
  } catch {
    if (process.env.NODE_ENV === 'production') {
      throw new Error(
        `[api] Invalid API base URL "${cleaned}" — expected an absolute http(s) URL.`,
      );
    }
    return cleaned;
  }

  // Plaintext http:// is tolerated ONLY for localhost dev servers. Any other
  // http:// origin is either upgraded to https:// or, in production builds,
  // rejected outright so the misconfiguration cannot ship.
  if (url.protocol === 'http:' && !LOCALHOST_HOSTS.has(url.hostname)) {
    if (process.env.NODE_ENV === 'production') {
      throw new Error(
        `[api] Refusing insecure API base URL "${cleaned}" in production. ` +
          'JWT Bearer tokens travel over this connection — the origin only ' +
          'exposes https:// (TLS termination is available at indemw.tech).',
      );
    }
    // Non-production (dev/test/staging): transparently upgrade instead of
    // letting tokens ride plaintext — defense in depth.
    url.protocol = 'https:';
    return url.toString().replace(/\/+$/, '');
  }

  return cleaned;
}

const API_BASE = resolveApiBase();

const api = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  },
});

// ── Token storage helpers (client-only) ────────────────────────────────
// 'token' is the legacy access-token key — kept for backward compatibility
// with the rest of the app. 'refresh_token' is the new refresh-token key.
const ACCESS_TOKEN_KEY = 'token';
const REFRESH_TOKEN_KEY = 'refresh_token';

function getAccessToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

function getRefreshToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

// ── Request interceptor: attach Bearer token ───────────────────────────
// Auth endpoints (login/register/refresh) must NEVER carry a stale Bearer
// header — a leftover/expired token makes the backend reject the request
// with 401 before credentials are even checked, permanently locking the
// user out. isAuthEndpoint is shared with the response interceptor so both
// agree on exactly which URLs are exempt.
api.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token && !isAuthEndpoint(config.url)) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ── Auth API ───────────────────────────────────────────────────────────
export const authApi = {
  register: (data: RegisterData) => api.post<AuthResponse>('/auth/register/', data),
  login: (credentials: LoginCredentials) => api.post<AuthResponse>('/auth/login/', credentials),
  logout: () =>
    api.post<LogoutResponse>('/auth/logout/', { refresh: getRefreshToken() }),
  me: () => api.get<AuthUser>('/auth/me/'),
  refresh: (refreshToken: string) =>
    api.post<RefreshResponse>('/auth/refresh/', { refresh: refreshToken }),
};

// ── Single-flight token refresh ────────────────────────────────────────
// Concurrent 401s share one refresh call to avoid a refresh stampede.
let refreshPromise: Promise<string> | null = null;

/** Silent session restore on boot: rotates the stored refresh JWT for a
 *  fresh access token. Exported for TerminalLayout's reload-restore flow. */
export async function refreshAccessToken(): Promise<string> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    throw new Error('No refresh token available');
  }

  const { data } = await authApi.refresh(refreshToken);

  // Persist the new access token AND the rotated refresh token — the
  // backend blacklists the previous refresh token on every refresh
  // (ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION), so dropping it
  // here would kill the session on the next 401.
  useAuthStore.getState().setTokens(data.access_token, data.refresh_token);

  return data.access_token;
}

/** Auth endpoints must never trigger the refresh flow (login failures are
 *  credential errors, and refresh must not recurse into itself). Shared by
 *  the request interceptor (skip stale Bearer header) and the response
 *  interceptor (skip refresh loop) — one source of truth. */
function isAuthEndpoint(url: string | undefined): boolean {
  if (!url) return false;
  // Trailing slash is required by Django (APPEND_SLASH); tolerate both forms.
  return /\/auth\/(login|register|refresh)\/?$/.test(url);
}

interface RetryableRequestConfig extends InternalAxiosRequestConfig {
  _retry?: boolean;
}

// ── Response interceptor: 401 → refresh → retry once ───────────────────
api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as RetryableRequestConfig | undefined;

    // Only attempt refresh for 401s on non-auth endpoints, once per request.
    if (
      error.response?.status !== 401 ||
      !original ||
      original._retry ||
      isAuthEndpoint(original.url)
    ) {
      return Promise.reject(error);
    }

    original._retry = true;

    try {
      // Single-flight: concurrent 401s await the same refresh promise.
      refreshPromise = refreshPromise ?? refreshAccessToken();
      const newToken = await refreshPromise;
      refreshPromise = null;

      original.headers.Authorization = `Bearer ${newToken}`;
      return api(original);
    } catch (refreshError) {
      refreshPromise = null;
      // Refresh failed — force logout and reject the original request.
      useAuthStore.getState().clearAuth();
      return Promise.reject(refreshError);
    }
  }
);

export const terminalApi = {
  // Signals Engine
  generate: (symbol: string) => api.post('/signals/generate', { symbol }),
  live: () => api.get<SignalsEnvelope>('/api/signals/live/'),
  history: (limit = 50) =>
    api.get<SignalsEnvelope>('/api/signals/history/', { params: { limit } }),
  get: (id: string | number) => api.get(`/signals/${id}`),

  // Market Analysis Engine
  analyze: (symbol: string) =>
    api.get<AnalyzeResponse>(`/api/market/analyze/${encodeURIComponent(symbol)}/`),
  structure: (symbol: string) => api.get(`/market/structure/${symbol}`),
  sentiment: (symbol: string) => api.get(`/market/sentiment/${symbol}`),
};

// ── Signal Engine ──────────────────────────────────────────────
export const signalApi = {
  /** Generate a confluence signal for a symbol/timeframe */
  generate: (symbol: string, timeframe: string) =>
    api.post('/signal/generate', { symbol, timeframe }),

  /** Evaluate individual strategy outputs without final signal */
  evaluate: (symbol: string, timeframe: string) =>
    api.post('/strategy/evaluate', { symbol, timeframe }),

  /** Fetch recent confirmed signals — v2 (new signal engine), backend falls back to legacy */
  live: () => api.get<SignalsEnvelope>('/api/signals/live-v2/'),

  /** Fetch historical confirmed signals */
  history: (limit = 50) =>
    api.get<SignalsEnvelope>('/api/signals/history/', { params: { limit } }),

  /** Fetch strategy performance metrics */
  performance: () =>
    api.get<StrategiesPerformanceEnvelope>('/api/strategies/performance/'),
};

// ── Market Data ──────────────────────────────────────────────
export const marketApi = {
  /** Fetch historical candles — tries v2 (new candle engine) first, backend falls back to old aggregation */
  candles: (symbol: string, timeframe: string, limit = 200) =>
    api.get<CandlesEnvelope>('/api/market/candles-v2/', {
      params: { symbol, timeframe, limit },
    }),
};

// ── Chart Drawings (persisted per symbol, replace-all semantics) ────────
export interface ChartDrawingShape {
  kind: "trend" | "rect";
  t1: number;
  p1: number;
  t2: number;
  p2: number;
}
export const drawingApi = {
  list: (symbol: string) =>
    api.get<{ symbol: string; drawings: ChartDrawingShape[] }>(
      '/api/chart-drawings/',
      { params: { symbol } }
    ),
  save: (symbol: string, shapes: ChartDrawingShape[]) =>
    api.post<{ symbol: string; saved: number }>(
      '/api/chart-drawings/',
      { symbol, shapes }
    ),
};

// ── Symbols ──────────────────────────────────────────────────
export const symbolsApi = {
  /** Tradable instrument catalogue (plain symbols + rich asset metadata) */
  all: () => api.get<SymbolsEnvelope>('/api/symbols/'),
};

// ── Pepperstone Tick Pipeline ──────────────────────────────
// Trailing slashes are required by Django (APPEND_SLASH) — matches the
// auth endpoints above. All calls ride the shared `api` instance so the
// Bearer interceptor and single-flight 401→refresh→retry apply.
export const tickApi = {
  /** Fetch historical tick bars for a symbol (chart rendering) */
  chartData: (symbol: string, limit?: number) =>
    api.get<ChartDataResponse>('/api/chart-data/', { params: { symbol, limit } }),

  /** Ingest a raw tick from the Pepperstone feed */
  ingest: (payload: IngestTickPayload) => api.post('/api/ticks/', payload),

  /** Fetch multi-timeframe confluence status for a symbol */
  mtfStatus: (symbol: string) =>
    api.get<MtfStatusResponse>('/api/mtf-status/', { params: { symbol } }),
};

// ── Trades ──────────────────────────────────────────────
export const tradeApi = {
  /** List open trades */
  list: () => api.get('/trades'),

  /** Close a trade */
  close: (id: number) => api.post(`/trades/${id}/close`),

  /** Execute a trade through the Pepperstone pipeline */
  execute: (req: TradeRequest) => api.post<TradeResponse>('/api/trade/', req),
};

// ── Trading Provider (broker abstraction) ─────────────────────────
// Provider-agnostic: the backend reports whichever broker is active
// (pepperstone | mock). Trailing slashes are required by Django
// (APPEND_SLASH). All calls ride the shared `api` instance so the Bearer
// interceptor and single-flight 401→refresh→retry apply.
export const providerApi = {
  /** Live provider health snapshot */
  status: () => api.get<ProviderStatusResponse>('/api/provider/status/'),

  /** Account identity + balance */
  account: () => api.get<ProviderAccountResponse>('/api/provider/account/'),

  /** Open positions mirrored from the broker */
  positions: () => api.get<ProviderPositionsResponse>('/api/provider/positions/'),

  /** Broker orders */
  orders: () => api.get<ProviderOrdersResponse>('/api/provider/orders/'),

  /** Completed trade history */
  tradeHistory: (limit = 50) =>
    api.get<ProviderTradeHistoryResponse>('/api/provider/trade-history/', {
      params: { limit },
    }),

  /** Tradable symbols offered by the provider */
  symbols: () => api.get('/api/provider/symbols/'),

  /** Historical candles for a symbol */
  candles: (symbol: string, timeframe: string, limit = 100) =>
    api.get('/api/provider/candles/', { params: { symbol, timeframe, limit } }),

  /** Latest tick for a symbol */
  marketData: (symbol: string) =>
    api.get('/api/provider/market-data/', { params: { symbol } }),

  /** Switch the active provider (staff only) */
  switchProvider: (provider: ProviderName) =>
    api.post<ProviderSwitchResponse>('/api/provider/switch/', { provider }),
};

// ── Backtest Engine ──────────────────────────────────────────────
// Admin backtest over historical CSV datasets. Trailing slashes are
// required by Django (APPEND_SLASH). Multipart upload rides the shared
// `api` instance so the Bearer interceptor + single-flight 401 refresh apply.
export interface StrategyBacktestResult {
  strategy: string;
  total_trades: number;
  wins: number;
  losses: number;
  expired: number;
  win_rate: number;
  profit_factor: number;
  total_pnl: number;
  avg_win: number;
  avg_loss: number;
  max_drawdown: number;
  expectancy: number;
  passes_70_gate: boolean;
}

export interface BacktestTrade {
  strategy: string;
  direction: 'BUY' | 'SELL';
  entry: number;
  stop_loss: number;
  take_profit: number;
  exit: number;
  exit_time: string;
  pnl: number;
  win: boolean;
  confidence: number;
  explanation: string;
  execution_steps: string[];
  strength: string;
  combined: boolean;
  source_strategies: string[];
  entry_time: string;
  outcome: string;
}

export interface BacktestResult {
  id: number;
  symbol: string;
  csv_name: string;
  initial_balance: number;
  final_balance: number;
  total_pnl: number;
  overall_win_rate: number;
  best_strategy: string;
  per_strategy: StrategyBacktestResult[];
  trades: BacktestTrade[];
  timeframes_used: Array<{ timeframe: number; source: 'uploaded' | 'aggregated' }>;
}

export interface BacktestSummary {
  id: number;
  symbol: string;
  csv_name: string;
  created_at: string;
  overall_win_rate: number;
  best_strategy: string;
  total_trades: number;
}

export const backtestApi = {
  /** Upload the base CSV plus any higher-timeframe CSVs and run the backtest
   *  ensemble. Each higher-TF file is appended under the same `higher_tf_files`
   *  key so `request.FILES.getlist('higher_tf_files')` on the backend reads
   *  them all. Axios sets the multipart Content-Type automatically when a
   *  FormData body is passed — do NOT set it manually. `strategies` is sent
   *  comma-joined per contract. */
  upload: (
    file: File,
    higherTfFiles: File[] = [],
    symbol?: string,
    strategies?: string[],
  ) => {
    const form = new FormData();
    form.append('file', file);
    for (const f of higherTfFiles) form.append('higher_tf_files', f);
    if (symbol) form.append('symbol', symbol);
    if (strategies && strategies.length > 0) {
      form.append('strategies', strategies.join(','));
    }
    return api.post<BacktestResult>('/api/backtest/upload/', form);
  },

  /** Stream a backtest run over Server-Sent Events.
   *
   *  The engine is CPU-bound and slow (a real M15 upload can take minutes),
   *  so we stream progress ticks as the replay runs instead of making the
   *  user stare at a spinner.  Each progress event carries
   *  ``{processed, total, percent, signals_so_far, symbol}`` — the UI derives
   *  an ETA from ``processed/total`` and elapsed time.  The final ``data:``
   *  event carries the full ``BacktestResult``; ``event: done`` signals
   *  completion.  Resolves with the result; rejects on HTTP error or a
   *  malformed stream. */
  uploadStream: (
    file: File,
    higherTfFiles: File[] = [],
    symbol?: string,
    strategies?: string[],
    onProgress?: (p: {
      processed: number;
      total: number;
      percent: number;
      signals_so_far: number;
      symbol: string;
    }) => void,
  ): Promise<BacktestResult> => {
    const form = new FormData();
    form.append('file', file);
    for (const f of higherTfFiles) form.append('higher_tf_files', f);
    if (symbol) form.append('symbol', symbol);
    if (strategies && strategies.length > 0) {
      form.append('strategies', strategies.join(','));
    }
    const token = getAccessToken();
    return fetch(`${API_BASE}/api/backtest/upload/stream/`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    }).then(async (resp) => {
      if (!resp.ok || !resp.body) {
        throw new Error(`Backtest stream failed (HTTP ${resp.status})`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let result: BacktestResult | null = null;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by a blank line; parse each complete block.
        let sep = buffer.indexOf('\n\n');
        while (sep !== -1) {
          const block = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const dataLine = block
            .split('\n')
            .find((l) => l.startsWith('data: '));
          if (dataLine) {
            try {
              const payload = JSON.parse(dataLine.slice(6));
              if (payload && typeof payload.percent === 'number') {
                onProgress?.(payload);
              } else if (payload && typeof payload.id === 'number') {
                result = payload as BacktestResult;
              }
            } catch {
              // Ignore malformed progress frames; the final result still lands.
            }
          }
          sep = buffer.indexOf('\n\n');
        }
      }
      if (!result) throw new Error('Backtest stream ended without a result');
      return result;
    });
  },

  /** List recent backtest runs (summary shape). */
  list: () => api.get<BacktestSummary[]>('/api/backtest/results/'),

  /** Fetch full detail for a single run (same shape as upload response). */
  detail: (id: number) =>
    api.get<BacktestResult>(`/api/backtest/results/${id}/`),
};

export default api;