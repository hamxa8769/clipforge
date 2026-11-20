// Client component: posts + liveness are fetched in the browser so this
// page can be statically exported. Same UI, same demo fallback + badge.
"use client";
import { useEffect, useState } from "react";
import { apiGet, getPosts, type Post } from "@/lib/api";
import { demoPosts } from "@/lib/mock";
import { Card, DemoBadge, StatusPill } from "@/components/ui";

const STAGGER = [
  { platform: "Instagram Reels", time: "11:00", note: "11 AM–1 PM is the Reels window; 3–5 Reels/week." },
  { platform: "Facebook Reels", time: "13:00", note: "Early afternoon; 3–5/week. API Reels cap is 90 seconds." },
  { platform: "YouTube Shorts", time: "15:00", note: "2–4 PM window; 3–5 Shorts/week beats bursts." },
  { platform: "TikTok", time: "19:00", note: "6–10 PM local; 1–4/day for max growth, quality-gated." },
];

export default function SchedulePage() {
  // Post statuses are normalised in getPosts() (legacy "pending" -> "pending_approval").
  const [posts, setPosts] = useState<Post[]>(demoPosts);
  const [live, setLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetched, health] = await Promise.all([getPosts(), apiGet<unknown>("/health")]);
      if (cancelled) return;
      if (fetched !== null) setPosts(fetched);
      setLive(health !== null);
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  const sorted = [...posts].sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at));
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3"><h1 className="text-2xl font-bold">Schedule</h1>{!live && <DemoBadge />}</div>
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
        {sorted.length === 0 ? <p className="text-neutral-400">No posts scheduled yet.</p> : (
          <ul className="divide-y divide-neutral-800">{sorted.map((p) => (
            <li key={p.id} className="flex flex-wrap items-center gap-3 py-3">
              <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs font-medium">{p.platform}</span>
              {/* Locale-formatted date: suppress the hydration warning, the
                  static prerender uses the build machine's locale. */}
              <span className="text-sm font-medium" suppressHydrationWarning>{new Date(p.scheduled_at).toLocaleString()}</span>
              <span className="min-w-0 flex-1 truncate text-sm text-neutral-400">{p.caption}</span>
              <StatusPill status={p.status} />
            </li>
          ))}</ul>
        )}
      </Card>
    </div>
  );
}
