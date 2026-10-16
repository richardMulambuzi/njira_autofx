import React, { useState } from 'react';
import '../../app/globals.css';
import { getCookie } from 'cookies-next';
import Login from '../login/page';

export default function BeginTrading() {
    const [showChatbot, setShowChatbot] = useState(false);
    const closeChatbot = () => {
        setShowChatbot(false);
    };
    const handleDashboardClick = () => {
        if (token) {
            window.location.href = '/dashboard';
        } else {
            setShowChatbot(true);
        }
    };
    const token = getCookie('userToken');
    const openLogin = () => {
        if (token) {
            window.location.href = '/dashboard';
        } else {
            setShowChatbot(true);
        }
    };
    return (
        <div className="flex flex-col items-center justify-center w-full md:min-h-[60vh] h-fit">
            {/* Get Started Section with Background Image */}
            <div className="my-8 md:my-16 w-full py-12 md:py-20 px-4 bg-cover bg-center bg-no-repeat flex flex-col items-center justify-center">
                <h2 className="text-2xl sm:text-3xl md:text-4xl font-bold text-gray-600 mb-4 text-center">
                    Get <span className="text-sky-400">started</span> today with Fx<span className="text-sky-400">Auto</span>
                </h2>
                <p className="text-sm sm:text-base md:text-lg text-gray-600 text-center mb-6">
                    Open account and start trading instruments!
                </p>

                <div className='w-full max-w-xs sm:max-w-sm flex justify-center'>
                    {token ? (
                        <button 
                            className="links-in-top flex flex-row justify-center items-center inside-utton 
                                bg-sky-400 hover:bg-sky-500 text-white font-medium py-2 px-6 rounded-lg 
                                transition duration-300 ease-in-out transform hover:scale-105"
                            onClick={handleDashboardClick}
                        >
                            Your dashboard &gt;
                        </button>
                    ) : (
                        <button 
                            className="links-in-top flex flex-row justify-center items-center inside-utton 
                                bg-sky-400 hover:bg-sky-500 text-white font-medium py-2 px-6 rounded-lg 
                                transition duration-300 ease-in-out transform hover:scale-105"
                            onClick={openLogin}
                        >
                            Start trading with us &gt;
                        </button>
                    )}
                </div>
            </div>
            
            {/* Conditional rendering for the chatbot overlay */}
            {showChatbot && (
                <div className="fixed inset-0 bg-black bg-opacity-50 z-50 flex items-center justify-center p-4">
                    <div className="relative bg-white rounded-lg shadow-xl w-full max-w-md max-h-[90vh] overflow-y-auto">
                        <button 
                            className="absolute top-4 right-4 text-gray-500 hover:text-gray-700 text-2xl"
                            onClick={closeChatbot}
                        >
                            &times;
                        </button>
                        <Login />
                    </div>
                </div>
            )}
        </div>
    );
}