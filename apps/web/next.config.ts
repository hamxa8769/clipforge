import type { NextConfig } from "next";
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Standalone output: `next build` also emits apps/web/.next/standalone,
  // a self-contained Node server used by the packaged release zip
  // (see scripts/build_exe.py --with-web). `next dev` is unaffected.
  output: "standalone",
};
export default nextConfig;
