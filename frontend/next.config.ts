import type { NextConfig } from "next";

// Static export: the built UI in out/ is served by the FastAPI backend, so no Node process runs at runtime.
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
