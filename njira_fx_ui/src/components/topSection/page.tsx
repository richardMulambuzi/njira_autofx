"use client";
import "./top.css";
import React, { useState, useEffect } from "react";
import Login from "../login/page";
import { getCookie } from "cookies-next";

/**
 * TopSection Component
 *
 * The primary hero section for the application's landing page. This component displays
 * a compelling headline with animated elements, a call-to-action button, and conditionally
 * renders a login modal when needed.
 *
 * Features:
 * - Responsive text animations using Anime.js
 * - Conditional rendering based on authentication status
 * - Auto-login handling via URL parameters
 * - Modal-based login interface
 * - Smart button that adapts based on authentication state
 *
 * @returns {JSX.Element} The hero section with animated text and login functionality
 */
export default function TopSection() {
  /**
   * Controls the visibility of the login modal
   *
   * @state {boolean} showLoginModal - True when the login modal should be visible
   * @setState {Function} setShowLoginModal - Updates the login modal visibility state
   */
  const [showLoginModal, setShowLoginModal] = useState(false);

  /**
   * Handles auto-login from URL parameters
   *
   * This effect runs once when the component mounts and:
   * 1. Checks for an 'autoLogin' URL parameter
   * 2. Opens the login modal if the parameter exists and the user is not logged in
   * 3. Redirects to dashboard if the parameter exists and user is already logged in
   *
   * Use case: Allows deep linking that triggers the login flow automatically
   *
   * @effect
   * @dependencies {Array} Empty array ensures this only runs once on mount
   */
  useEffect(() => {
    // Extract URL parameters from the current location
    const query = new URLSearchParams(window.location.search);
    const autoLogin = query.get("autoLogin");

    if (autoLogin && !token) {
      setShowLoginModal(true); // Simulates opening the login modal
    } else if (autoLogin && token) {
      window.location.href = "/dashboard"; // Redirect to dashboard if already logged in
    }
  }, []);

  /**
   * Sets up animated visual elements using Anime.js
   *
   * This effect runs once when the component mounts and configures
   * three separate animations targeting different elements:
   * 1. Normal direction animation that moves right and fades
   * 2. Reverse direction animation that moves left and fades
   * 3. Alternate direction animation with different movement parameters
   *
   * All animations use the same easing function and alternate direction
   * to create a breathing, living feel to the interface.
   *
   * @effect
   * @dependencies {Array} Empty array ensures this only runs once on mount
   */
  useEffect(() => {
    // Animation support for newer animejs version
    // TODO: Update animations to use animejs 4.x API
    // const animateElements = () => {
    //   animate({
    //     targets: ".dir-normal",
    //     translateX: 500,
    //     opacity: [1, 0.3],
    //     direction: "alternate",
    //     easing: "easeInOutSine",
    //   });
    // };
    // animateElements();
  }, []);

  /**
   * Authentication token retrieved from browser cookies
   *
   * Used to determine if the user is currently logged in, which affects:
   * - The text and behavior of the main CTA button
   * - Whether to show login modal or redirect to dashboard
   * - Automatic handling of the 'autoLogin' URL parameter
   *
   * @constant {string|undefined} token - The user's authentication token or undefined if not logged in
   */
  const token = getCookie("userToken");

  /**
   * Handles the opening of the login modal
   *
   * This function:
   * - Checks if the user is already logged in via the token
   * - If logged in, redirects directly to the dashboard
   * - If not logged in, displays the login modal
   *
   * This provides a seamless UX by avoiding the login form for authenticated users.
   */
  const openLoginModal = () => {
    if (token) {
      window.location.href = "/dashboard";
    } else {
      setShowLoginModal(true);
    }
  };

  /**
   * Closes the login modal
   *
   * Sets the showLoginModal state to false, which triggers
   * the modal to be removed from the DOM via conditional rendering.
   */
  const closeLoginModal = () => {
    setShowLoginModal(false);
  };

  /**
   * Handles the dashboard button click
   *
   * Functions identically to openLoginModal, but named differently to better
   * reflect its context when used with the dashboard button.
   *
   * This function:
   * - Redirects authenticated users directly to the dashboard
   * - Shows the login modal for unauthenticated users
   */
  const handleDashboardClick = () => {
    if (token) {
      window.location.href = "/dashboard";
    } else {
      setShowLoginModal(true);
    }
  };

  return (
    <>
      {/*
       * Main Hero Section Container
       *
       * Wraps the entire top section with centering alignment.
       * The 'topsection' class defines background styling and dimensions.
       */}
      <div className="items-center flex flex-col justify-center w-full topsection">
        {/*
         * Inner Content Container
         *
         * Provides additional positioning and styling for the hero content.
         * Centers all content both horizontally and vertically.
         */}
        <div className="w-full items-center flex flex-col justify-center inner-div">
          {/*
           * Text Content Container
           *
           * Contains the headline, subheading text, and call-to-action button.
           * Arranged in a vertical column with center alignment.
           */}
          <div className="h-fit flex items-center justify-center flex-col text-div">
            {/*
             * Animated Headline
             *
             * The main h1 heading with the 'dir-normal' class for animation.
             * The word "AUTOMATED" is styled differently for emphasis.
             */}
            <h1 className="text-center dir-normal">
              TRADE FOREX WITH <strong className="automated">AUTOMATED</strong>{" "}
              PRECISION
            </h1>

            {/*
             * Subtitle Text
             *
             * Descriptive paragraph explaining the product's value proposition.
             * Styled with reduced opacity for visual hierarchy.
             */}
            <p className="text-m text-white opacity-70 text-center">
              Harness the power of algorithmic trading. Let our system trade for
              you while you watch your portfolio grow, 24/7.
            </p>

            {/*
             * Call-to-Action Button Container
             *
             * Contains either a "Your dashboard" or "Start trading" button
             * depending on the user's authentication status.
             * Has animation classes applied.
             */}
            <div className="w-full flex justify-center mt-20 top-links dir-alternate dir-reverse">
              {/*
               * Conditional Button Rendering
               *
               * Shows different button text and behavior based on whether
               * the user is logged in (has token) or not.
               */}
              {token ? (
                <a
                  className="links-in-top flex flex-row justify-center items-center inside-utton"
                  onClick={handleDashboardClick}
                >
                  Your dashboard &gt;
                </a>
              ) : (
                <button
                  className="links-in-top flex flex-row justify-center items-center inside-utton"
                  onClick={openLoginModal}
                >
                  Start trading with us &gt;
                </button>
              )}
            </div>
          </div>
        </div>

        {/*
         * Login Modal (Conditionally Rendered)
         *
         * Only appears when showLoginModal is true.
         * Consists of:
         * - A semi-transparent backdrop overlay
         * - A white modal container
         * - A close button in the top-right corner
         * - The Login component imported from ../login/page
         */}
        {showLoginModal && (
          <div className="fixed inset-0 bg-black bg-opacity-50 backdrop-blur-sm flex items-center justify-center z-50">
            <div className="relative bg-white rounded-lg shadow-lg w-full max-w-md p-6">
              <button
                className="absolute top-4 right-4 bg-red-500 text-white rounded-full w-8 h-8 flex items-center justify-center hover:bg-red-600 transition"
                onClick={closeLoginModal}
              >
                &times;
              </button>
              <Login />
            </div>
          </div>
        )}
      </div>
    </>
  );
}
