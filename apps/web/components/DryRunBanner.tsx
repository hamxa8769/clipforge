// Dry-run banner for the header. Client component: the root layout must
// stay a plain (non-async) server component so the app can be statically
// exported, so the /health check that used to run in the layout happens
// here in the browser instead. Behaviour is unchanged: unknown API state
// is the safe default (banner shown, "server not answering" text); the banner
// disappears only once the API reports dry_run: false.
//
// v0.9.3: the health state comes from the shared useServerStatus()
// hook — this banner no longer fetches /health itself, so it can never
// disagree with the header about whether the server is up. While the
// server is reconnecting it says so plainly instead of claiming the
// page is showing demo data.
"use client";
import { useServerStatus } from "./ServerStatusProvider";

export default function DryRunBanner() {
  const server = useServerStatus();
  const dryRun = server.health?.dry_run ?? true; // Unknown API state: show dry-run banner (safe default).
  if (server.live && !dryRun) return null;
  return (
    <div className="bg-amber-500/15 px-4 py-2 text-center text-sm font-medium text-amber-300" role="status">
      Dry-run mode — nothing will be published to any platform.{" "}
      {server.live
        ? "API reports dry-run."
        : server.checking
          ? "Checking the server…"
          : "Server reconnecting — the app keeps trying on its own."}
    </div>
  );
}
