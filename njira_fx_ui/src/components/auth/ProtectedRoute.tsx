'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuthStore } from '@/store/useAuthStore';

interface ProtectedRouteProps {
  children: React.ReactNode;
}

/**
 * Client-side route guard.
 *
 * Auth state lives in the Zustand store (persisted to localStorage under
 * 'njira-auth-storage'), which Next.js middleware cannot read — the app does
 * not set an auth cookie. This wrapper therefore redirects unauthenticated
 * users to /login and renders nothing until the redirect completes.
 */
export default function ProtectedRoute({ children }: ProtectedRouteProps) {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const router = useRouter();

  useEffect(() => {
    if (!isAuthenticated) {
      router.replace('/login');
    }
  }, [isAuthenticated, router]);

  if (!isAuthenticated) return null;

  return <>{children}</>;
}