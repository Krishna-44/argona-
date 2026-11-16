/** @type {import('next').NextConfig} */
// Backend lives on 4110. Keep this in sync with launch.sh / launch.json —
// a stale default here silently 500s every /api/* call.
const API = process.env.ARGONA_API || "http://127.0.0.1:4110";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/:path*` },
    ];
  },
  env: {
    NEXT_PUBLIC_WS_URL:
      (process.env.ARGONA_WS || API.replace(/^http/, "ws")) + "/ws",
  },
};

export default nextConfig;
