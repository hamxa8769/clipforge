// Top bar (v0.9.0) — the slim strip above the content: live/demo state on
// the left, performance profile + notifications bell on the right.
//
// PERFORMANCE MODE: this is where the Low profile takes effect visually.
// The profile comes from GET /perf/profile (auto-detected hardware or the
// user's override in Settings). When it is "low" we set data-perf="low"
// on <html>, and globals.css turns off ALL animation / transition /
// transform for the whole app — that is what keeps the UI fast on
// low-end hardware. Balanced / High leave motion on.
"use client";
import { useCallback, useEffect, useState } from "react";
import { getHealth, getPerfProfile, type Health, type PerfProfile } from "@/lib/api";
import NotificationsBell from "./NotificationsBell";

export default function TopBar() {
  const [health, setHealth] = useState<Health | null | undefined>(undefined);
  const [perf, setPerf] = useState<PerfProfile | null>(null);

  const load = useCallback(async () => {
    const [h, p] = await Promise.all([getHealth(), getPerfProfile()]);
    setHealth(h);
    setPerf(p);
  }, []);

  useEffect(() => {
    load().catch(() => setHealth(null));
    const t = setInterval(() => { load().catch(() => {}); }, 30000);
    return () => clearInterval(t);
  }, [load]);

  // Apply the performance profile to the document element.
  useEffect(() => {
    if (typeof document === "undefined") return;
    if (perf?.profile === "low") document.documentElement.dataset.perf = "low";
    else delete document.documentElement.dataset.perf;
  }, [perf]);

  const live = health !== null && health !== undefined;
  const profileLabel = perf ? perf.profile.charAt(0).toUpperCase() + perf.profile.slice(1) : null;

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-neutral-800 bg-neutral-900/60 px-6">
      <span className="inline-flex items-center gap-2 text-xs font-medium text-neutral-300">
        <span className={`h-2 w-2 rounded-full ${live ? "live-dot bg-emerald-400" : "bg-red-400"}`} aria-hidden="true" />
        {health === undefined ? "Checking server…" : live ? "Live — server connected" : "Demo mode — server unreachable"}
      </span>
      {profileLabel && (
        <span
          className="inline-flex items-center gap-1.5 rounded-full bg-neutral-800 px-2.5 py-1 text-xs text-neutral-400"
          title={perf?.reason ? `Performance profile: ${perf.reason}` : "Performance profile"}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-3.5 w-3.5" aria-hidden="true">
            <path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" />
          </svg>
          Performance: {profileLabel}
          {perf?.override ? " (override)" : ""}
        </span>
      )}
      <span className="ml-auto" />
      <NotificationsBell />
    </header>
  );
}
