'use client';

import React, { useEffect, useRef, useState } from 'react';
import {
  createChart,
  CandlestickSeries,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
  type IPriceLine,
} from 'lightweight-charts';
import { marketApi, drawingApi, signalApi, type ChartDrawingShape } from '@/lib/api';
import { useReverb } from '@/hooks/useReverb';
import { useMarketEvents } from '@/hooks/useMarketEvents';
import { useSymbols } from '@/hooks/useSymbols';
import type { Candle, OrderBlockEvent, LiquidityEvent } from '@/types/market';

/** Backend `Candle` with the epoch-seconds guarantee lightweight-charts needs. */
type CandleData = Candle & { time: UTCTimestamp };

type Tool = 'none' | 'trend' | 'rect';

interface Shape {
  kind: 'trend' | 'rect';
  /** Chart coordinates (epoch seconds + price) — survive pan/zoom, unlike pixels. */
  t1: number;
  p1: number;
  t2: number;
  p2: number;
}

export interface ChartOrder {
  label: string;
  price: number;
  color: string;
}

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] as const;

/**
 * TradingView-style candlestick chart for the Markets page.
 *
 * - History: GET /api/market/candles/ via marketApi (ClickHouse-backed).
 * - Live: Pusher 'njira-market' channel 'tick' events (bid) aggregated into
 *   1-minute candles client-side; ticks are throttled server-side to 1/s.
 * - Interactions (wheel zoom, drag pan, axis drag): lightweight-charts defaults.
 * - Drawings are stored in chart coordinates so they stay glued to candles.
 */
export default function MarketChart({ initialSymbol }: { initialSymbol?: string }) {
  // ponytail: read global symbol from localStorage, fallback to initialSymbol or EURUSD
  const defaultSymbol = (() => {
    if (typeof window === 'undefined') return initialSymbol ?? 'EURUSD';
    const stored = localStorage.getItem('njira-symbol');
    return stored || initialSymbol || 'EURUSD';
  })();
  const containerRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const lastCandleRef = useRef<CandleData | null>(null);
  const shapesRef = useRef<Shape[]>([]);
  const draftRef = useRef<Shape | null>(null);
  const drawingRef = useRef(false);

  const [symbol, setSymbol] = useState(defaultSymbol);
  const [timeframe, setTimeframe] = useState<(typeof TIMEFRAMES)[number]>('15m');
  const [tool, setTool] = useState<Tool>('none');
  const [hud, setHud] = useState<CandleData | null>(null);
  const [candles, setCandles] = useState<CandleData[]>([]);
  const [orders, setOrders] = useState<ChartOrder[]>([]);
  const [spread, setSpread] = useState<number | null>(null);
  const { client } = useReverb();
  const { orderBlocks, liquidityPools, volumeProfile } = useMarketEvents();
  const { available } = useSymbols();
  const symbolOptions = available.length > 0 ? available : ['EURUSD'];

  // ── 1. Chart lifecycle ──────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    // autoSize uses ResizeObserver internally — no manual resize listener.
    const chart = createChart(el, {
      autoSize: true,
      layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
      grid: { vertLines: { color: '#1f222e' }, horzLines: { color: '#1f222e' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#2b2b3a' },
      timeScale: { borderColor: '#2b2b3a', timeVisible: true, secondsVisible: false },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
      borderVisible: false,
    });
    chartRef.current = chart;
    seriesRef.current = series;

    chart.subscribeCrosshairMove((param) => {
      if (!param.time || param.point === undefined) {
        setHud(lastCandleRef.current);
        return;
      }
      const d = param.seriesData.get(series) as CandleData | undefined;
      if (d) setHud({ ...d, time: param.time as UTCTimestamp });
    });

    // Redraw overlay while panning/zooming so shapes track their candles.
    const redraw = () => drawShapes();
    chart.timeScale().subscribeVisibleLogicalRangeChange(redraw);

    return () => {
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(redraw);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // ── 2. Historical candles per symbol/timeframe ──────────────────────
  useEffect(() => {
    let cancelled = false;
    marketApi.candles(symbol, timeframe).then((res) => {
      if (cancelled) return;
      // Backend guarantees epoch-seconds ints; cast at the trust boundary.
      const data = (res.data.candles ?? []).map(
        (c) => ({ ...c, time: c.time as UTCTimestamp }) as CandleData,
      );
      setCandles(data);
      seriesRef.current?.setData(data);
      lastCandleRef.current = data.length ? { ...data[data.length - 1] } : null;
      setHud(lastCandleRef.current);
      if (data.length) chartRef.current?.timeScale().fitContent();
    }).catch(() => {/* endpoint degrades to empty; nothing to do */});
    return () => { cancelled = true; };
  }, [symbol, timeframe]);

  // ── 2a. Load persisted drawings on symbol change ────────────────────
  useEffect(() => {
    let cancelled = false;
    drawingApi.list(symbol).then((res) => {
      if (cancelled) return;
      shapesRef.current = (res.data.drawings ?? []) as Shape[];
      drawShapes();
    }).catch(() => { if (!cancelled) shapesRef.current = []; drawShapes(); });
    return () => { cancelled = true; };
  }, [symbol]);

  // ── 2b. Fetch live signal for order lines (entry/SL/TP) ─────────────
  useEffect(() => {
    let cancelled = false;
    signalApi.live().then((res) => {
      if (cancelled) return;
      const match = (res.data.signals ?? []).find(
        (s) => s.symbol === symbol || s.symbol?.toUpperCase() === symbol.toUpperCase()
      );
      if (!match) { setOrders([]); return; }
      const entry = Number(match.entry);
      const sl    = Number(match.stop_loss);
      const tp    = Number(match.take_profit);
      const isBuy = String(match.direction).toUpperCase().includes('BUY');
      const newOrders: ChartOrder[] = [];
      if (entry > 0) newOrders.push({ label: `Entry (${isBuy ? 'BUY' : 'SELL'})`, price: entry, color: '#2962ff' });
      if (sl > 0)    newOrders.push({ label: 'Stop Loss', price: sl, color: '#ef5350' });
      if (tp > 0)    newOrders.push({ label: 'Take Profit', price: tp, color: '#26a69a' });
      setOrders(newOrders);
    }).catch(() => { if (!cancelled) setOrders([]); });
    return () => { cancelled = true; };
  }, [symbol]);

  // ── 3. Live ticks → 1-minute candle aggregation (Pusher) ────────────
  useEffect(() => {
    if (!client) return;
    const channel = client.subscribe('njira-market');
    const onTick = (tick: { symbol?: string; bid?: number; spread?: number; timestamp?: string }) => {
      if (!seriesRef.current || tick.symbol !== symbol || typeof tick.bid !== 'number') return;
      const secs = Math.floor(new Date(tick.timestamp ?? Date.now()).getTime() / 1000);
      const bucket = (Math.floor(secs / 60) * 60) as UTCTimestamp;
      let c = lastCandleRef.current;
      if (!c || c.time !== bucket) {
        c = { time: bucket, open: c?.close ?? tick.bid, high: tick.bid, low: tick.bid, close: tick.bid };
      } else {
        c.high = Math.max(c.high, tick.bid);
        c.low = Math.min(c.low, tick.bid);
        c.close = tick.bid;
      }
      lastCandleRef.current = c;
      seriesRef.current.update(c);
      // spread is pre-computed server-side as (ask − bid) in price units
      if (typeof tick.spread === 'number') setSpread(Number((tick.spread * 10000).toFixed(1)));
    };
    channel.bind('tick', onTick);
    return () => {
      channel.unbind('tick', onTick);
      client.unsubscribe('njira-market');
    };
  }, [client, symbol]);

  // ── 4. Order lines (entry/SL/TP) as native price lines ──────────────
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const lines: IPriceLine[] = orders.map((o) =>
      series.createPriceLine({
        price: o.price,
        color: o.color,
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: o.label,
      }),
    );
    return () => lines.forEach((l) => series.removePriceLine(l));
  }, [orders]);

  // ── 4a. Intelligence overlays: OB zones, liquidity levels as price lines ─
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const lines: IPriceLine[] = [];

    // Order blocks: high and low boundaries
    const latestObs = orderBlocks.slice(-5); // last 5 OBs
    for (const ob of latestObs) {
      const isBull = ob.direction.includes('BULLISH');
      const color = isBull ? 'rgba(38,166,154,0.6)' : 'rgba(239,83,80,0.6)';
      const label = isBull ? 'OB↑' : 'OB↓';
      lines.push(series.createPriceLine({
        price: ob.high, color, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: `${label} H`,
      }));
      lines.push(series.createPriceLine({
        price: ob.low, color, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: false, title: `${label} L`,
      }));
    }

    // Liquidity levels
    const latestLiq = liquidityPools.slice(-5);
    for (const pool of latestLiq) {
      const isSwept = pool.event === 'SWEEP_DETECTED';
      const color = isSwept ? '#ff9800' : 'rgba(156,39,176,0.5)';
      const label = isSwept ? '⚡Sweep' : pool.pool_type;
      lines.push(series.createPriceLine({
        price: pool.price, color, lineWidth: 1, lineStyle: isSwept ? 0 : 2,
        axisLabelVisible: true, title: label,
      }));
    }

    return () => lines.forEach((l) => series.removePriceLine(l));
  }, [orderBlocks, liquidityPools]);

  // ── 4b. Developing volume profile: POC / VAH / VAL price lines ─────
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !volumeProfile || !Number.isFinite(volumeProfile.poc)) return;
    const lines: IPriceLine[] = [
      series.createPriceLine({
        price: volumeProfile.poc,
        color: '#8b5cf6',
        lineWidth: 2,
        lineStyle: 2,
        axisLabelVisible: true,
        title: 'POC',
      }),
      series.createPriceLine({
        price: volumeProfile.vah,
        color: '#22d3ee',
        lineWidth: 1,
        lineStyle: 0,
        axisLabelVisible: true,
        title: 'VAH',
      }),
      series.createPriceLine({
        price: volumeProfile.val,
        color: '#34d399',
        lineWidth: 1,
        lineStyle: 0,
        axisLabelVisible: true,
        title: 'VAL',
      }),
    ];
    return () => lines.forEach((l) => series.removePriceLine(l));
  }, [volumeProfile]);

  // ── 5. Drawing engine (overlay canvas, chart-coordinate shapes) ─────
  const toChartCoords = (x: number, y: number): { t: number; p: number } | null => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series) return null;
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return null;
    const t = chart.timeScale().coordinateToTime(x - rect.left);
    const p = series.coordinateToPrice(y - rect.top);
    if (t === null || p === null) return null;
    return { t: Number(t), p: Number(p) };
  };

  const drawShapes = () => {
    const canvas = overlayRef.current;
    const ctx = canvas?.getContext('2d');
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!canvas || !ctx || !chart || !series) return;

    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const ts = chart.timeScale();
    const xy = (s: Shape) => ({
      x1: ts.timeToCoordinate(s.t1 as UTCTimestamp),
      y1: series.priceToCoordinate(s.p1),
      x2: ts.timeToCoordinate(s.t2 as UTCTimestamp),
      y2: series.priceToCoordinate(s.p2),
    });

    const paint = (s: Shape, preview = false) => {
      const { x1, y1, x2, y2 } = xy(s);
      if (x1 === null || y1 === null || x2 === null || y2 === null) return;
      ctx.lineWidth = preview ? 1 : 2;
      if (s.kind === 'rect') {
        ctx.fillStyle = preview ? 'rgba(38,166,154,0.2)' : 'rgba(38,166,154,0.15)';
        ctx.strokeStyle = '#26a69a';
        ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      } else {
        ctx.strokeStyle = preview ? 'rgba(41,98,255,0.6)' : '#2962ff';
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
    };

    shapesRef.current.forEach((s) => paint(s));
    if (draftRef.current) paint(draftRef.current, true);
  };

  useEffect(() => { drawShapes(); }, [candles]); // repaint once history lands

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (tool === 'none') return;
    const rect = e.currentTarget.getBoundingClientRect();
    const c = toChartCoords(e.clientX - rect.left, e.clientY - rect.top);
    if (!c) return;
    drawingRef.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    draftRef.current = { kind: tool, t1: c.t, p1: c.p, t2: c.t, p2: c.p };
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawingRef.current || !draftRef.current) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const c = toChartCoords(e.clientX - rect.left, e.clientY - rect.top);
    if (!c) return;
    draftRef.current = { ...draftRef.current, t2: c.t, p2: c.p };
    drawShapes();
  };

  const onPointerUp = () => {
    if (!drawingRef.current || !draftRef.current) return;
    drawingRef.current = false;
    const d = draftRef.current;
    draftRef.current = null;
    // Ignore accidental clicks (no drag distance in either axis).
    if (d.t1 !== d.t2 || d.p1 !== d.p2) shapesRef.current.push(d);
    drawShapes();
    setTool('none'); // back to pan mode, like TradingView
    // ponytail: fire-and-forget persist; no debounce for single-user deployment
    drawingApi.save(symbol, shapesRef.current as ChartDrawingShape[]).catch(() => {});
  };

  const clearShapes = () => {
    shapesRef.current = [];
    drawShapes();
    drawingApi.save(symbol, []).catch(() => {});
  };

  return (
    <div className="w-full bg-[#1c2030] rounded-xl border border-[#2b2b3a] shadow-2xl relative select-none">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 p-3 border-b border-[#2b2b3a]">
        <select
          value={symbol}
          onChange={(e) => { setSymbol(e.target.value); localStorage.setItem('njira-symbol', e.target.value); }}
          className="bg-[#131722] text-xs text-gray-200 rounded px-2 py-1.5 border border-[#2b2b3a] focus:outline-none focus:border-[#2962ff]"
          aria-label="Symbol"
        >
          {symbolOptions
            .filter((s, i, a) => a.indexOf(s) === i)
            .map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
        </select>
        <div className="flex gap-1">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={`px-2 py-1 text-xs rounded font-medium transition-colors ${
                timeframe === tf ? 'bg-[#2962ff] text-white' : 'bg-[#131722] text-gray-400 hover:text-white'
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
        <div className="flex gap-1 ml-auto">
          <button
            onClick={() => setTool(tool === 'trend' ? 'none' : 'trend')}
            aria-pressed={tool === 'trend'}
            className={`px-2 py-1 text-xs rounded font-medium transition-colors ${
              tool === 'trend' ? 'bg-[#2962ff] text-white' : 'bg-[#131722] text-gray-400 hover:text-white'
            }`}
          >
            Trendline
          </button>
          <button
            onClick={() => setTool(tool === 'rect' ? 'none' : 'rect')}
            aria-pressed={tool === 'rect'}
            className={`px-2 py-1 text-xs rounded font-medium transition-colors ${
              tool === 'rect' ? 'bg-[#26a69a] text-white' : 'bg-[#131722] text-gray-400 hover:text-white'
            }`}
          >
            Order Block
          </button>
          <button
            onClick={clearShapes}
            className="px-2 py-1 text-xs rounded font-medium bg-red-900/40 text-red-400 hover:bg-red-900/60"
          >
            Clear
          </button>
        </div>
      </div>

      {/* HUD */}
      <div className="absolute top-16 left-6 z-10 flex gap-3 text-xs font-mono bg-[#131722]/80 backdrop-blur px-3 py-1.5 rounded border border-[#2b2b3a] pointer-events-none">
        <span className="text-gray-400">O:<span className="text-[#26a69a] ml-1">{hud?.open?.toFixed(5)}</span></span>
        <span className="text-gray-400">H:<span className="text-[#26a69a] ml-1">{hud?.high?.toFixed(5)}</span></span>
        <span className="text-gray-400">L:<span className="text-[#ef5350] ml-1">{hud?.low?.toFixed(5)}</span></span>
        <span className="text-gray-400">C:<span className="text-[#26a69a] ml-1">{hud?.close?.toFixed(5)}</span></span>
        {spread !== null && (
          <span className="text-gray-400 ml-2">Spread:<span className="text-[#ff9800] ml-1">{spread}p</span></span>
        )}
      </div>

      {/* Chart + drawing overlay */}
      <div className="relative w-full h-[480px]">
        <div ref={containerRef} className="absolute inset-0 z-0" />
        <canvas
          ref={overlayRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          className={`absolute inset-0 w-full h-full ${tool !== 'none' ? 'z-20 cursor-crosshair' : 'z-10 pointer-events-none'}`}
        />
      </div>

      <div className="flex items-center justify-between px-3 py-2 text-[10px] uppercase tracking-widest text-slate-500">
        <span>{symbol} · {timeframe}</span>
        <button
          onClick={() => chartRef.current?.timeScale().fitContent()}
          className="px-2 py-0.5 rounded bg-[#131722] border border-[#2b2b3a] text-gray-400 hover:text-white transition-colors"
        >
          Reset view
        </button>
      </div>
    </div>
  );
}
