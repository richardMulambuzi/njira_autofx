import "../../app/globals.css";

/**
 * Footer Component
 *
 * Renders the application's footer section with contact information,
 * quick navigation links, service descriptions, and a newsletter subscription form.
 *
 * The footer is organized into a responsive grid layout with four columns on desktop
 * that collapse to a single column on mobile devices for better readability.
 *
 * Features:
 * - Contact information with physical address
 * - Quick links to key site pages
 * - List of services provided by the platform
 * - Email subscription form for updates
 *
 * @returns {JSX.Element} The footer section with all content sections
 */
const Footer = () => {
  /**
   * Renders the complete footer with four information sections
   *
   * The grid layout uses:
   * - Full-width single column on mobile (<768px)
   * - Four-column layout on tablet and desktop (≥768px)
   * - Maximum width constraint with horizontal centering
   * - Consistent spacing between columns
   */
  return (
    <footer className="bg-darkBlue text-white py-12 px-4">
      <div className="grid md:grid-cols-4 gap-8 max-w-screen-xl mx-auto">
        {/*
         * Contact Information Section
         *
         * Displays the organization's physical address information.
         * This helps establish legitimacy and provides users with
         * a way to reach the company through traditional means.
         */}
        <div>
          <h4 className="font-semibold">Contact information</h4>
          <ul className="text-gray-400 mt-2 space-y-1">
            <li>University Of Malawi</li>
            <li>P.O Box 280</li>
            <li>Zomba</li>
            <li>Malawi</li>
          </ul>
        </div>

        {/*
         * Quick Links Section
         *
         * Provides direct navigation links to important pages on the site.
         * These links help users quickly access key information and features
         * without having to search or use the main navigation.
         *
         * Each link has a hover state that changes text to white for better visibility.
         */}
        <div>
          <h4 className="font-semibold">Quick Links</h4>
          <ul className="text-gray-400 mt-2 space-y-1">
            <li>
              <a href="/aboutus" className="hover:text-white">
                About
              </a>
            </li>
            <li>
              <a href="/charts" className="hover:text-white">
                Charts
              </a>
            </li>
            <li>
              <a href="/contactus" className="hover:text-white">
                Contact us
              </a>
            </li>
            <li>
              <a href="/login" className="hover:text-white">
                Start trading with us
              </a>
            </li>
          </ul>
        </div>

        {/*
         * Services Information Section
         *
         * Highlights the main services offered by the platform.
         * This section reinforces the value proposition and reminds
         * users of the core offerings available through the application.
         */}
        <div>
          <h4 className="font-semibold">Our Services</h4>
          <ul className="text-gray-400 mt-2 space-y-1">
            <li>Automated Trading </li>
            <li>AI Market Analysis</li>
            <li>Risk Management</li>
            <li>Secure Execution</li>
          </ul>
        </div>

        {/*
         * Newsletter Subscription Section
         *
         * Allows users to subscribe for email updates about trades and market trends.
         * Features a simple form with email input field and subscription button.
         *
         * Note: Currently this is a UI-only implementation without backend integration.
         * To fully implement this feature, you would need to:
         * 1. Add state management for the email input
         * 2. Add form submission handling
         * 3. Connect to a newsletter API or backend endpoint
         */}
        <div className="md:pl-4">
          <h4 className="font-semibold">Get Latest Update</h4>
          <p className="text-gray-400 mt-2">
            Get real-time updates on your trades and market trends.
          </p>
          <div className="mt-4 flex">
            <input
              type="email"
              placeholder="Enter Your Email"
              className="p-2 w-full border-none text-black rounded-l-md text-sm"
            />
            <button className="bg-sky-500 px-4 py-2 rounded-r-md">
              Subscribe
            </button>
          </div>
        </div>
      </div>
    </footer>
  );
};

export default Footer;
