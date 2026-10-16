/**
 * Next.js Configuration
 *
 * This file contains configuration options for the Next.js application.
 * It defines server behavior, build options, and runtime settings.
 *
 * @see {@link https://nextjs.org/docs/api-reference/next.config.js/introduction} for official documentation
 */

/**
 * Next.js configuration object
 *
 * Contains various settings to customize the behavior of the Next.js application.
 * Currently configured with custom HTTP headers for API routes.
 */
module.exports = {
  /**
   * Custom HTTP Headers Configuration
   *
   * Defines HTTP headers that will be added to responses from the Next.js server.
   * This function is primarily used to set up Cross-Origin Resource Sharing (CORS)
   * policies that wjdetermine which domains can access the API endpoints.
   *
   * CORS is a security feature implemented by browsers that restricts web pages
   * from making requests to a different domain than the one that served the original page.
   *
   * @async
   * @returns {Array<Object>} An array of route-specific header configurations
   */
  async headers() {
    return [
      {
        /**
         * API Route Pattern
         *
         * This pattern matches all API routes in the application.
         * The ':path*' wildcard captures any path segments after '/api/'.
         */
        source: "/api/:path*",

        /**
         * Headers for API Routes
         *
         * These headers control cross-origin access to the API endpoints:
         */
        headers: [
          {
            /**
             * Access-Control-Allow-Origin
             *
             * Specifies which origins are allowed to access the API.
             * Currently set to the production frontend domain 'https://forex.xhed.net/'.
             * This restricts API access to only this specific domain for security.
             */
            key: "Access-Control-Allow-Origin",
            value: "https://forex.xhed.net/",
          },
          {
            /**
             * Access-Control-Allow-Methods
             *
             * Specifies which HTTP methods are allowed when accessing the API.
             * Currently allows GET, POST, and OPTIONS requests.
             * - GET: For retrieving data
             * - POST: For submitting data
             * - OPTIONS: For preflight requests (automatically sent by browsers before actual requests)
             */
            key: "Access-Control-Allow-Methods",
            value: "GET, POST, OPTIONS",
          },
          {
            /**
             * Access-Control-Allow-Headers
             *
             * Specifies which HTTP headers can be used when making requests to the API.
             * Currently allows:
             * - Content-Type: For specifying the media type of the resource
             * - Authorization: For sending authentication credentials
             */
            key: "Access-Control-Allow-Headers",
            value: "Content-Type, Authorization",
          },
        ],
      },
    ];
  },
};
