"use client";

import { useState, useEffect } from "react";
import Header from "../../components/header/page";
import Footer from "../../components/footer/page";
import ProtectedRoute from "../../components/auth/ProtectedRoute";

/**
 * ProductTour Component
 *
 * Renders a feature tour page showcasing different aspects of the FxAuto trading platform.
 * Includes expandable sections, URL hash-based navigation, and smooth scrolling functionality.
 *
 * Features displayed include:
 * - New platform features
 * - AI-powered predictions
 * - Security measures
 * - Access control features
 * - End-to-end encryption
 */
export default function ProductTour() {
  /**
   * State to track which sections are currently expanded
   * Uses a Record with section identifiers as keys and boolean values
   * indicating whether each section is expanded
   */
  const [expandedSections, setExpandedSections] = useState<
    Record<string, boolean>
  >({
    newFeatures: false,
    automatedIndices: false,
    automatedCurrency: false,
    aiPredictions: false,
    phoneSupport: false,
    faqs: false,
    productTour: false,
    safu: false,
    accessControl: false,
    encryption: false,
  });

  /**
   * Effect hook to handle URL hash-based navigation
   *
   * When component mounts or URL hash changes:
   * 1. Checks if the hash corresponds to an expandable section
   * 2. Expands that section if found
   * 3. Smoothly scrolls to the target element
   * 4. Cleans up URL by removing "-expand" suffix
   */
  useEffect(() => {
    // Check URL hash when component mounts
    const handleHash = () => {
      const hash = window.location.hash;
      // Mapping of URL hashes to section state keys
      const sectionMap: Record<string, string> = {
        "#newfeatures-expand": "newFeatures",
        "#aipredictions-expand": "aiPredictions",
        "#phonesupport-expand": "phoneSupport",
        "#faqs-expand": "faqs",
        "#safu-expand": "safu",
        "#accesscontrol-expand": "accessControl",
        "#endtoend-expand": "encryption",
      };

      if (hash in sectionMap) {
        // Expand the section corresponding to the hash
        const section = sectionMap[hash];
        setExpandedSections((prev) => ({
          ...prev,
          [section]: true,
        }));

        // Scroll to the section after a slight delay to allow expansion
        setTimeout(() => {
          // Extract section ID by removing "-expand" suffix
          const element = document.getElementById(
            hash.substring(1, hash.indexOf("-expand"))
          );
          if (element) {
            element.scrollIntoView({ behavior: "smooth" });
            // Clean up the URL by removing "-expand" suffix
            window.history.replaceState(
              null,
              "",
              `/producttour#${hash.substring(1, hash.indexOf("-expand"))}`
            );
          }
        }, 100);
      } else if (
        hash === "#newfeatures" ||
        hash === "#aipredictions" ||
        hash === "#phonesupport" ||
        hash === "#faqs" ||
        hash === "#safu" ||
        hash === "#accesscontrol" ||
        hash === "#endtoend"
      ) {
        // Just scroll to the section if the hash is present without "-expand"
        const element = document.getElementById(hash.substring(1));
        if (element) {
          element.scrollIntoView({ behavior: "smooth" });
        }
      }
    };

    // Execute hash handling on component mount
    handleHash();

    // Also handle hash changes if they occur while on the page
    const hashChangeHandler = () => {
      handleHash();
    };

    // Add event listener for URL hash changes
    window.addEventListener("hashchange", hashChangeHandler);

    // Clean up event listener when component unmounts
    return () => window.removeEventListener("hashchange", hashChangeHandler);
  }, []);

  /**
   * Toggles the expansion state of a specific section
   *
   * @param section - The identifier for the section to toggle
   */
  const toggleSection = (section: string) => {
    setExpandedSections((prev) => {
      const newState = !prev[section];
      // If expanding a section, scroll to it after a brief delay
      if (newState) {
        setTimeout(() => {
          const element = document.getElementById(getSectionId(section));
          if (element) {
            element.scrollIntoView({ behavior: "smooth" });
          }
        }, 100);
      }
      // Return updated state with toggled section
      return {
        ...prev,
        [section]: newState,
      };
    });
  };

  /**
   * Maps internal section identifiers to HTML element IDs
   *
   * @param section - The section identifier in camelCase
   * @returns The corresponding HTML element ID in lowercase
   */
  const getSectionId = (section: string): string => {
    const sectionMap: Record<string, string> = {
      newFeatures: "newfeatures",
      aiPredictions: "aipredictions",
      phoneSupport: "phonesupport",
      faqs: "faqs",
      safu: "safu",
      accessControl: "accesscontrol",
      encryption: "endtoend",
    };
    return sectionMap[section] || "";
  };

  return (
    <ProtectedRoute>
      <div className="font-sans text-gray-900 min-h-screen flex flex-col">
      {/* Header Component */}
      <Header />
      <br></br>
      <br></br>

      {/* Main Content */}
      <main className="flex-grow">
        {/* Hero Banner */}
        <header
          className="relative overflow-hidden bg-cover bg-center bg-no-repeat mb-8 text-center py-12 md:py-20 px-4"
          style={{
            backgroundImage:
              "url('https://pic.rutubelist.ru/video/72/65/726509244160c9cad6c8236c95379019.jpg')",
          }}
        >
          {/* Dark overlay to improve text readability */}
          <div className="absolute inset-0 bg-black bg-opacity-40"></div>

          {/* Hero Content */}
          <div className="relative z-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <h2 className="text-2xl sm:text-3xl md:text-4xl font-semibold text-white">
              <span className="underline decoration-sky-500">Discover</span>{" "}
              FxAuto: Smarter Automated Trading
            </h2>
            <p className="mt-3 text-gray-300 max-w-3xl mx-auto text-sm sm:text-base">
              Explore how FxAuto merges automation, AI, and top-tier security to
              redefine your trading success.
            </p>
          </div>
        </header>

        {/* Product Features Container */}
        <div className="max-w-6xl mx-auto space-y-8 pb-8 px-4 sm:px-6">
          {/* New Features Section */}
          <div
            id="newfeatures"
            className="bg-sky-50 rounded-xl p-4 sm:p-6 border border-sky-100"
          >
            {/* Section Header with Toggle */}
            <div
              className="flex justify-between items-center cursor-pointer"
              onClick={() => toggleSection("newFeatures")}
            >
              <h2 className="text-xl sm:text-2xl font-semibold text-sky-800">
                New Features
              </h2>
              {/* Animated arrow icon that rotates based on section expansion */}
              <svg
                className={`w-5 h-5 sm:w-6 sm:h-6 text-sky-600 transition-transform ${
                  expandedSections.newFeatures ? "rotate-180" : ""
                }`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                xmlns="http://www.w3.org/2000/svg"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M19 9l-7 7-7-7"
                />
              </svg>
            </div>

            {/* Section Summary */}
            <p className="mt-2 sm:mt-3 text-sm sm:text-base text-gray-700">
              Explore our enhanced trading platform with integrated wallets,
              exchanges, and essential market insights.
            </p>

            {/* Expanded Content - Only shown when section is expanded */}
            {expandedSections.newFeatures && (
              <div className="mt-4 sm:mt-6 space-y-4 sm:space-y-6">
                {/* Automated Indices Trading Subsection */}
                <div className="space-y-3 sm:space-y-4 pl-4 sm:pl-6 border-l-2 border-sky-500">
                  <h3 className="font-medium text-base sm:text-lg text-sky-700">
                    Automated Indices Trading
                  </h3>
                  <p className="text-sm sm:text-base text-gray-600">
                    Enjoy automated trading on selected indices.
                  </p>

                  {/* Feature List with Checkmarks */}
                  <div className="space-y-2 sm:space-y-3 ml-3 sm:ml-4">
                    <div className="flex items-start">
                      <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                        <svg
                          className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                          xmlns="http://www.w3.org/2000/svg"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M5 13l4 4L19 7"
                          />
                        </svg>
                      </div>
                      <p className="text-sm sm:text-base text-gray-700">
                        Trade major global indices including S&P 500, NASDAQ,
                        FTSE 100, and more
                      </p>
                    </div>
                    <div className="flex items-start">
                      <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                        <svg
                          className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                          xmlns="http://www.w3.org/2000/svg"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M5 13l4 4L19 7"
                          />
                        </svg>
                      </div>
                      <p className="text-sm sm:text-base text-gray-700">
                        24/5 automated trading aligned with global market hours
                      </p>
                    </div>
                    <div className="flex items-start">
                      <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                        <svg
                          className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                          xmlns="http://www.w3.org/2000/svg"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M5 13l4 4L19 7"
                          />
                        </svg>
                      </div>
                      <p className="text-sm sm:text-base text-gray-700">
                        Advanced risk management with automatic stop-loss and
                        take-profit
                      </p>
                    </div>

                    {/* Nested Toggle Button for Detailed Information */}
                    <button
                      onClick={() => toggleSection("automatedIndices")}
                      className="text-sky-600 hover:text-sky-500 text-xs sm:text-sm flex items-center"
                    >
                      {expandedSections.automatedIndices
                        ? "Show Less Details"
                        : "Show More Details"}
                      <svg
                        className={`w-3 h-3 sm:w-4 sm:h-4 ml-1 transition-transform ${
                          expandedSections.automatedIndices ? "rotate-180" : ""
                        }`}
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        xmlns="http://www.w3.org/2000/svg"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M19 9l-7 7-7-7"
                        />
                      </svg>
                    </button>

                    {/* Nested Expanded Content */}
                    {expandedSections.automatedIndices && (
                      <div className="ml-4 sm:ml-6 mt-1 sm:mt-2 space-y-1 sm:space-y-2 text-xs sm:text-sm text-gray-600">
                        <p>
                          • Algorithmic strategies optimized for index
                          volatility patterns
                        </p>
                        <p>
                          • Sector rotation detection for enhanced positioning
                        </p>
                        <p>
                          • Correlation matrix to diversify across uncorrelated
                          indices
                        </p>
                      </div>
                    )}
                  </div>
                </div>

                {/* Automated Currency Trading Subsection */}
                <div className="space-y-3 sm:space-y-4 pl-4 sm:pl-6 border-l-2 border-sky-500">
                  <h3 className="font-medium text-base sm:text-lg text-sky-700">
                    Automated Currency Trading
                  </h3>
                  <p className="text-sm sm:text-base text-gray-600">
                    Trade currency pairs effortlessly with our automated system
                    now!
                  </p>

                  {/* Feature List with Checkmarks */}
                  <div className="space-y-2 sm:space-y-3 ml-3 sm:ml-4">
                    <div className="flex items-start">
                      <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                        <svg
                          className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                          xmlns="http://www.w3.org/2000/svg"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M5 13l4 4L19 7"
                          />
                        </svg>
                      </div>
                      <p className="text-sm sm:text-base text-gray-700">
                        Access to 50+ major, minor and exotic currency pairs
                      </p>
                    </div>
                    <div className="flex items-start">
                      <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                        <svg
                          className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                          xmlns="http://www.w3.org/2000/svg"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M5 13l4 4L19 7"
                          />
                        </svg>
                      </div>
                      <p className="text-sm sm:text-base text-gray-700">
                        24/6 trading to capture all forex market sessions
                      </p>
                    </div>

                    {/* Nested Toggle Button for Detailed Information */}
                    <button
                      onClick={() => toggleSection("automatedCurrency")}
                      className="text-sky-600 hover:text-sky-500 text-xs sm:text-sm flex items-center"
                    >
                      {expandedSections.automatedCurrency
                        ? "Show Less Details"
                        : "Show More Details"}
                      <svg
                        className={`w-3 h-3 sm:w-4 sm:h-4 ml-1 transition-transform ${
                          expandedSections.automatedCurrency ? "rotate-180" : ""
                        }`}
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        xmlns="http://www.w3.org/2000/svg"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M19 9l-7 7-7-7"
                        />
                      </svg>
                    </button>

                    {/* Nested Expanded Content */}
                    {expandedSections.automatedCurrency && (
                      <div className="ml-4 sm:ml-6 mt-1 sm:mt-2 space-y-1 sm:space-y-2 text-xs sm:text-sm text-gray-600">
                        <p>
                          • Carry trade optimization based on interest rate
                          differentials
                        </p>
                        <p>• News sentiment analysis for fundamental trading</p>
                        <p>• Correlation hedging between correlated pairs</p>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* AI-Powered Market Predictions Section */}
          <div
            id="aipredictions"
            className="bg-sky-50 rounded-xl p-4 sm:p-6 border border-sky-100"
          >
            {/* Section Header with Toggle */}
            <div
              className="flex justify-between items-center cursor-pointer"
              onClick={() => toggleSection("aiPredictions")}
            >
              <h2 className="text-xl sm:text-2xl font-semibold text-sky-800">
                AI-Powered Market Predictions
              </h2>
              <svg
                className={`w-5 h-5 sm:w-6 sm:h-6 text-sky-600 transition-transform ${
                  expandedSections.aiPredictions ? "rotate-180" : ""
                }`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                xmlns="http://www.w3.org/2000/svg"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M19 9l-7 7-7-7"
                />
              </svg>
            </div>

            {/* Section Summary */}
            <p className="mt-2 sm:mt-3 text-sm sm:text-base text-gray-700">
              Leverage AI-driven market forecasts to make informed trading
              decisions.
            </p>

            {/* Expanded Content - Only shown when section is expanded */}
            {expandedSections.aiPredictions && (
              <div className="mt-4 sm:mt-6 space-y-3 sm:space-y-4 pl-4 sm:pl-6 border-l-2 border-sky-500">
                <div className="flex items-start">
                  <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                    <svg
                      className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <p className="text-sm sm:text-base text-gray-700">
                    Neural network models trained on 10+ years of market data
                  </p>
                </div>
                <div className="flex items-start">
                  <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                    <svg
                      className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <p className="text-sm sm:text-base text-gray-700">
                    Sentiment analysis scanning 1000+ news sources in real-time
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* Secure Asset Fund for Users (SAFU) Section */}
          <div
            id="safu"
            className="bg-sky-50 rounded-xl p-4 sm:p-6 border border-sky-100"
          >
            {/* Section Header with Toggle */}
            <div
              className="flex justify-between items-center cursor-pointer"
              onClick={() => toggleSection("safu")}
            >
              <h2 className="text-xl sm:text-2xl font-semibold text-sky-800">
                Secure Asset Fund for Users (SAFU)
              </h2>
              <svg
                className={`w-5 h-5 sm:w-6 sm:h-6 text-sky-600 transition-transform ${
                  expandedSections.safu ? "rotate-180" : ""
                }`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                xmlns="http://www.w3.org/2000/svg"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M19 9l-7 7-7-7"
                />
              </svg>
            </div>

            {/* Section Summary */}
            <p className="mt-2 sm:mt-3 text-sm sm:text-base text-gray-700">
              Our trading bot allocates 10% of platform fees to a secure asset
              fund, providing an additional layer of protection for user funds.
            </p>

            {/* Expanded Content - Only shown when section is expanded */}
            {expandedSections.safu && (
              <div className="mt-4 sm:mt-6 space-y-3 sm:space-y-4 pl-4 sm:pl-6 border-l-2 border-sky-500">
                <div className="flex items-start">
                  <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                    <svg
                      className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <p className="text-sm sm:text-base text-gray-700">
                    Funds held in cold storage with multi-signature access
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* Personalized Access Control Section */}
          <div
            id="accesscontrol"
            className="bg-sky-50 rounded-xl p-4 sm:p-6 border border-sky-100"
          >
            {/* Section Header with Toggle */}
            <div
              className="flex justify-between items-center cursor-pointer"
              onClick={() => toggleSection("accessControl")}
            >
              <h2 className="text-xl sm:text-2xl font-semibold text-sky-800">
                Personalized Access Control
              </h2>
              <svg
                className={`w-5 h-5 sm:w-6 sm:h-6 text-sky-600 transition-transform ${
                  expandedSections.accessControl ? "rotate-180" : ""
                }`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                xmlns="http://www.w3.org/2000/svg"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M19 9l-7 7-7-7"
                />
              </svg>
            </div>

            {/* Section Summary */}
            <p className="mt-2 sm:mt-3 text-sm sm:text-base text-gray-700">
              Advanced security features allow you to restrict device and IP
              address access, ensuring only authorized users can control your
              trading bot.
            </p>

            {/* Expanded Content - Only shown when section is expanded */}
            {expandedSections.accessControl && (
              <div className="mt-4 sm:mt-6 space-y-3 sm:space-y-4 pl-4 sm:pl-6 border-l-2 border-sky-500">
                <div className="flex items-start">
                  <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                    <svg
                      className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <p className="text-sm sm:text-base text-gray-700">
                    Device authorization with fingerprinting technology
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* End-to-End Encryption Section */}
          <div
            id="endtoend"
            className="bg-sky-50 rounded-xl p-4 sm:p-6 border border-sky-100"
          >
            {/* Section Header with Toggle */}
            <div
              className="flex justify-between items-center cursor-pointer"
              onClick={() => toggleSection("encryption")}
            >
              <h2 className="text-xl sm:text-2xl font-semibold text-sky-800">
                End-to-End Encryption
              </h2>
              <svg
                className={`w-5 h-5 sm:w-6 sm:h-6 text-sky-600 transition-transform ${
                  expandedSections.encryption ? "rotate-180" : ""
                }`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                xmlns="http://www.w3.org/2000/svg"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M19 9l-7 7-7-7"
                />
              </svg>
            </div>

            {/* Section Summary */}
            <p className="mt-2 sm:mt-3 text-sm sm:text-base text-gray-700">
              Your transaction data is safeguarded with industry-leading
              encryption, ensuring complete privacy and security for all your
              trading activities.
            </p>

            {/* Expanded Content - Only shown when section is expanded */}
            {expandedSections.encryption && (
              <div className="mt-4 sm:mt-6 space-y-3 sm:space-y-4 pl-4 sm:pl-6 border-l-2 border-sky-500">
                <div className="flex items-start">
                  <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                    <svg
                      className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <p className="text-sm sm:text-base text-gray-700">
                    AES-256 encryption for all data in transit and at rest
                  </p>
                </div>
                <div className="flex items-start">
                  <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                    <svg
                      className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <p className="text-sm sm:text-base text-gray-700">
                    Perfect forward secrecy for all communications
                  </p>
                </div>
                <div className="flex items-start">
                  <div className="bg-sky-500 rounded-full p-1 mr-2 sm:mr-3 mt-1 flex-shrink-0">
                    <svg
                      className="w-3 h-3 sm:w-4 sm:h-4 text-white"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <p className="text-sm sm:text-base text-gray-700">
                    Regular third-party security audits
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      </main>

      {/* Footer Component */}
      <Footer />
      </div>
    </ProtectedRoute>
  );
}
