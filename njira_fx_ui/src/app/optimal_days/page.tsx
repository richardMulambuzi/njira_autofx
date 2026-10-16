'use client';
import { useState, useEffect } from 'react';
import ProtectedRoute from '@/components/auth/ProtectedRoute';
import api from '@/lib/api';

interface OptimalTime {
  symbol: string;
  best_days: string[];
  best_hours: string[];
}

export default function OptimalDays() {
  const [optimalTimes, setOptimalTimes] = useState<OptimalTime[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const fetchOptimalTimes = async () => {
      try {
        const response = await api.get('/optimalTime/');

        const data = response.data;

        type RawOptimalTime = [string, string[] | string, string[] | string, string];

        const formattedData = (data.optimal_times as RawOptimalTime[]).map((item) => ({
          symbol: item[0],
          best_days: Array.isArray(item[1]) ? item[1] : [item[1]],
          best_hours: Array.isArray(item[2]) ? item[2] : [item[2]],
          notes: item[3] || ''
        }));

        // const formattedData = data.optimal_times.map((item: any[]) => ({
        //   symbol: item[0],
        //   best_days: Array.isArray(item[1]) ? item[1] : [item[1]],
        //   best_hours: Array.isArray(item[2]) ? item[2] : [item[2]],
        //   notes: item[3] || ''
        // }));

        setOptimalTimes(formattedData);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error occurred');
      } finally {
        setLoading(false);
      }
    };

    fetchOptimalTimes();
  }, []);

  if (loading) return <LoadingSpinner />;
  if (error) return <ErrorDisplay message={error} />;

  return (
    <ProtectedRoute>
      <div className="bg-white p-6 md:py-12 rounded-lg shadow-sm overflow-hidden ">
      <div className='flex flex-col items-center justify-center'>

        <h2 className="text-3xl font-bold mb-6 text-gray-800 text-center">Optimal <span className='trading'>Trading</span> Times</h2>
        <p className='lg:w-[60%] text-gray-600 text-center mb-8'>Discover the best days and hours to trade based on past performance data. Use these insights to time your trades more effectively and increase your chances of success. Hover over each card to explore key recommendations for every trading symbol.</p>

      </div>
      <div className="relative overflow-hidden">
        <div className="flex gap-4 animate-scroll whitespace-nowrap hover:[animation-play-state:paused]">
          {optimalTimes.map((time, index) => (
            <OptimalTimeCard
              key={index}
              symbol={time.symbol}
              bestDays={time.best_days}
              bestHours={time.best_hours}
            />
          ))}
        </div>
      </div>

      {optimalTimes.length === 0 && !loading && <EmptyState />}
    </div>
    </ProtectedRoute>
  );
}

const LoadingSpinner = () => (
  <div className="flex justify-center items-center h-40">
    <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-blue-500"></div>
  </div>
);

const ErrorDisplay = ({ message }: { message: string }) => (
  <div className="bg-red-50 border-l-4 border-red-500 p-4">
    <div className="flex">
      <div className="flex-shrink-0">
        <svg className="h-5 w-5 text-red-500" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
        </svg>
      </div>
      <div className="ml-3">
        <p className="text-sm text-red-700">{message}</p>
      </div>
    </div>
  </div>
);

interface OptimalTimeCardProps {
  symbol: string;
  bestDays: string[];
  bestHours: string[];
}

const OptimalTimeCard = ({ symbol, bestDays, bestHours }: OptimalTimeCardProps) => (
  <div className="flex-shrink-0 w-72 bg-gradient-to-br from-blue-50 to-blue-100 rounded-lg p-5 border border-blue-200">
    <div className="flex items-center mb-3">
      <span className="inline-block  trading text-lg font-semibold  rounded-xl">
        {symbol}
      </span>
    </div>

    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-medium text-gray-500">Best Days</h3>
        <div className="flex flex-wrap gap-1 mt-1">
          {bestDays.map((day, i) => (
            <span key={i} className="bg-white px-2 py-1 rounded text-sm font-medium text-gray-700 shadow-xs">
              {day}
            </span>
          ))}
        </div>
      </div>

      <div>
        <h3 className="text-sm font-medium text-gray-500">Best Hours</h3>
        <div className="flex flex-wrap gap-1 mt-1">
          {bestHours.map((hour, i) => (
            <span key={i} className="bg-white px-2 py-1 rounded text-sm font-medium text-black shadow-xs">
              {hour}
            </span>
          ))}
        </div>
      </div>
    </div>
  </div>
);

const EmptyState = () => (
  <div className="text-center py-10">
    <svg className="mx-auto h-12 w-12 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
    <h3 className="mt-2 text-sm font-medium text-gray-900">No optimal times found</h3>
    <p className="mt-1 text-sm text-gray-500">We couldn&apos;t find any optimal trading times for your account.</p>
  </div>
);
