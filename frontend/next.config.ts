import type { NextConfig } from "next";

// Static export: the built UI in out/ is served by the FastAPI backend, so no Node process runs at runtime.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true, // each view exports as <view>/index.html, which the backend's static server finds directly
  images: { unoptimized: true },
  turbopack: { root: process.cwd() }, // the frontend folder (npm runs scripts here); ignore lockfiles further up
};

export default nextConfig;
