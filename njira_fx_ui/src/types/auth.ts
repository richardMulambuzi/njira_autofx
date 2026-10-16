/**
 * Auth contract types — mirror the Django backend JWT contract exactly.
 * Field names are the wire format (snake_case) as returned by the API.
 *
 * POST /auth/login    {email, password}            → AuthResponse
 * POST /auth/register {name, email, password}      → AuthResponse
 * GET  /auth/me       (Bearer)                     → AuthUser
 * POST /auth/refresh  {refresh}                    → RefreshResponse
 * POST /auth/logout   {refresh} (Bearer)           → LogoutResponse
 */
export interface AuthUser {
  id: number;
  name: string;
  email: string;
  status: string;
}

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface RegisterData {
  name: string;
  email: string;
  password: string;
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  user: AuthUser;
}

export interface RefreshResponse {
  access_token: string;
  /** Rotated refresh token — the backend blacklists the previous one
   *  (ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION), so the client
   *  MUST persist this or the next refresh fails. */
  refresh_token: string;
}

export interface LogoutResponse {
  message: string;
}