// Client component: posts + liveness are fetched in the browser so this
// page can be statically exported. Same UI, same demo fallback + badge.
//
// v0.3.0: list view with state filters (Postiz list-view pattern — All /
// Scheduled / Draft / Published / Failed) and failed posts shown with a
// red ring + their stored error, so a failed publish is diagnosable here
// instead of silently sitting in the list.
"use client";
import { useEffect, useState } from "react";
import { getPosts, type Post } from "@/lib/api";
import { useServerStatus } from "@/components/ServerStatusProvider";
import { demoPosts } from "@/lib/mock";
import { Card, DemoBadge, StatusPill } from "@/components/ui";

const STAGGER = [
  { platform: "Instagram Reels", time: "11:00", note: "11 AM–1 PM is the Reels window; 3–5 Reels/week." },
  { platform: "Facebook Reels", time: "13:00", note: "Early afternoon; 3–5/week. API Reels cap is 90 seconds." },
  { platform: "YouTube Shorts", time: "15:00", note: "2–4 PM window; 3–5 Shorts/week beats bursts." },
  { platform: "TikTok", time: "19:00", note: "6–10 PM local; 1–4/day for max growth, quality-gated." },
];

const FILTERS = [
  { key: "all", label: "All" },
  { key: "scheduled", label: "Scheduled" },
  { key: "pending_approval", label: "Awaiting approval" },
  { key: "published", label: "Published" },
  { key: "failed", label: "Failed" },
] as const;

export default function SchedulePage() {
  // Post statuses are normalised in getPosts() (legacy "pending" -> "pending_approval").
  const [posts, setPosts] = useState<Post[]>(demoPosts);
  // Liveness is the shared server status (v0.9.3). The demo badge
  // shows only once a probe has actually failed, not while the first
  // probe is still checking.
  const server = useServerStatus();
  const live = server.live;
  const [filter, setFilter] = useState<string>("all");
  // Re-runs when liveness flips, so the page recovers on its own
  // when the server comes back.
  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    (async () => {
      const fetched = await getPosts();
      if (cancelled) return;
      if (fetched !== null) setPosts(fetched);
    })();
    return () => {
      cancelled = true;
    };
  }, [live]);
  const sorted = [...posts].sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at));
  const visible = filter === "all" ? sorted : sorted.filter((p) => p.status === filter);
  const countFor = (key: string) => key === "all" ? posts.length : posts.filter((p) => p.status === key).length;
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3"><h1 className="text-2xl font-bold">Schedule</h1>{!live && server.status === "reconnecting" && <DemoBadge />}</div>
      <Card>
        <h2 className="mb-3 text-lg font-semibold">Staggered posting times</h2>
        <p className="mb-4 text-sm text-neutral-400">Stagger per platform instead of blasting all four at once — each platform gets its own best window.</p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {STAGGER.map((s) => (
            <div key={s.platform} className="rounded-lg bg-neutral-950 p-4">
              <p className="font-medium">{s.platform}</p>
              <p className="mt-1 text-2xl font-bold">{s.time}</p>
              <p className="mt-1 text-xs text-neutral-400">{s.note}</p>
            </div>
          ))}
        </div>
      </Card>
      <Card>
        <h2 className="mb-4 text-lg font-semibold">Scheduled posts</h2>
        <div className="mb-4 flex flex-wrap gap-2" role="tablist" aria-label="Filter posts by state">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              role="tab"
              aria-selected={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded-full px-3 py-1.5 text-sm font-medium ${filter === f.key ? "bg-amber-400 text-neutral-950" : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"}`}
            >
              {f.label} ({countFor(f.key)})
            </button>
          ))}
        </div>
        {visible.length === 0 ? <p className="text-neutral-400">No posts in this state yet.</p> : (
          <ul className="divide-y divide-neutral-800">{visible.map((p) => (
            <li key={p.id} className={`py-3 ${p.status === "failed" ? "rounded-lg ring-1 ring-red-500/60" : ""}`}>
              <div className="flex flex-wrap items-center gap-3 px-1">
                <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs font-medium">{p.platform}</span>
                {/* Locale-formatted date: suppress the hydration warning, the
                    static prerender uses the build machine's locale. */}
                <span className="text-sm font-medium" suppressHydrationWarning>{p.scheduled_at ? new Date(p.scheduled_at).toLocaleString() : "unscheduled"}</span>
                <span className="min-w-0 flex-1 truncate text-sm text-neutral-400">{p.caption}</span>
                <StatusPill status={p.status} />
              </div>
              {p.status === "failed" && p.error && (
                <p className="mt-2 rounded-md bg-red-500/10 px-3 py-2 text-xs text-red-200">{p.error}{p.attempts > 0 ? ` (attempt ${p.attempts})` : ""}</p>
              )}
            </li>
          ))}</ul>
        )}
      </Card>
    </div>
  );
}
