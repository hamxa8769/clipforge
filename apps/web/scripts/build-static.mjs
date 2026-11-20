// Build the web UI as a pure static export (no Node runtime needed).
// Equivalent to running, from apps/web:
//   CLIPFORGE_STATIC_EXPORT=1 NEXT_PUBLIC_API_URL= next build
// The env vars are set here instead of a shell prefix so the script also
// works on Windows cmd. Verifies apps/web/out/index.html exists afterwards.
//
//   CLIPFORGE_STATIC_EXPORT=1  -> next.config.ts switches to output: "export"
//   NEXT_PUBLIC_API_URL=""      -> lib/api.ts keeps API_URL "" so fetches
//                                are relative ("/jobs"), i.e. same origin
//                                as the UI served by clipforge-server.
//                                Unset would fall back to localhost:8000.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// Run Next's CLI through node directly (node_modules/next/dist/bin/next)
// instead of the node_modules/.bin/next[.cmd] shim: on Windows,
// spawnSync cannot execute a .cmd file without a shell and fails with
// EINVAL (seen in CI on windows-latest). Invoking the CLI with node
// works identically on Windows and Linux, no shell needed.
const nextCli = path.join(root, "node_modules", "next", "dist", "bin", "next");

const res = spawnSync(process.execPath, [nextCli, "build"], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, CLIPFORGE_STATIC_EXPORT: "1", NEXT_PUBLIC_API_URL: "" },
});
if (res.error) {
  console.error(`failed to run next build: ${res.error.message}`);
  process.exit(1);
}
if (res.status !== 0) process.exit(res.status ?? 1);
if (!existsSync(path.join(root, "out", "index.html"))) {
  console.error("static export missing: apps/web/out/index.html not found after `next build`");
  process.exit(1);
}
console.log("static export OK: apps/web/out/index.html exists");
