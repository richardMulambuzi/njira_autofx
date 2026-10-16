import axios, {
  AxiosError,
  AxiosResponse,
  InternalAxiosRequestConfig,
} from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import api, { authApi, tickApi, tradeApi } from '@/lib/api';
import { useAuthStore } from '@/store/useAuthStore';

// ── Mock adapter helpers ────────────────────────────────────────────────────
// The real axios adapters reject with an AxiosError for non-2xx statuses, so
// the mock must do the same for the response interceptor's 401 path to run.

function makeResponse(
  config: InternalAxiosRequestConfig,
  status: number,
  data: unknown,
): AxiosResponse {
  return {
    data,
    status,
    statusText: status === 401 ? 'Unauthorized' : 'OK',
    headers: {},
    config,
  };
}

function makeError(
  config: InternalAxiosRequestConfig,
  status: number,
  data: unknown,
): AxiosError {
  return new AxiosError(
    `Request failed with status code ${status}`,
    status === 401 ? 'ERR_BAD_REQUEST' : undefined,
    config,
    undefined,
    makeResponse(config, status, data),
  );
}

const USER = { id: 1, name: 'Test User', email: 'test@example.com', status: 'active' };

describe('api.ts interceptor', () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.getState().clearAuth();
  });

  it('adds Authorization Bearer from localStorage on non-auth requests', async () => {
    localStorage.setItem('token', 'access-123');

    let captured: InternalAxiosRequestConfig | undefined;
    api.defaults.adapter = async (config) => {
      captured = config;
      return makeResponse(config, 200, USER);
    };

    await api.get('/auth/me/');
    expect(captured?.headers.Authorization).toBe('Bearer access-123');
  });

  it('does NOT attach Authorization to auth endpoints (login/register/refresh)', async () => {
    // A stale/expired token must never ride along on auth requests — the
    // backend would reject the login with 401 before checking credentials.
    localStorage.setItem('token', 'stale-access');

    const captured: Record<string, InternalAxiosRequestConfig> = {};
    api.defaults.adapter = async (config) => {
      captured[config.url ?? ''] = config;
      return makeResponse(config, 200, {});
    };

    await authApi.login({ email: 'test@example.com', password: 'pw' });
    await authApi.register({ name: 'Test', email: 'test@example.com', password: 'pw' });
    await authApi.refresh('refresh-1');

    expect(captured['/auth/login/']?.headers.Authorization).toBeUndefined();
    expect(captured['/auth/register/']?.headers.Authorization).toBeUndefined();
    expect(captured['/auth/refresh/']?.headers.Authorization).toBeUndefined();
  });

  it('401 triggers refresh then retries the original request with the new token', async () => {
    localStorage.setItem('token', 'old-access');
    localStorage.setItem('refresh_token', 'refresh-1');

    let meCalls = 0;
    let refreshCalls = 0;
    api.defaults.adapter = async (config) => {
      const url = config.url ?? '';
      if (url.includes('/auth/refresh/')) {
        refreshCalls += 1;
        return makeResponse(config, 200, {
          access_token: 'new-access',
          refresh_token: 'new-refresh',
        });
      }
      if (url.includes('/auth/me/')) {
        meCalls += 1;
        if (meCalls === 1) {
          throw makeError(config, 401, { detail: 'Invalid or expired token.' });
        }
        return makeResponse(config, 200, USER);
      }
      return makeResponse(config, 200, {});
    };

    const resp = await api.get('/auth/me/');

    expect(resp.status).toBe(200);
    expect(meCalls).toBe(2); // original 401 + retried request
    expect(refreshCalls).toBe(1); // exactly one refresh
    // Rotated tokens persisted (backend blacklists the old refresh token)
    expect(localStorage.getItem('token')).toBe('new-access');
    expect(localStorage.getItem('refresh_token')).toBe('new-refresh');
  });

  it('single-flights concurrent 401s into one refresh call', async () => {
    localStorage.setItem('token', 'old-access');
    localStorage.setItem('refresh_token', 'refresh-1');

    let refreshCalls = 0;
    let meCalls = 0;
    api.defaults.adapter = async (config) => {
      const url = config.url ?? '';
      if (url.includes('/auth/refresh/')) {
        refreshCalls += 1;
        await new Promise((resolve) => setTimeout(resolve, 20));
        return makeResponse(config, 200, {
          access_token: 'new-access',
          refresh_token: 'new-refresh',
        });
      }
      if (url.includes('/auth/me/')) {
        meCalls += 1;
        // First two calls (the concurrent originals) 401; retries succeed.
        if (meCalls <= 2) {
          throw makeError(config, 401, { detail: 'Invalid or expired token.' });
        }
        return makeResponse(config, 200, USER);
      }
      return makeResponse(config, 200, {});
    };

    const [r1, r2] = await Promise.all([api.get('/auth/me/'), api.get('/auth/me/')]);

    expect(refreshCalls).toBe(1); // shared refresh promise
    expect(r1.status).toBe(200);
    expect(r2.status).toBe(200);
  });

  it('does NOT trigger refresh on auth endpoint 401s (login/register/refresh)', async () => {
    localStorage.setItem('token', 'old-access');
    localStorage.setItem('refresh_token', 'refresh-1');

    let refreshCalls = 0;
    api.defaults.adapter = async (config) => {
      const url = config.url ?? '';
      if (url.includes('/auth/refresh/')) {
        refreshCalls += 1;
        return makeResponse(config, 200, { access_token: 'x', refresh_token: 'y' });
      }
      throw makeError(config, 401, { detail: 'Invalid email or password.' });
    };

    await expect(
      authApi.login({ email: 'test@example.com', password: 'wrong' }),
    ).rejects.toThrow();
    expect(refreshCalls).toBe(0);
  });

  it('clears auth when the refresh itself fails', async () => {
    localStorage.setItem('token', 'old-access');
    localStorage.setItem('refresh_token', 'expired-refresh');
    useAuthStore.getState().setAuth(USER, 'old-access', 'expired-refresh');

    api.defaults.adapter = async (config) => {
      const url = config.url ?? '';
      if (url.includes('/auth/refresh/')) {
        throw makeError(config, 401, { detail: 'Invalid or expired refresh token.' });
      }
      throw makeError(config, 401, { detail: 'Invalid or expired token.' });
    };

    await expect(api.get('/auth/me/')).rejects.toThrow();

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('refresh_token')).toBeNull();
  });
});

describe('tickApi / tradeApi (Pepperstone tick pipeline)', () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.getState().clearAuth();
  });

  it('chartData builds the correct URL and query params', async () => {
    let captured: InternalAxiosRequestConfig | undefined;
    api.defaults.adapter = async (config) => {
      captured = config;
      return makeResponse(config, 200, {
        status: 'ok',
        symbol: 'EURUSD',
        count: 2,
        data: [
          { bid: 1.0842, ask: 1.0844, spread: 2, time: '2026-08-17T10:00:00Z' },
          { bid: 1.0841, ask: 1.0843, spread: 2, time: '2026-08-17T10:00:01Z' },
        ],
      });
    };

    const resp = await tickApi.chartData('EURUSD', 500);

    expect(captured?.url).toBe('/api/chart-data/');
    expect(captured?.params).toEqual({ symbol: 'EURUSD', limit: 500 });
    expect(resp.data.symbol).toBe('EURUSD');
    expect(resp.data.data).toHaveLength(2);
  });

  it('mtfStatus sends the symbol param to the trailing-slash endpoint', async () => {
    let captured: InternalAxiosRequestConfig | undefined;
    api.defaults.adapter = async (config) => {
      captured = config;
      return makeResponse(config, 200, {
        status: 'ok',
        symbol: 'EURUSD',
        masterSignal: 'BUY',
        timeframes: {},
      });
    };

    await tickApi.mtfStatus('EURUSD');

    expect(captured?.url).toBe('/api/mtf-status/');
    expect(captured?.params).toEqual({ symbol: 'EURUSD' });
  });

  it('tradeApi.execute POSTs the request body to /api/trade/', async () => {
    let captured: InternalAxiosRequestConfig | undefined;
    api.defaults.adapter = async (config) => {
      captured = config;
      return makeResponse(config, 200, { status: 'ok', position_id: 'pos-1' });
    };

    const resp = await tradeApi.execute({ symbol: 'EURUSD', side: 'BUY', volume: 0.1 });

    expect(captured?.url).toBe('/api/trade/');
    expect(captured?.method).toBe('post');
    // axios transformRequest JSON-stringifies the body before the adapter runs
    expect(JSON.parse(captured?.data as string)).toEqual({
      symbol: 'EURUSD',
      side: 'BUY',
      volume: 0.1,
    });
    expect(resp.data.position_id).toBe('pos-1');
  });

  it('tickApi.ingest POSTs the raw tick payload to /api/ticks/', async () => {
    let captured: InternalAxiosRequestConfig | undefined;
    api.defaults.adapter = async (config) => {
      captured = config;
      return makeResponse(config, 201, { status: 'success' });
    };

    const payload = {
      symbol: 'EURUSD',
      bid: 1.105,
      ask: 1.1052,
      spread: 2,
      digits: 5,
      timestamp: '1750000000000',
    };
    const resp = await tickApi.ingest(payload);

    expect(captured?.url).toBe('/api/ticks/');
    expect(captured?.method).toBe('post');
    expect(JSON.parse(captured?.data as string)).toEqual(payload);
    expect(resp.status).toBe(201);
  });

  it('tickApi.chartData omits limit when not provided', async () => {
    let captured: InternalAxiosRequestConfig | undefined;
    api.defaults.adapter = async (config) => {
      captured = config;
      return makeResponse(config, 200, {
        status: 'ok',
        symbol: 'EURUSD',
        count: 0,
        data: [],
      });
    };

    await tickApi.chartData('EURUSD');

    expect(captured?.url).toBe('/api/chart-data/');
    expect(captured?.params).toEqual({ symbol: 'EURUSD', limit: undefined });
  });

  it('tradeApi.list and tradeApi.close hit the trades endpoints', async () => {
    const urls: string[] = [];
    api.defaults.adapter = async (config) => {
      urls.push(`${config.method?.toUpperCase()} ${config.url}`);
      return makeResponse(config, 200, {});
    };

    await tradeApi.list();
    await tradeApi.close(42);

    expect(urls).toEqual(['GET /trades', 'POST /trades/42/close']);
  });
});

describe('resolveApiBase — HTTPS enforcement (SECURITY HIGH #8)', () => {
  // The API base is resolved at module load, so each case re-imports a fresh
  // module with the env vars under test. `api.defaults.baseURL` exposes the
  // resolved value exactly as the axios instance uses it.
  const ENV_KEYS = ['NEXT_PUBLIC_API_URL', 'NEXT_PUBLIC_API_BASE_URL'];

  afterEach(() => {
    vi.resetModules();
    vi.unstubAllEnvs();
  });

  async function resolveBase(env: Record<string, string>): Promise<string> {
    vi.resetModules();
    vi.unstubAllEnvs();
    Object.entries(env).forEach(([key, value]) => vi.stubEnv(key, value));
    const fresh = await import('@/lib/api');
    return fresh.default.defaults.baseURL as string;
  }

  it('defaults to an https:// base when no env vars are set', async () => {
    expect(await resolveBase({})).toBe('https://indemw.tech:8101');
  });

  it('upgrades a non-localhost http:// origin to https:// outside production', async () => {
    expect(
      await resolveBase({ NEXT_PUBLIC_API_URL: 'http://indemw.tech:8101' }),
    ).toBe('https://indemw.tech:8101');
  });

  it('throws a clear error in production when the origin is still plaintext http', async () => {
    vi.resetModules();
    vi.unstubAllEnvs();
    vi.stubEnv('NEXT_PUBLIC_API_URL', 'http://indemw.tech:8101');
    vi.stubEnv('NODE_ENV', 'production');

    await expect(import('@/lib/api')).rejects.toThrow(
      /Refusing insecure API base URL "http:\/\/indemw\.tech:8101" in production/,
    );
  });

  it('keeps http:// for localhost dev origins', async () => {
    expect(
      await resolveBase({
        NEXT_PUBLIC_API_URL: 'http://localhost:8101',
        NODE_ENV: 'production',
      }),
    ).toBe('http://localhost:8101');
  });

  it('keeps http:// for 127.0.0.1 dev origins', async () => {
    expect(
      await resolveBase({ NEXT_PUBLIC_API_URL: 'http://127.0.0.1:8101' }),
    ).toBe('http://127.0.0.1:8101');
  });

  it('keeps http:// for [::1] dev origins', async () => {
    expect(
      await resolveBase({ NEXT_PUBLIC_API_URL: 'http://[::1]:8101' }),
    ).toBe('http://[::1]:8101');
  });

  it('passes https:// through untouched (including the secure default)', async () => {
    expect(
      await resolveBase({ NEXT_PUBLIC_API_URL: 'https://indemw.tech:8101' }),
    ).toBe('https://indemw.tech:8101');
  });

  it('strips a legacy /api/vN suffix from the base URL', async () => {
    expect(
      await resolveBase({ NEXT_PUBLIC_API_URL: 'https://indemw.tech:8101/api/v1' }),
    ).toBe('https://indemw.tech:8101');
  });

  it('strips a legacy /api suffix from the base URL', async () => {
    expect(
      await resolveBase({ NEXT_PUBLIC_API_URL: 'https://indemw.tech:8101/api' }),
    ).toBe('https://indemw.tech:8101');
  });

  it('strips trailing slashes from the base URL', async () => {
    expect(
      await resolveBase({ NEXT_PUBLIC_API_URL: 'https://indemw.tech:8101///' }),
    ).toBe('https://indemw.tech:8101');
  });
});