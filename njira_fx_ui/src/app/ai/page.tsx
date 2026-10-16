'use client';
import Footer from '@/components/footer/page';
import Header from '@/components/header/page';
import ProtectedRoute from '@/components/auth/ProtectedRoute';
import api from '@/lib/api';
import { useState, useEffect } from 'react';

interface Signal {
  Signal: string;
  Entry: string;
  SL: string;
  TP: string;
  Pair: string;
  Strategy: string;
  Timestamp: string;
}

interface RefinedData {
  signal_quality?: number;
  refinement_suggestions?: string[];
  risk_reward_evaluation?: {
    recommended_stop_loss?: number;
    recommended_take_profit?: number;
    suggested_position_size?: string;
  };
  market_context?: {
    volatility_impact?: string;
    economic_events_to_watch?: string;
  };
}

export default function AIOnCenter() {
  const [signal, setSignal] = useState<Signal>({
    Signal: '',
    Entry: '',
    SL: '',
    TP: '',
    Pair: 'EURUSD',
    Strategy: 'Manual',
    Timestamp: new Date().toISOString()
  });
  const [response, setResponse] = useState('');
  const [refinedData, setRefinedData] = useState<RefinedData | null>(null);
  const [cards, setCards] = useState<string[]>([]);
  const [copySuccess, setCopySuccess] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    try {
      const signalData = {
        ...signal,
        Timestamp: new Date().toISOString()
      };

      const requestData = [signalData];

      const res = await api.post('/optimise/', requestData);

      const data = res.data;

      // Handle the response format shown in your console
      if (data.results && Array.isArray(data.results) && data.results.length > 0) {
        const result = data.results[0];
        setResponse(result.original);

        // Extract JSON from the refined string
        if (result.refined) {
          try {
            // Remove the markdown code block markers
            const jsonString = result.refined
              .replace(/```json/g, '')
              .replace(/```/g, '')
              .trim();

            const parsedData = JSON.parse(jsonString);
            setRefinedData(parsedData);
          } catch (parseError) {
            console.error("Failed to parse refined data:", parseError);
            // Fallback: show the raw refined string if parsing fails
            setRefinedData({
              refinement_suggestions: [result.refined]
            });
          }
        }
      } else {
        setResponse("Received unexpected response format");
      }
    } catch (error) {
      console.error("Request failed:", error);
      setResponse("Error: " + (error instanceof Error ? error.message : "Unknown error"));
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value } = e.target;
    setSignal(prev => ({
      ...prev,
      [name]: value
    }));
  };

  const handleCopy = async () => {
    await navigator.clipboard.writeText(response);
    setCopySuccess('Copied!');
    setTimeout(() => setCopySuccess(''), 1500);
  };

  useEffect(() => {
    api.get('/optimalTime/')
      .then((res) => setCards(res.data.cards || []))
      .catch((err) => console.error('Failed to fetch optimal trade times:', err));
  }, []);

  return (
    <ProtectedRoute>
      <Header />
      <div className="bg-white min-fit flex flex-col items-center justify-center py-16 px-4">

        <h1 className="text-6xl font-bold mb-6 trading mt-[15rem] text-center">Optimize Your Signals <br></br> with AI Insights</h1>
        <p className='lg:w-[50%] w-full text-gray-600 text-center mt-4 mb-8'>
          Take your trading strategy to the next level. Enter your signal and let our AI provide refined suggestions.
        </p>

        <div className='lg:w-[80%] w-full bg-gray-100 rounded-lg gap-8 border-2 flex flex-col items-center justify-center shadow-m mt-4 p-8'>
          <form onSubmit={handleSubmit} className="w-full max-w-2xl space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Signal Type</label>
                <input
                  type="text"
                  name="Signal"
                  className="w-full p-2 border border-gray-200 rounded border border-blue-600/20 h-12"
                  placeholder="e.g., Buy, Sell"
                  value={signal.Signal}
                  onChange={handleInputChange}
                  required
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Entry Price</label>
                <input
                  type="text"
                  name="Entry"
                  className="w-full p-2 border border-gray-300 rounded border border-blue-600/20 h-12"
                  placeholder="e.g., 1.0850"
                  value={signal.Entry}
                  onChange={handleInputChange}
                  required
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Stop Loss (SL)</label>
                <input
                  type="text"
                  name="SL"
                  className="w-full p-2 border border-gray-300 rounded border border-blue-600/20 h-12"
                  placeholder="e.g., 1.0800"
                  value={signal.SL}
                  onChange={handleInputChange}
                  required
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Take Profit (TP)</label>
                <input
                  type="text"
                  name="TP"
                  className="w-full p-2 border border-gray-300 rounded border border-blue-600/20 h-12"
                  placeholder="e.g., 1.0900"
                  value={signal.TP}
                  onChange={handleInputChange}
                  required
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Currency Pair</label>
                <select
                  name="Pair"
                  className="w-full p-2 border border-gray-300 rounded border border-blue-600/20 h-12"
                  value={signal.Pair}
                  onChange={handleInputChange}
                >

                  <option value="EURUSD">EUR/USD</option>
                  <option value="GBPUSD">GBP/USD</option>
                  <option value="XAUUSD">XAU/USD</option>
                  <option value="USDJPY">USD/JPY</option>
                  <option value="NAS100">NAS100</option>
                  <option value="US500">US500</option>
                  <option value="US30">US30</option>
                  <option value="XTIUSD">XTIUSD</option>
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Strategy</label>
                <select
                  name="Strategy"
                  className="w-full p-2 border border-gray-300 rounded border border-blue-600/20 h-12"
                  value={signal.Strategy}
                  onChange={handleInputChange}
                >
                  <option value="Manual">Manual</option>
                  <option value="Malysian">Malysian</option>
                  <option value="moving averages">Moving Averages</option>
                </select>
              </div>
            </div>

            <button
              type="submit"
              className="w-full bg-blue-600 text-white text-xl font-bold px-4 py-3 rounded hover:bg-blue-700 transition-colors"
            >
              Optimize Signal
            </button>
          </form>

          {response && (
            <div className="w-full max-w-2xl bg-white p-6 rounded-lg shadow-md relative mt-6">
              <h3 className="text-lg font-semibold mb-2">Original Signal:</h3>
              <p className="mb-4 p-2 bg-gray-100 rounded">
                {response}
              </p>

              {refinedData && (
                <>
                  {/* Signal Quality */}
                  {refinedData.signal_quality !== undefined && (
                    <div className="mb-4 p-4 bg-blue-50 rounded">
                      <div className="flex items-center space-x-4">
                        <div className="flex items-center">
                          <span className="font-medium mr-2">Signal Quality:</span>
                          <div className="flex">
                            {[...Array(5)].map((_, i) => (
                              <svg
                                key={i}
                                className={`w-5 h-5 ${i < (refinedData.signal_quality || 0) ? 'text-yellow-400' : 'text-gray-300'}`}
                                fill="currentColor"
                                viewBox="0 0 20 20"
                              >
                                <path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z" />
                              </svg>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Suggested Changes */}
                  <div className="mb-4 p-4 bg-green-50 rounded border border-green-100">
                    <h4 className="font-medium text-lg mb-2 text-green-800">Suggested Changes:</h4>
                    {refinedData.risk_reward_evaluation?.recommended_stop_loss !== undefined && (
                      <p className="mb-1">
                        <span className="font-medium">Stop Loss:</span> From {signal.SL} to {refinedData.risk_reward_evaluation.recommended_stop_loss}
                      </p>
                    )}
                    {refinedData.risk_reward_evaluation?.recommended_take_profit !== undefined && (
                      <p className="mb-1">
                        <span className="font-medium">Take Profit:</span> From {signal.TP} to {refinedData.risk_reward_evaluation.recommended_take_profit}
                      </p>
                    )}
                    {refinedData.risk_reward_evaluation?.suggested_position_size && (
                      <p>
                        <span className="font-medium">Position Size:</span> {refinedData.risk_reward_evaluation.suggested_position_size}
                      </p>
                    )}
                  </div>

                  {/* Refinement Suggestions */}
                  {refinedData.refinement_suggestions && refinedData.refinement_suggestions.length > 0 && (
                    <div className="mb-4">
                      <h4 className="font-medium text-lg mb-2">Refinement Suggestions:</h4>
                      <ul className="space-y-2">
                        {refinedData.refinement_suggestions.map((suggestion, index) => (
                          <li key={index} className="flex items-start">
                            <span className="text-blue-500 mr-2">•</span>
                            <span>{suggestion}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Market Context */}
                  {(refinedData.market_context?.volatility_impact || refinedData.market_context?.economic_events_to_watch) && (
                    <div className="grid grid-cols-2 gap-4">
                      {refinedData.market_context?.volatility_impact && (
                        <div className="p-3 bg-gray-50 rounded">
                          <h4 className="font-medium mb-1">Market Volatility</h4>
                          <p className="text-sm">{refinedData.market_context.volatility_impact}</p>
                        </div>
                      )}
                      {refinedData.market_context?.economic_events_to_watch && (
                        <div className="p-3 bg-gray-50 rounded">
                          <h4 className="font-medium mb-1">Economic Events</h4>
                          <p className="text-sm">{refinedData.market_context.economic_events_to_watch}</p>
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}

              <button
                onClick={handleCopy}
                className="absolute top-4 right-4 text-sm text-blue-600 hover:text-blue-800"
              >
                {copySuccess || 'Copy'}
              </button>
            </div>
          )}
        </div>

        <div className="w-full overflow-x-auto mt-8">
          <div className="flex gap-4 px-4 pb-4">
            {cards.map((card, index) => (
              <div
                key={index}
                className="min-w-[200px] p-4 bg-blue-100 rounded shadow-sm flex-shrink-0"
              >
                {card}
              </div>
            ))}
          </div>
        </div>

      </div>
      <Footer />
    </ProtectedRoute>
  );
}