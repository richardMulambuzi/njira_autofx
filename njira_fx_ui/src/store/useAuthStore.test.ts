import { beforeEach, describe, expect, it } from 'vitest';
import { useAuthStore } from '@/store/useAuthStore';

const USER = { id: 1, name: 'Test User', email: 'test@example.com', status: 'active' };

describe('useAuthStore', () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.getState().clearAuth();
  });

  it('setAuth persists token + refresh_token to localStorage', () => {
    useAuthStore.getState().setAuth(USER, 'access-1', 'refresh-1');

    expect(localStorage.getItem('token')).toBe('access-1');
    expect(localStorage.getItem('refresh_token')).toBe('refresh-1');

    const state = useAuthStore.getState();
    expect(state.user).toEqual(USER);
    expect(state.token).toBe('access-1');
    expect(state.refreshToken).toBe('refresh-1');
    expect(state.isAuthenticated).toBe(true);
  });

  it('clearAuth removes both tokens from localStorage and state', () => {
    useAuthStore.getState().setAuth(USER, 'access-1', 'refresh-1');
    useAuthStore.getState().clearAuth();

    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('refresh_token')).toBeNull();

    const state = useAuthStore.getState();
    expect(state.user).toBeNull();
    expect(state.token).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(state.isAuthenticated).toBe(false);
  });

  it('setTokens updates both tokens when a new refresh token is provided', () => {
    useAuthStore.getState().setAuth(USER, 'access-1', 'refresh-1');
    useAuthStore.getState().setTokens('access-2', 'refresh-2');

    expect(localStorage.getItem('token')).toBe('access-2');
    expect(localStorage.getItem('refresh_token')).toBe('refresh-2');
    expect(useAuthStore.getState().token).toBe('access-2');
    expect(useAuthStore.getState().refreshToken).toBe('refresh-2');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  it('setTokens keeps the existing refresh token when omitted', () => {
    useAuthStore.getState().setAuth(USER, 'access-1', 'refresh-1');
    useAuthStore.getState().setTokens('access-2');

    expect(localStorage.getItem('token')).toBe('access-2');
    // refresh_token key untouched
    expect(localStorage.getItem('refresh_token')).toBe('refresh-1');
    expect(useAuthStore.getState().token).toBe('access-2');
    expect(useAuthStore.getState().refreshToken).toBe('refresh-1');
  });
});