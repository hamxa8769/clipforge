import type { NextConfig } from "next";

// Static-export mode for the desktop app (Tauri sidecar): the API server
// serves the exported files from <base>/web/out, so the UI needs no Node
// at runtime. Enable with CLIPFORGE_STATIC_EXPORT=1
// (`npm run build:static`). Without it, keep `output: "standalone"` —
// the existing release-zip behaviour (Node server under web/) is unchanged.
const staticExport = process.env.CLIPFORGE_STATIC_EXPORT === "1";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Standalone output: `next build` also emits apps/web/.next/standalone,
  // a self-contained Node server used by the packaged release zip
  // (see scripts/build_exe.py --with-web). `next dev` is unaffected.
  output: staticExport ? "export" : "standalone",
  // next/image optimisation needs a server; static export has none.
  // trailingSlash makes the export emit queue/index.html (not queue.html)
  // so clipforge-server's StaticFiles(html=True) mount serves /queue,
  // /jobs/job-001, ... — without it those extensionless routes 404.
  ...(staticExport ? { images: { unoptimized: true }, trailingSlash: true } : {}),
};
export default nextConfig;
