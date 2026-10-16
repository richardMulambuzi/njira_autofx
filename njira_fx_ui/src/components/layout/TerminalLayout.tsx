import React, { ReactNode, useEffect, useState } from 'react';
import { useReverb } from '@/hooks/useReverb';
import { useRouter, usePathname } from 'next/navigation';
import { useAuthStore } from '@/store/useAuthStore';
import { refreshAccessToken } from '@/lib/api';
import { 
  Activity, Signal, Zap, Shield, BarChart2, List, 
  LogOut, User, Clock, ZapOff, History, Settings
} from 'lucide-react';
import TopIntelligenceBar from '../terminal/TopIntelligenceBar';
import BottomMarketStream from '../terminal/BottomMarketStream';
import RightEnginePanel from '../terminal/RightEnginePanel';

interface TerminalLayoutProps {
  children: ReactNode;
}

export default function TerminalLayout({ children }: TerminalLayoutProps) {
  const { isConnected } = useReverb();
  const { isAuthenticated, clearAuth, _hasHydrated } = useAuthStore();
  const [restoring, setRestoring] = useState(false);
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    // Wait for zustand-persist to read localStorage before judging the session.
    if (!_hasHydrated || isAuthenticated || restoring) return;
    const rt = typeof window !== 'undefined' ? localStorage.getItem('refresh_token') : null;
    if (!rt) {
      router.push('/login');
      return;
    }
    // Silent restore: rotate the refresh JWT into a fresh access token.
    setRestoring(true);
    refreshAccessToken()
      .catch(() => {
        clearAuth();
        router.push('/login');
      })
      .finally(() => setRestoring(false));
  }, [_hasHydrated, isAuthenticated, restoring, router, clearAuth]);

  if (!_hasHydrated || restoring || !isAuthenticated) {
    return (
      <div className="h-screen w-full bg-[#05080f] flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 skeleton-shimmer rounded-none" />
          <div className="w-24 h-2 skeleton-shimmer rounded-none" />
        </div>
      </div>
    );
  }

  const handleLogout = () => {
    clearAuth();
    router.push('/login');
  };

  return (
    <div className="h-screen w-full bg-[#05080f] text-slate-300 font-sans overflow-hidden grid select-none" 
      style={{
        gridTemplateAreas: `
          "top top top"
          "nav main right"
          "bottom bottom bottom"
        `,
        gridTemplateColumns: '52px 1fr 360px',
        gridTemplateRows: '42px 1fr 28px'
      }}
    >
      
      {/* 1. TOP INTELLIGENCE BAR */}
      <header style={{ gridArea: 'top' }} className="border-b border-slate-800/60 bg-[#0a0e17] z-30">
        <TopIntelligenceBar />
      </header>

      {/* 2. LEFT NAVIGATION PANEL (FUNCTION TABS) */}
      <aside style={{ gridArea: 'nav' }} className="bg-[#0a0e17] border-r border-slate-800/60 flex flex-col justify-between items-center py-4 z-20">
        <div className="flex flex-col gap-6 items-center">
          <div className="w-8 h-8 rounded-none bg-white text-black flex items-center justify-center font-black text-[10px] italic mb-4 shadow-[0_0_15px_rgba(255,255,255,0.1)]">
            MW
          </div>
          
          <nav className="flex flex-col gap-2">
            <NavTab icon={<BarChart2 size={16} />} label="Dashboard" active={pathname === '/dashboard'} onClick={() => router.push('/dashboard')} />
            <NavTab icon={<Activity size={16} />} label="Markets" active={pathname === '/markets'} onClick={() => router.push('/markets')} />
            <NavTab icon={<Signal size={16} />} label="Signals" active={pathname === '/signals'} onClick={() => router.push('/signals')} />
            <NavTab icon={<Zap size={16} />} label="Strategies" active={pathname === '/strategies'} onClick={() => router.push('/strategies')} />
            <NavTab icon={<History size={16} />} label="Backtest" active={pathname === '/backtest'} onClick={() => router.push('/backtest')} />
            <NavTab icon={<List size={16} />} label="Trades" active={pathname === '/trades'} onClick={() => router.push('/trades')} />
            <NavTab icon={<Shield size={16} />} label="System" active={pathname === '/system'} onClick={() => router.push('/system')} />
            <NavTab icon={<Settings size={16} />} label="Settings" active={pathname === '/settings'} onClick={() => router.push('/settings')} />
          </nav>
        </div>

        <div className="flex flex-col items-center gap-4">
          <div className={`w-1.5 h-1.5 rounded-none ${isConnected ? 'bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]' : 'bg-rose-500'}`} title={isConnected ? 'WS Connected' : 'WS Disconnected'} />
          <button onClick={handleLogout} className="p-2 text-slate-600 hover:text-rose-400 transition-colors">
            <LogOut size={16} />
          </button>
        </div>
      </aside>

      {/* 3. MAIN WORKSPACE */}
      <main style={{ gridArea: 'main' }} className="relative overflow-y-auto bg-[#05080f] custom-scrollbar z-10">
        <div className="p-5">
          {children}
        </div>
      </main>

      {/* 4. RIGHT SIGNAL + STRATEGY PANEL */}
      <aside style={{ gridArea: 'right' }} className="bg-[#0a0e17] border-l border-slate-800/60 z-20">
        <RightEnginePanel />
      </aside>

      {/* 5. BOTTOM MARKET + NEWS STREAM */}
      <footer style={{ gridArea: 'bottom' }} className="border-t border-slate-800/60 bg-[#0a0e17] z-30">
        <BottomMarketStream />
      </footer>

    </div>
  );
}

function NavTab({ icon, label, active = false, onClick }: { icon: React.ReactNode, label: string, active?: boolean, onClick: () => void }) {
  return (
    <div className="group relative">
      <button 
        onClick={onClick}
        className={`p-2.5 rounded-none transition-all duration-100 ${
          active 
            ? 'bg-blue-600/10 text-blue-400 border border-blue-500/20' 
            : 'hover:bg-slate-800/30 text-slate-600 hover:text-slate-300'
        }`}
      >
        {icon}
      </button>
      
      <div className="absolute left-12 top-1/2 -translate-y-1/2 px-2 py-1 rounded-none bg-slate-800 text-[9px] text-white font-bold uppercase tracking-widest opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50 whitespace-nowrap border border-slate-700">
        {label}
      </div>
    </div>
  );
}
