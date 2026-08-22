import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits a minimal self-contained server bundle for the runtime image.
  output: "standalone",
  // Was hardcoded to a single HPE PCAI development hostname, which broke dev
  // access from anywhere else. Supplied per-environment instead.
  allowedDevOrigins: process.env.NEXT_ALLOWED_DEV_ORIGINS?.split(",").filter(Boolean) ?? [],
};

export default nextConfig;
