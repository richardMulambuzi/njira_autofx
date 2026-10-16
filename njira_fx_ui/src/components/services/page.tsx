import React from 'react';
import {
    AiOutlineLineChart,
    AiOutlineRobot,
    AiOutlineBulb,
    AiOutlineRight
} from 'react-icons/ai';
import {
    FiShield,
    FiPieChart,
    FiDollarSign,
    FiHelpCircle,
    FiBookOpen,
    FiPhone,
    FiLock,
    FiUsers
} from 'react-icons/fi';
import '../../app/globals.css';
import Link from 'next/link';

export default function Services() {
    return (
        <div className="flex flex-col items-center justify-center pt-20 mb-20 w-full px-4 sm:px-6 lg:px-8">
            {/* Image and Text Section */}
            <div className="flex flex-col lg:flex-row items-center justify-center gap-8 mb-10 w-full lg:w-[90%]">
                {/* Image Slot - Full width on mobile, half on desktop */}
                <div className="w-full lg:w-[50%] mb-8 lg:mb-0">
                    <img
                        src="https://news.store.rambler.ru/img/dbcbbad4e514c45bbe20f130be3f8cac?img-format=auto&img-1-resize=height:350,fit:max&img-2-filter=sharpen"
                        alt="Trading Platform Overview"
                        className="rounded-lg w-full h-auto"
                    />
                </div>

                {/* New Features Section - Full width on mobile, half on desktop */}
                <div className="w-full w-[50%]">
                    <h3 className="text-2xl font-bold mb-4">
                        New <span className="text-sky-500">Features</span>
                    </h3>
                    <p className="text-gray-600">
                        Explore our enhanced trading platform with integrated wallets, exchanges, and essential market insights.
                    </p>
                    <br />
                    <div className="flex flex-col md:flex-row items-start">
                        {/* Left Section */}
                        <div className="w-full md:w-1/2 md:pr-4 mb-6 md:mb-0">
                            <h4 className="text-lg font-semibold mb-2">Automated Indices Trading</h4>
                            <p className="text-gray-600">
                                Enjoy automated trading on selected indices.
                            </p>
                            <br />
                            <Link href="/producttour#newfeatures-expand" className="mt-4 px-4 py-2 bg-sky-500 text-white rounded-lg hover:bg-sky-700 transition inline-block">
                                Learn More
                            </Link>
                        </div>

                        {/* Shorter Vertical Divider - Hidden on mobile */}
                        <div className="hidden md:block w-px bg-gray-300 h-20 mx-4"></div>

                        {/* Right Section */}
                        <div className="w-full md:w-1/2 md:pl-4">
                            <h4 className="text-lg font-semibold mb-2">Automated Currency Trading</h4>
                            <p className="text-gray-600">
                                Trade currency pairs effortlessly with our automated system now!
                            </p>
                        </div>
                    </div>
                </div>
            </div>

            {/* Title */}
            <h2 className="text-3xl font-bold mb-10 text-center">
                Fx<span className="text-sky-500">Auto</span> Key Features
            </h2>

            {/* Key Features Grid */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 justify-center gap-6  w-full  lg:w-[90%]">
                {/* Automated Trading */}
                <Link href="/" className="flex flex-col p-6 bg-white rounded-lg border border-sky-400 hover:shadow-lg transition">
                    <AiOutlineRobot size={50} className="text-sky-500" />
                    <h2 className="mt-4 text-xl font-semibold">Automated Trading</h2>
                    <p className="mt-4 text-gray-600">
                        Our trading app is fully automated, designed to place trades on your behalf based on deep market analysis.
                    </p>
                </Link>

                {/* Real-time Analytics and Reporting */}
                <Link href="/" className="flex flex-col p-6 bg-white rounded-lg border border-sky-400 hover:shadow-lg transition">
                    <FiPieChart size={50} className="text-sky-500" />
                    <h2 className="mt-4 text-xl font-semibold">Real-time Analytics and Reporting</h2>
                    <p className="mt-4 text-gray-600">
                        Get access to real-time market insights, analytics, and trade execution reports.
                    </p>
                </Link>

                {/* Risk Management Tools */}
                <Link href="/" className="flex flex-col p-6 bg-white rounded-lg border border-sky-400 hover:shadow-lg transition">
                    <FiShield size={50} className="text-sky-500" />
                    <h2 className="mt-4 text-xl font-semibold">Risk Management Tools</h2>
                    <p className="mt-4 text-gray-600">
                        We prioritize capital protection with robust risk management systems.
                    </p>
                </Link>

                {/* Profit and Loss Tracking */}
                <Link href="/" className="flex flex-col p-6 bg-white rounded-lg border border-sky-400 hover:shadow-lg transition">
                    <AiOutlineLineChart size={50} className="text-sky-500" />
                    <h2 className="mt-4 text-xl font-semibold">Profit and Loss Tracking</h2>
                    <p className="mt-4 text-gray-600">
                        Our platform provides detailed insights into your trading performance.
                    </p>
                </Link>

                {/* Multi-Asset Trading Support */}
                <Link href="/" className="flex flex-col p-6 bg-white rounded-lg border border-sky-400 hover:shadow-lg transition">
                    <FiDollarSign size={50} className="text-sky-500" />
                    <h2 className="mt-4 text-xl font-semibold">Multi-Asset Trading Support</h2>
                    <p className="mt-4 text-gray-600">
                        Trade across multiple asset classes, including forex, stocks, indices, and commodities.
                    </p>
                </Link>

                {/* AI-Powered Market Predictions */}
                <Link href="/producttour#aipredictions-expand" className="flex flex-col p-6 bg-white rounded-lg border border-sky-400 hover:shadow-lg transition">
                    <AiOutlineBulb size={50} className="text-sky-500" />
                    <h2 className="mt-4 text-xl font-semibold">AI-Powered Market Predictions</h2>
                    <p className="mt-4 text-gray-600">
                        Leverage AI-driven market forecasts to make informed trading decisions.
                    </p>
                </Link>
            </div>
            <br></br>
            <br></br>
            <br></br>

            {/* Need Help Section */}
            <div className="mt-16 w-full md:w-[90%]">
                <h3 className="text-2xl font-bold mb-6 text-center">
                    Need <span className="text-sky-500">Help?</span>
                </h3>
                <div className="flex flex-col md:flex-row justify-center gap-8 p-6 bg-white">
                    {/* Phone Support */}
                    <div className="flex flex-col sm:flex-row items-start w-full sm:w-64 mb-8 sm:mb-0">
                        <FiPhone size={80} className="text-sky-500 mr-4 mb-4 sm:mb-0" />
                        <div>
                            <h4 className="text-lg font-semibold">24/7 Phone Support</h4>
                            <p className="mt-2 text-gray-600 text-sm">
                                Contact our customer service team anytime for immediate assistance.
                            </p>
                            <a href="/contactus#chatnow" className="flex items-center mt-4 text-sky-500 hover:text-sky-700">
                                Contact Now <AiOutlineRight className="ml-1" />
                            </a>
                        </div>
                    </div>

                    {/* FAQs */}
                    <div className="flex flex-col sm:flex-row items-start w-full sm:w-64 mb-8 sm:mb-0">
                        <FiHelpCircle size={80} className="text-sky-500 mr-4 mb-4 sm:mb-0" />
                        <div>
                            <h4 className="text-lg font-semibold">FAQs</h4>
                            <p className="mt-2 text-gray-600 text-sm">
                                View FAQs for detailed instructions on specific features.
                            </p>
                            <a href="/contactus#faqs" className="flex items-center mt-4 text-sky-500 hover:text-sky-700">
                                Learn More <AiOutlineRight className="ml-1" />
                            </a>
                        </div>
                    </div>

                    {/* Blog */}
                    <div className="flex flex-col sm:flex-row items-start w-full sm:w-64">
                        <FiBookOpen size={80} className="text-sky-500 mr-4 mb-4 sm:mb-0" />
                        <div>
                            <h4 className="text-lg font-semibold">Product Tour</h4>
                            <p className="mt-2 text-gray-600 text-sm">
                                Stay informed with the latest updates and developments in FxAuto.
                            </p>
                            <a href="/producttour" className="flex items-center mt-4 text-sky-500 hover:text-sky-700">
                                Learn More <AiOutlineRight className="ml-1" />
                            </a>
                        </div>
                    </div>
                </div>
            </div>

            {/* Trusted Crypto Exchange Section */}
            <div className="mt-16 w-full lg:w-[90%]">
                <div className="flex flex-col lg:flex-row items-center justify-center gap-8">
                    {/* Left Side - Text Content */}
                    <div className="w-full lg:w-1/2 order-2 lg:order-1">
                        <h3 className="text-2xl font-bold mb-4">
                            Your <span className="text-sky-500">trusted</span> forex bot
                        </h3>
                        <p className="text-gray-600 mb-8">
                            Here at FxAuto, we are committed to user protection with strict protocol and industry-leading technical measures.
                        </p>

                        {/* Three Feature Blocks with Icons on Left */}
                        <div className="space-y-6">
                            {/* Secure Asset Fund */}
                            <div className="flex items-start gap-4">
                                <FiDollarSign size={24} className="text-sky-500 mt-1 flex-shrink-0" />
                                <div className="border-l-4 border-sky-500 pl-4 w-full">
                                    <h4 className="text-lg font-semibold pb-2 border-b border-gray-200">Secure Asset Fund for Users (SAFU)</h4>
                                    <p className="mt-2 text-gray-600 text-sm">
                                        Our trading bot allocates 10% of platform fees to a secure asset fund, providing an additional layer of protection for user funds.
                                    </p>
                                    <a href="/producttour#safu-expand" className="flex items-center mt-4 text-sky-500 hover:text-sky-700">
                                        Learn More <AiOutlineRight className="ml-1" />
                                    </a>
                                </div>
                            </div>

                            {/* Personalized Access Control */}
                            <div className="flex items-start gap-4">
                                <FiLock size={24} className="text-sky-500 mt-1 flex-shrink-0" />
                                <div className="border-l-4 border-sky-500 pl-4 w-full">
                                    <h4 className="text-lg font-semibold pb-2 border-b border-gray-200">Personalized Access Control</h4>
                                    <p className="mt-2 text-gray-600 text-sm">
                                        Advanced security features allow you to restrict device and IP address access, ensuring only authorized users can control your trading bot.
                                    </p>
                                    <a href="/producttour#accesscontrol-expand" className="flex items-center mt-4 text-sky-500 hover:text-sky-700">
                                        Learn More <AiOutlineRight className="ml-1" />
                                    </a>
                                </div>
                            </div>

                            {/* Secure Asset Fund for All */}
                            <div className="flex items-start gap-4">
                                <FiUsers size={24} className="text-sky-500 mt-1 flex-shrink-0" />
                                <div className="border-l-4 border-sky-500 pl-4 w-full">
                                    <h4 className="text-lg font-semibold pb-2 border-b border-gray-200">End-to-End Encryption</h4>
                                    <p className="mt-2 text-gray-600 text-sm">
                                        Your transaction data is safeguarded with industry-leading encryption, ensuring complete privacy and security for all your trading activities.
                                    </p>
                                    <a href="/producttour#endtoend-expand" className="flex items-center mt-4 text-sky-500 hover:text-sky-700">
                                        Learn More <AiOutlineRight className="ml-1" />
                                    </a>
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* Right Side - Image */}
                    <div className="w-full lg:w-1/2 order-1 lg:order-2 mb-8 lg:mb-0">
                        <img
                            src="https://www.fingramota.by/files/images/2015/7/2/635714311701708846.jpg"
                            alt="Secure Crypto Exchange"
                            className="w-full h-auto rounded-lg"
                        />
                    </div>
                </div>
            </div>
        </div>
    );
}