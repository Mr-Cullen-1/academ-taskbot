
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  allowedDevOrigins: [
    "resolution-bradley-assignment-venue.trycloudflare.com",
  ],

  async rewrites() {
    const upstream = process.env.TASKBOT_API_UPSTREAM ||
      (process.env.NODE_ENV === "production" ? undefined : "http://127.0.0.1:8000");
    if (!upstream) throw new Error("TASKBOT_API_UPSTREAM is required in production");
    const url = new URL(upstream);
    if (!(["http:", "https:"].includes(url.protocol)) || url.username || url.password ||
        url.pathname !== "/" || url.search || url.hash ||
        (process.env.NODE_ENV === "production" && url.protocol !== "https:")) {
      throw new Error("TASKBOT_API_UPSTREAM must be an HTTPS origin in production");
    }
    return [{ source: "/api/v1/:path*", destination: `${url.origin}/api/v1/:path*` }];
  },
};

export default nextConfig;

