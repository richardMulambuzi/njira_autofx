'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useReverb } from './useReverb';
import type {
  StructureEvent,
  LiquidityEvent,
  OrderBlockEvent,
  SignalEvent,
  CandleEvent,
  FVGEvent,
  TickEvent,
  VolumeProfileEvent,
  ProviderStatusEvent,
  MtfStatusEvent,
  AnalyzeResponse,
} from '@/types/market';

/**
 * Subscribes to all intelligence events on the 'njira-market' Pusher channel
 * and exposes them as React state for chart visualization.
 *
 * Events: tick (throttled), candle, structure, liquidity, order_block, signal.
 */
export interface MarketEventsState {
  lastCandle: CandleEvent | null;
  lastTick: TickEvent | null;
  structures: StructureEvent[];
  liquidityPools: LiquidityEvent[];
  orderBlocks: OrderBlockEvent[];
  fvgEvents: FVGEvent[];
  volumeProfile: VolumeProfileEvent | null;
  signals: SignalEvent[];
  analysisSnapshot: AnalyzeResponse | null;
  providerStatus: ProviderStatusEvent | null;
  mtfStatus: MtfStatusEvent | null;
  isConnected: boolean;
}

const MAX_HISTORY = 50;

export function useMarketEvents(): MarketEventsState {
  const { client, isConnected } = useReverb();
  const [lastCandle, setLastCandle] = useState<CandleEvent | null>(null);
  const [lastTick, setLastTick] = useState<TickEvent | null>(null);
  const [structures, setStructures] = useState<StructureEvent[]>([]);
  const [liquidityPools, setLiquidityPools] = useState<LiquidityEvent[]>([]);
  const [orderBlocks, setOrderBlocks] = useState<OrderBlockEvent[]>([]);
  const [fvgEvents, setFvgEvents] = useState<FVGEvent[]>([]);
  const [volumeProfile, setVolumeProfile] = useState<VolumeProfileEvent | null>(null);
  const [signals, setSignals] = useState<SignalEvent[]>([]);
  const [analysisSnapshot, setAnalysisSnapshot] = useState<AnalyzeResponse | null>(null);
  const [providerStatus, setProviderStatus] = useState<ProviderStatusEvent | null>(null);
  const [mtfStatus, setMtfStatus] = useState<MtfStatusEvent | null>(null);

  // Use refs for stable callbacks
  const structuresRef = useRef(structures);
  const liquidityRef = useRef(liquidityPools);
  const orderBlocksRef = useRef(orderBlocks);
  const fvgRef = useRef(fvgEvents);
  const signalsRef = useRef(signals);
  structuresRef.current = structures;
  liquidityRef.current = liquidityPools;
  orderBlocksRef.current = orderBlocks;
  fvgRef.current = fvgEvents;
  signalsRef.current = signals;

  const handleCandle = useCallback((data: unknown) => {
    setLastCandle(data as CandleEvent);
  }, []);

  const handleStructure = useCallback((data: unknown) => {
    const evt = data as StructureEvent;
    setStructures((prev) => [...prev, evt].slice(-MAX_HISTORY));
  }, []);

  const handleLiquidity = useCallback((data: unknown) => {
    const evt = data as LiquidityEvent;
    setLiquidityPools((prev) => [...prev, evt].slice(-MAX_HISTORY));
  }, []);

  const handleOrderBlock = useCallback((data: unknown) => {
    const evt = data as OrderBlockEvent;
    setOrderBlocks((prev) => [...prev, evt].slice(-MAX_HISTORY));
  }, []);

  const handleSignal = useCallback((data: unknown) => {
    const evt = data as SignalEvent;
    setSignals((prev) => [evt, ...prev].slice(0, MAX_HISTORY));
  }, []);

  const handleFvg = useCallback((data: unknown) => {
    const evt = data as FVGEvent;
    setFvgEvents((prev) => [...prev, evt].slice(-MAX_HISTORY));
  }, []);

  const handleVolumeProfile = useCallback((data: unknown) => {
    setVolumeProfile(data as VolumeProfileEvent);
  }, []);

  const handleTick = useCallback((data: unknown) => {
    setLastTick(data as TickEvent);
  }, []);

  const handleAnalysis = useCallback((data: unknown) => {
    setAnalysisSnapshot(data as AnalyzeResponse);
  }, []);

  const handleProviderStatus = useCallback((data: unknown) => {
    setProviderStatus(data as ProviderStatusEvent);
  }, []);

  const handleMtfStatus = useCallback((data: unknown) => {
    setMtfStatus(data as MtfStatusEvent);
  }, []);

  useEffect(() => {
    if (!client) return;

    const channel = client.subscribe('njira-market');
    channel.bind('candle', handleCandle);
    channel.bind('tick', handleTick);
    channel.bind('structure', handleStructure);
    channel.bind('liquidity', handleLiquidity);
    channel.bind('order_block', handleOrderBlock);
    channel.bind('fvg', handleFvg);
    channel.bind('volume_profile', handleVolumeProfile);
    channel.bind('signal', handleSignal);
    channel.bind('analysis', handleAnalysis);
    channel.bind('provider_status', handleProviderStatus);
    channel.bind('mtf_status', handleMtfStatus);

    return () => {
      channel.unbind('candle', handleCandle);
      channel.unbind('tick', handleTick);
      channel.unbind('structure', handleStructure);
      channel.unbind('liquidity', handleLiquidity);
      channel.unbind('order_block', handleOrderBlock);
      channel.unbind('fvg', handleFvg);
      channel.unbind('volume_profile', handleVolumeProfile);
      channel.unbind('signal', handleSignal);
      channel.unbind('analysis', handleAnalysis);
      channel.unbind('provider_status', handleProviderStatus);
      channel.unbind('mtf_status', handleMtfStatus);
      client.unsubscribe('njira-market');
    };
  }, [client, handleCandle, handleTick, handleStructure, handleLiquidity, handleOrderBlock, handleFvg, handleVolumeProfile, handleSignal, handleAnalysis, handleProviderStatus, handleMtfStatus]);

  return {
    lastCandle,
    lastTick,
    structures,
    liquidityPools,
    orderBlocks,
    fvgEvents,
    volumeProfile,
    signals,
    analysisSnapshot,
    providerStatus,
    mtfStatus,
    isConnected,
  };
}
