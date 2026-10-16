'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import axios from 'axios';
import { authApi } from '@/lib/api';
import { useAuthStore } from '@/store/useAuthStore';
import { LogIn, Loader2, AlertCircle, ShieldCheck, Key } from 'lucide-react';

export default function LoginForm() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
  const router = useRouter();
  const setAuth = useAuthStore((state) => state.setAuth);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setError(null);

    try {
      const response = await authApi.login({ email, password });
      const { access_token, refresh_token, user } = response.data;
      
      let currentUser = user;
      if (!currentUser) {
        const meResponse = await authApi.me();
        currentUser = meResponse.data;
      }

      setAuth(currentUser, access_token, refresh_token);
      router.push('/dashboard');
    } catch (err: unknown) {
      let message = 'Access Denied: Invalid Security Credentials';
      if (axios.isAxiosError(err)) {
        const detail = err.response?.data?.detail;
        if (Array.isArray(detail)) message = detail.join(', ');
        else if (typeof detail === 'string') message = detail;
        else if (err.response?.data?.error) message = err.response.data.error;
      }
      setError(message);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="w-full max-w-md p-10 rounded-none border border-slate-800 bg-[#0d131f]/80 backdrop-blur-md shadow-2xl relative overflow-hidden group">
      {/* Structural Decoration */}
      <div className="absolute top-0 left-0 w-16 h-16 border-t-2 border-l-2 border-blue-500/30 -ml-1 -mt-1"></div>
      <div className="absolute bottom-0 right-0 w-16 h-16 border-b-2 border-r-2 border-blue-500/30 -mr-1 -mb-1"></div>
      
      <div className="relative z-10 flex flex-col items-center mb-10">
        <div className="w-16 h-16 bg-white text-black flex items-center justify-center mb-6 shadow-2xl group-hover:scale-105 transition-transform duration-500">
           <span className="font-black text-2xl italic tracking-tighter">MW</span>
        </div>
        <h1 className="text-xl font-black text-white tracking-[0.2em] uppercase">Security Portal</h1>
        <div className="h-0.5 w-12 bg-blue-600 mt-3"></div>
        <p className="text-slate-500 text-[10px] uppercase font-bold tracking-widest mt-4">Authorized Institutional Access Only</p>
      </div>

      {error && (
        <div className="mb-8 p-4 rounded-none bg-rose-500/10 border border-rose-500/20 flex items-start gap-3 text-rose-400 text-[10px] font-bold uppercase tracking-wider animate-[fadeIn_0.3s_ease-out]">
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        <div className="space-y-2">
          <label className="block text-[9px] font-black text-slate-500 uppercase tracking-[0.3em] ml-1">Terminal ID</label>
          <div className="relative">
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full bg-slate-900 border border-slate-800 rounded-none px-4 py-4 text-white text-xs font-mono focus:outline-none focus:border-blue-500/50 transition-all placeholder:text-slate-800"
              placeholder="operator@autofx.mw"
              required
            />
          </div>
        </div>

        <div className="space-y-2">
          <label className="block text-[9px] font-black text-slate-500 uppercase tracking-[0.3em] ml-1">Access Protocol</label>
          <div className="relative">
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-slate-900 border border-slate-800 rounded-none px-4 py-4 text-white text-xs font-mono focus:outline-none focus:border-blue-500/50 transition-all placeholder:text-slate-800"
              placeholder="••••••••••••"
              required
            />
          </div>
        </div>

        <button
          type="submit"
          disabled={isLoading}
          className="w-full group relative overflow-hidden rounded-none bg-blue-600 px-6 py-5 transition-all hover:bg-blue-500 disabled:opacity-50"
        >
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/5 to-transparent translate-x-[-100%] group-hover:translate-x-[100%] transition-transform duration-1000"></div>
          <div className="relative flex items-center justify-center gap-3">
            {isLoading ? (
              <Loader2 className="animate-spin text-white" size={20} />
            ) : (
              <>
                <span className="font-black text-white tracking-[0.3em] uppercase text-[11px]">Grant Access</span>
                <ShieldCheck size={18} className="text-white/80" />
              </>
            )}
          </div>
        </button>
      </form>

      <div className="mt-12 pt-8 border-t border-slate-800/50 text-center">
        <div className="flex justify-center gap-6 mb-4 grayscale opacity-30">
            <Key size={14} className="text-slate-500" />
            <div className="h-4 w-px bg-slate-800" />
            <ShieldCheck size={14} className="text-slate-500" />
        </div>
        <p className="text-slate-600 text-[8px] uppercase tracking-[0.2em] font-bold">
            AES-256 AES-GCM Encrypted Connection<br/>
            Node: AFX-Terminal-Primary-01
        </p>
      </div>
    </div>
  );
}
