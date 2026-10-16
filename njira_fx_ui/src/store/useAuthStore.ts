import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { AuthUser } from '@/types/auth';

// 'token' is the legacy access-token key — kept for backward compatibility
// with the rest of the app (api.ts request interceptor reads it directly).
const ACCESS_TOKEN_KEY = 'token';
const REFRESH_TOKEN_KEY = 'refresh_token';

interface AuthState {
  user: AuthUser | null;
  token: string | null;
  refreshToken: string | null;
  isAuthenticated: boolean;
  /** True once zustand-persist has read localStorage — gates auth redirects. */
  _hasHydrated: boolean;
  setHasHydrated: (v: boolean) => void;
  setAuth: (user: AuthUser, token: string, refreshToken: string) => void;
  /** Update tokens only (used by the api.ts refresh interceptor). */
  setTokens: (accessToken: string, refreshToken?: string) => void;
  clearAuth: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      token: null,
      refreshToken: null,
      isAuthenticated: false,
      _hasHydrated: false,
      setHasHydrated: (v) => set({ _hasHydrated: v }),
      setAuth: (user, token, refreshToken) => {
        if (typeof window !== 'undefined') {
          localStorage.setItem(ACCESS_TOKEN_KEY, token);
          localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
        }
        set({ user, token, refreshToken, isAuthenticated: true });
      },
      setTokens: (accessToken, refreshToken) => {
        if (typeof window !== 'undefined') {
          localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
          if (refreshToken) {
            localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
          }
        }
        set((state) => ({
          token: accessToken,
          refreshToken: refreshToken ?? state.refreshToken,
          isAuthenticated: true,
        }));
      },
      clearAuth: () => {
        if (typeof window !== 'undefined') {
          localStorage.removeItem(ACCESS_TOKEN_KEY);
          localStorage.removeItem(REFRESH_TOKEN_KEY);
        }
        set({ user: null, token: null, refreshToken: null, isAuthenticated: false });
      },
    }),
    {
      name: 'njira-auth-storage',
      // Backfill refreshToken from the legacy localStorage key for sessions
      // created before refresh-token persistence was introduced.
      onRehydrateStorage: () => (state) => {
        // Flag hydration FIRST — every auth redirect in the app gates on it.
        state?.setHasHydrated(true);
        if (!state || state.refreshToken || typeof window === 'undefined') return;
        if (!state.token) return;
        const legacyRefresh = localStorage.getItem(REFRESH_TOKEN_KEY);
        if (legacyRefresh) {
          state.setTokens(state.token, legacyRefresh);
        }
      },
    }
  )
);