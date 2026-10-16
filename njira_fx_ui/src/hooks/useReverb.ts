'use client';

import { useEffect, useState } from 'react';
import Pusher from 'pusher-js';

/**
 * Realtime connectivity via Pusher cloud — shared singleton.
 *
 * All consumers share ONE Pusher instance (module-level). The instance is
 * created on first mount and torn down when the last consumer unmounts.
 */

export interface UseReverbResult {
  client: Pusher | null;
  isConnected: boolean;
}

// ── Singleton ────────────────────────────────────────────────────────
let sharedPusher: Pusher | null = null;
let refCount = 0;
let listeners: Array<(connected: boolean) => void> = [];

function notifyListeners(connected: boolean) {
  listeners.forEach(fn => fn(connected));
}

function getOrCreatePusher(): Pusher {
  if (sharedPusher) return sharedPusher;

  const key = process.env.NEXT_PUBLIC_PUSHER_KEY;
  if (!key) return null as unknown as Pusher;

  sharedPusher = new Pusher(key, {
    cluster: process.env.NEXT_PUBLIC_PUSHER_CLUSTER || 'ap2',
  });

  sharedPusher.connection.bind('connected', () => notifyListeners(true));
  sharedPusher.connection.bind('disconnected', () => notifyListeners(false));

  // Connectivity-proof binding
  const ch = sharedPusher.subscribe('njira-market');
  ch.bind('ping', (data: unknown) => console.debug('[njira-market] ping', data));

  return sharedPusher;
}

function destroyPusher() {
  if (!sharedPusher) return;
  sharedPusher.unsubscribe('njira-market');
  sharedPusher.connection.unbind_all();
  sharedPusher.disconnect();
  sharedPusher = null;
}
// ── End singleton ────────────────────────────────────────────────────

export function useReverb(): UseReverbResult {
  const [client, setClient] = useState<Pusher | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const realtimeEnabled = process.env.NEXT_PUBLIC_ENABLE_REALTIME === 'true';
    const key = process.env.NEXT_PUBLIC_PUSHER_KEY;
    if (!realtimeEnabled || !key) return;

    const pusher = getOrCreatePusher();
    refCount++;
    setClient(pusher);
    setIsConnected(pusher.connection.state === 'connected');

    const listener = (connected: boolean) => setIsConnected(connected);
    listeners.push(listener);

    return () => {
      listeners = listeners.filter(fn => fn !== listener);
      refCount--;
      if (refCount <= 0 && process.env.NODE_ENV === 'production') {
        destroyPusher();
        refCount = 0;
      }
      setClient(null);
    };
  }, []);

  return { client, isConnected };
}
