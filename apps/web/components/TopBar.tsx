// Top bar (v0.9.0) — the slim strip above the content: live/demo state on
// the left, performance profile + notifications bell on the right.
//
// The live state comes from the shared useServerStatus() hook (v0.9.3) —
// this component no longer polls /health itself. Before that, each
// component polled on its own schedule, so the header could say "Live"
// while Settings said "unreachable" in the same second.
//
// PERFORMANCE MODE: this is where the Low profile takes effect visually.
// The profile comes from GET /perf/profile (auto-detected hardware or the
// user's override in Settings). When it is "low" we set data-perf="low"
// on <html>, and globals.css turns off ALL animation / transition /
// transform for the whole app — that is what keeps the UI fast on
// low-end hardware. Balanced / High leave motion on.
"use client";
import { useEffect, useState } from "react";
import { getPerfProfile, type PerfProfile } from "@/lib/api";
import { serverStatusLabel, formatLastSeenAge } from "@/lib/server-status";
import { useServerStatus, RestartServerButton } from "./ServerStatusProvider";
import NotificationsBell from "./NotificationsBell";

export default function TopBar() {
  const server = useServerStatus();
  const [perf, setPerf] = useState<PerfProfile | null>(null);

  // The performance profile is independent of server liveness: a
  // failed fetch just leaves the pill hidden, it never affects the
  // live state. It is re-checked when the server comes back.
  useEffect(() => {
    if (!server.live) return;
    let cancelled = false;
    getPerfProfile().then((p) => { if (!cancelled) setPerf(p); }).catch(() => {});
    const t = setInterval(() => { getPerfProfile().then((p) => { if (!cancelled) setPerf(p); }).catch(() => {}); }, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, [server.live]);

  // Apply the performance profile to the document element.
  useEffect(() => {
    if (typeof document === "undefined") return;
    if (perf?.profile === "low") document.documentElement.dataset.perf = "low";
    else delete document.documentElement.dataset.perf;
  }, [perf]);

  const profileLabel = perf ? perf.profile.charAt(0).toUpperCase() + perf.profile.slice(1) : null;

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-neutral-800 bg-neutral-900/60 px-6">
      <span
        className="inline-flex items-center gap-2 text-xs font-medium text-neutral-300"
        title={server.lastLiveAt ? `Last connected ${formatLastSeenAge(server.lastLiveAt, Date.now())}` : undefined}
      >
        <span className={`h-2 w-2 rounded-full ${server.live ? "live-dot bg-emerald-400" : server.checking ? "bg-amber-400" : "bg-red-400"}`} aria-hidden="true" />
        {serverStatusLabel(server.status, server.attempts)}
      </span>
      {server.reconnecting && <RestartServerButton />}
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
