import type { NextConfig } from "next";

const apiOrigin = process.env.PROOFPATH_API_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  devIndicators: false,
  async rewrites() {
    return [
      { source: "/__legacy", destination: `${apiOrigin}/` },
      { source: "/__legacy/:path*", destination: `${apiOrigin}/:path*` },
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/auth.css", destination: `${apiOrigin}/auth.css` },
      { source: "/auth-ui.js", destination: `${apiOrigin}/auth-ui.js` },
      { source: "/portfolio-renderer.js", destination: `${apiOrigin}/portfolio-renderer.js` },
      { source: "/openapi.json", destination: `${apiOrigin}/openapi.json` },
      { source: "/docs", destination: `${apiOrigin}/docs` },
      { source: "/redoc", destination: `${apiOrigin}/redoc` },
    ];
  },
};

export default nextConfig;
