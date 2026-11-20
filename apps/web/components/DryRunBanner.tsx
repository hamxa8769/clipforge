// Dry-run banner for the header. Client component: the root layout must
// stay a plain (non-async) server component so the app can be statically
// exported, so the /health check that used to run in the layout happens
// here in the browser instead. Behaviour is unchanged: unknown API state
// is the safe default (banner shown, "API unreachable" text); the banner
// disappears only once the API reports dry_run: false.
"use client";
import { useEffect, useState } from "react";
import { getHealth, type Health } from "@/lib/api";

export default function DryRunBanner() {
  // undefined = not checked yet; null = API unreachable.
  const [health, setHealth] = useState<Health | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    getHealth().then((h) => {
      if (!cancelled) setHealth(h);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  const dryRun = health?.dry_run ?? true; // Unknown API state: show dry-run banner (safe default).
  if (!dryRun) return null;
  return (
    <div className="bg-amber-500/15 px-4 py-2 text-center text-sm font-medium text-amber-300" role="status">
      Dry-run mode — nothing will be published to any platform. {health ? "API reports dry-run." : "API unreachable; showing demo data."}
    </div>
  );
}
