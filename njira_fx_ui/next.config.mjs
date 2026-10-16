/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export for deployment
  output: 'export',
  distDir: 'dist',

  // Enable React's strict mode for debugging
  reactStrictMode: true,

  // Configure allowed remote image domains for the next/image component
  images: {
    unoptimized: true, // Required for static export
    remotePatterns: [
      {
        protocol: "https",
        hostname: "images.pexels.com",
        port: "",
        pathname: "/**", // Allow all paths
      },
      {
        protocol: "https",
        hostname: "images.unsplash.com",
        port: "",
        pathname: "/**", // Allow all paths
      },
      {
        protocol: "https",
        hostname: "scontent.fblz1-1.fna.fbcdn.net",
        port: "",
        pathname: "/**", // Allow all paths
      },
      {
        protocol: "https",
        hostname: "photos.fife.usercontent.google.com",
        port: "",
        pathname: "/**", // Allow all paths
      },
    ],
  },
  async headers() {
    return [
      {
        source: "/api/:path*",
        headers: [
          {
            key: "Access-Control-Allow-Origin",
            value: "https://forex.xhed.net/",
          }, // Replace '*' with your frontend's origin if needed
          { key: "Access-Control-Allow-Methods", value: "GET, POST, OPTIONS" },
          {
            key: "Access-Control-Allow-Headers",
            value: "Content-Type, Authorization",
          },
        ],
      },
    ];
  },
};

export default nextConfig;