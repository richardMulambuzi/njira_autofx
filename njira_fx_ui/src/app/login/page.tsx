import React from 'react';
import LoginForm from '@/components/auth/LoginForm';

export default function LoginPage() {
  return (
    <div className="min-h-screen w-full bg-[#05080f] flex items-center justify-center p-4 relative overflow-hidden">
      {/* Background Orbs */}
      <div className="absolute top-[-10%] left-[-10%] w-[40%] h-[40%] bg-blue-600/10 rounded-full blur-[120px]"></div>
      <div className="absolute bottom-[-10%] right-[-10%] w-[40%] h-[40%] bg-indigo-600/10 rounded-full blur-[120px]"></div>
      
      <div className="relative z-10 w-full flex flex-col items-center">
        <div className="mb-8 flex items-center gap-3">
          <div className="w-10 h-10 bg-white rounded flex items-center justify-center">
            <span className="text-black font-black text-xl italic">MW</span>
          </div>
          <span className="text-xl font-bold text-white tracking-widest uppercase">Auto FX Terminal</span>
        </div>
        
        <LoginForm />
        
        <div className="mt-8 flex gap-6 text-slate-600 text-[10px] uppercase tracking-[0.2em]">
          <span className="hover:text-slate-400 cursor-pointer transition-colors">Privacy Policy</span>
          <span className="hover:text-slate-400 cursor-pointer transition-colors">Terms of Service</span>
          <span className="hover:text-slate-400 cursor-pointer transition-colors">Security Audit</span>
        </div>
      </div>
    </div>
  );
}
