'use client';

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import LoginForm from '@/components/auth/LoginForm';
import BootScreen from '@/components/terminal/BootScreen';
import { useAuthStore } from '@/store/useAuthStore';
import Head from 'next/head';

export default function EntryPage() {
  const { isAuthenticated } = useAuthStore();
  const router = useRouter();
  const [isBooting, setIsBooting] = useState(true);

  // Redirect to dashboard if already authenticated
  useEffect(() => {
    if (isAuthenticated) {
      router.push('/dashboard');
    }
  }, [isAuthenticated, router]);

  if (isBooting) {
    return <BootScreen onComplete={() => setIsBooting(false)} />;
  }

  if (isAuthenticated) {
    return (
      <div className="min-h-screen bg-[#05080f] flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <div className="w-12 h-12 border-4 border-blue-500/20 border-t-blue-500 rounded-full animate-spin"></div>
          <p className="text-slate-400 font-medium animate-pulse">Entering Secure Terminal...</p>
        </div>
      </div>
    );
  }

  return (
    <>
      <Head>
        <title>Login | Auto FX MW Terminal</title>
        <meta name="description" content="Institutional-grade automated trading terminal access." />
      </Head>
      
      <div className="min-h-screen w-full bg-[#05080f] flex items-center justify-center p-4 relative overflow-hidden">
        {/* Advanced Background Aesthetics */}
        <div className="absolute top-[-20%] left-[-10%] w-[60%] h-[60%] bg-blue-600/10 rounded-full blur-[140px] animate-pulse"></div>
        <div className="absolute bottom-[-20%] right-[-10%] w-[60%] h-[60%] bg-indigo-600/10 rounded-full blur-[140px] animate-pulse" style={{ animationDelay: '2s' }}></div>
        
        {/* Animated Grid Overlay */}
        <div className="absolute inset-0 bg-[url('https://grainy-gradients.vercel.app/noise.svg')] opacity-20 pointer-events-none"></div>
        <div className="absolute inset-0 bg-[linear-gradient(to_right,#80808012_1px,transparent_1px),linear-gradient(to_bottom,#80808012_1px,transparent_1px)] bg-[size:40px_40px] [mask-image:radial-gradient(ellipse_60%_50%_at_50%_0%,#000_70%,transparent_100%)]"></div>

        <div className="relative z-10 w-full flex flex-col items-center">
          <div className="mb-10 flex flex-col items-center gap-4 group">
            <div className="w-16 h-16 bg-white rounded-2xl flex items-center justify-center shadow-[0_0_30px_rgba(255,255,255,0.2)] group-hover:scale-110 transition-transform duration-500">
              <span className="text-black font-black text-3xl italic tracking-tighter">MW</span>
            </div>
            <div className="flex flex-col items-center">
              <span className="text-2xl font-bold text-white tracking-[0.3em] uppercase">Auto FX MW</span>
              <div className="h-px w-12 bg-blue-500 mt-2"></div>
            </div>
          </div>
          
          <LoginForm />
          
          <div className="mt-12 flex flex-col items-center gap-6">
            <div className="flex gap-8 text-slate-500 text-[10px] uppercase tracking-[0.2em] font-medium">
              <span className="hover:text-blue-400 cursor-pointer transition-colors border-b border-transparent hover:border-blue-400 pb-1">Network Status</span>
              <span className="hover:text-blue-400 cursor-pointer transition-colors border-b border-transparent hover:border-blue-400 pb-1">Protocol v2.4</span>
              <span className="hover:text-blue-400 cursor-pointer transition-colors border-b border-transparent hover:border-blue-400 pb-1">Security Node</span>
            </div>
            
            <div className="px-4 py-2 rounded-full border border-slate-800/50 bg-slate-900/30 backdrop-blur-sm flex items-center gap-3">
               <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              <span className="text-[10px] text-slate-400 font-mono tracking-wider">SYSTEMS OPERATIONAL • GLOBAL NODES ONLINE</span>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
