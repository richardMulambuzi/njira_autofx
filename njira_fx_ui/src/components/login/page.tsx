"use client";
import React, { useState } from "react";

/**
 * Login Component
 *
 * A two-step authentication component that first prompts users to select their
 * broker, then presents a standard login form for the selected broker.
 *
 * The component handles:
 * 1. Broker selection using a dropdown menu
 * 2. Email/password form for the selected broker and admin login
 *
 * Note: the legacy broker OAuth flow was removed with the old broker backend.
 *
 * @returns {JSX.Element} The login interface with conditional rendering based on selection state
 */
export default function Login() {
  /**
   * State for tracking the selected broker
   *
   * Controls the flow and displayed UI:
   * - Empty string: Shows the broker selection dropdown
   * - Any other value: Shows the email/password login form
   *
   * @state {string} selectedBroker - The currently selected broker
   * @setState {Function} setSelectedBroker - Updates the selected broker state
   */
  const [selectedBroker, setSelectedBroker] = useState<string>(""); // State for selected broker

  /**
   * Updates the selected broker when user changes the dropdown selection
   *
   * This handler captures the selected value from the dropdown and updates
   * the component's state accordingly, triggering conditional rendering.
   *
   * @param {React.ChangeEvent<HTMLSelectElement>} event - The change event from the dropdown
   */
  const handleBrokerChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
    setSelectedBroker(event.target.value);
  };

  /**
   * Processes the login form submission
   *
   * This handler:
   * 1. Prevents the default form submission behavior
   * 2. Extracts the email and password from the form data
   * 3. Currently logs the values to console (placeholder for actual authentication)
   *
   * Note: This is a stub implementation that needs to be connected to an authentication service
   *
   * @param {React.FormEvent<HTMLFormElement>} event - The form submission event
   * @async - Marked async for future authentication API integration
   */
  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    // Extract form data using FormData API
    const formData = new FormData(event.currentTarget);
    const email = formData.get("email") as string;
    const password = formData.get("password") as string;

    // Currently logs credentials to console (placeholder for API call)
    console.log(email);
    console.log(password);

    // TODO: Implement actual authentication with API call
    // Example:
    // const response = await fetch('/api/login', {
    //   method: 'POST',
    //   headers: { 'Content-Type': 'application/json' },
    //   body: JSON.stringify({ email, password, broker: selectedBroker })
    // });
  };

  return (
    <div className="flex flex-col items-center justify-center w-full">
      <div className="w-full max-w-md">
        {/*
         * Broker Selection Section
         *
         * First step in the authentication flow.
         * Only displayed when no broker is selected yet.
         * Provides a dropdown of supported brokers and a Next button.
         */}
        {selectedBroker === "" && (
          <>
            <h1 className="text-2xl font-bold text-center mb-4">
              Choose Your Broker
            </h1>
            <select
              className="w-full p-3 mb-4 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              value={selectedBroker}
              onChange={handleBrokerChange}
              required
            >
              <option value="first">Select a broker</option>
              <option value="otherBroker">Other Broker</option>
              <option value="admin">Local Admin</option>
            </select>
            <button
              className="w-full p-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition"
              onClick={() => {
                // The Next button is a UI affordance; selecting a broker above
                // triggers the conditional login form below via state.
                if (selectedBroker !== "" && selectedBroker !== "first") {
                  // Proceed: the login form is rendered below once a broker is chosen.
                }
              }}
            >
              Next
            </button>
          </>
        )}

        {/*
         * Login Form Section
         *
         * Second step in the authentication flow.
         * Only displayed when a broker is selected.
         * Provides standard email/password fields for authentication.
         */}
        {selectedBroker && selectedBroker !== "first" && (
          <form
            className="w-full flex flex-col items-center"
            onSubmit={handleSubmit}
          >
            <h1 className="text-2xl font-bold text-center mb-4">Login</h1>
            {/*
             * Email Input
             *
             * Collects the user's email address with client-side validation:
             * - Required field (cannot be empty)
             * - Must be a valid email format (enforced by input type="email")
             */}
            <input
              className="w-full p-3 mb-4 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              type="email"
              id="email"
              name="email"
              placeholder="Your email"
              required
            />
            {/*
             * Password Input
             *
             * Collects the user's password with basic validation:
             * - Required field (cannot be empty)
             * - Masked for security (input type="password")
             */}
            <input
              className="w-full p-3 mb-4 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              type="password"
              id="password"
              name="password"
              placeholder="Your Password"
              required
            />
            {/*
             * Login Submit Button
             *
             * Submits the form data to the handleSubmit function.
             * Uses type="submit" to trigger form validation before submission.
             */}
            <button
              className="w-full p-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition"
              type="submit"
              id="submit"
              name="submit"
            >
              Login
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
