'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import api from '@/lib/api';
import { useAuthStore } from '@/store/useAuthStore';
import { useReverb } from '@/hooks/useReverb';

/**
 * Single source of truth for tradable symbols.
 *
 * - **Catalogue**: pushed via Pusher (`symbols_catalogue` event on `njira-market`).
 *   Falls back to HTTP on first mount if Pusher hasn't delivered yet.
 * - **User selection**: persisted in ClickHouse via REST (`/save_symbols/`).
 */

export interface CatalogueSymbol {
  symbol: string;
  name: string;
}

export interface UseSymbolsResult {
  catalogue: CatalogueSymbol[];
  available: string[];
  selected: string[];
  loading: boolean;
  brokerOnline: boolean;
  addSymbol: (sym: string) => Promise<void>;
  removeSymbol: (sym: string) => Promise<void>;
  saving: boolean;
}

export function useSymbols(): UseSymbolsResult {
  const { user } = useAuthStore();
  const email = user?.email ?? '';
  const { client: pusher } = useReverb();

  const [catalogue, setCatalogue] = useState<CatalogueSymbol[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [brokerOnline, setBrokerOnline] = useState(false);
  const gotPusherEvent = useRef(false);

  const available = catalogue.map(c => c.symbol);

  // ── Catalogue: subscribe to Pusher for live updates ──
  useEffect(() => {
    if (!pusher) return;
    const channel = pusher.subscribe('njira-market');

    const handler = (data: { symbols?: Array<{ symbol: string; symbolId?: number; digits?: number }>; count?: number }) => {
      gotPusherEvent.current = true;
      const rows = data.symbols ?? [];
      const syms: CatalogueSymbol[] = rows
        .map(r => ({ symbol: String(r.symbol ?? '').toUpperCase(), name: String(r.symbol ?? '') }))
        .filter(s => s.symbol);
      setCatalogue(syms);
      setBrokerOnline(true);
      setLoading(false);
    };

    channel.bind('symbols_catalogue', handler);

    // Initial HTTP fallback — if Pusher hasn't delivered yet, fetch once.
    // ponytail: silent catch — cTrader only connects from ticks_engine, not Django; 503 is expected
    const timeout = setTimeout(() => {
      if (gotPusherEvent.current) return;
      fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/provider/symbols/`)
        .then(r => r.ok ? r.json() : Promise.reject())
        .then(json => {
          const rows = json?.data?.symbols ?? [];
          const syms: CatalogueSymbol[] = rows
            .map((r: Record<string, unknown>) => ({
              symbol: String(r.symbol ?? '').toUpperCase(),
              name: String(r.symbol ?? ''),
            }))
            .filter((s: CatalogueSymbol) => s.symbol);
          setCatalogue(syms);
          setBrokerOnline(true);
        })
        .catch(() => setBrokerOnline(false))
        .finally(() => setLoading(false));
    }, 3000); // give Pusher 3s to deliver before HTTP fallback

    return () => {
      clearTimeout(timeout);
      channel.unbind('symbols_catalogue', handler);
    };
  }, [pusher]);

  // ── User selection: fetch from DB ──
  useEffect(() => {
    if (!email) return;
    api.get('/get_symbols/', { params: { email } })
      .then(res => {
        const syms = res.data?.symbol;
        if (Array.isArray(syms)) {
          const flat = [...new Set(syms.map((s: string | string[]) => Array.isArray(s) ? s[0] : s).filter(Boolean))];
          setSelected(flat);
        }
      })
      .catch(() => {});
  }, [email]);

  const persistSelected = useCallback(async (next: string[]) => {
    if (!email) return;
    setSaving(true);
    try {
      await api.post('/save_symbols/', { email, symbols: next });
      setSelected(next);
      if (next.length > 0) localStorage.setItem('njira-symbol', next[0]);
    } finally {
      setSaving(false);
    }
  }, [email]);

  const addSymbol = useCallback(async (sym: string) => {
    await persistSelected([...new Set([...selected, sym])]);
  }, [selected, persistSelected]);

  const removeSymbol = useCallback(async (sym: string) => {
    await persistSelected(selected.filter(s => s !== sym));
  }, [selected, persistSelected]);

  return {
    catalogue, available, selected, loading, brokerOnline,
    addSymbol, removeSymbol, saving,
  };
}
