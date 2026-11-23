// Growth page (v0.6.0, Worker C) — what your posted clips are actually
// doing, and what to post next.
//
// Honesty rule: this page NEVER invents performance data. Every advice
// section carries its basis — "from your N posted clips" or "YouTube best
// practice (general)". With no posted clips it shows an honest empty
// state plus general guidance, clearly marked as general. When the API is
// unreachable it says so — unlike other pages it does NOT fall back to
// demo numbers, because fake view counts would be worse than no data.
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiGet, apiPostJson } from "@/lib/api";
import { Card, CardHeader, EmptyState, PlatformDot, Skeleton, fmtCompact, fmtNum, platformLabel } from "@/components/ui";
import { ReconnectingNotice, useServerStatus } from "@/components/ServerStatusProvider";

type Basis = { kind: "own_data" | "general"; label: string; n: number };

type GrowthClip = {
  post_id: string;
  posted_video_id: string | null;
  platform: string;
  title: string;
  posted_at: string | null;
  niche: string;
  views: number | null;
  likes: number | null;
  comments: number | null;
  views_per_day: number | null;
  engagement_pct: number | null;
  trend: string;
  stale: boolean;
  stats_status: string;
};

type GrowthAdvice = {
  best_posting_times: { basis: Basis; best_hours_utc: number[]; hours: { hour_utc: number; avg_views_per_day: number }[]; note: string };
  best_titles: { basis: Basis; top: { post_id: string; title: string; platform: string; views: number; views_per_day: number }[] };
  niches: { basis: Basis; ranking: { niche: string; clips: number; avg_views_per_day: number }[] };
  what_to_post_next: { text: string; basis: string }[];
  general_best_practice: { topic: string; detail: string }[];
  min_clips_for_own_data: number;
};

type GrowthView = {
  posted_clips: GrowthClip[];
  totals: { posted_clips: number; clips_with_stats: number; total_views: number; total_likes: number; total_comments: number; avg_views_per_day: number | null };
  advice: GrowthAdvice;
  platform_status: Record<string, string>;
  fetch_error: string | null;
  fetched_at: string;
  has_posted_clips: boolean;
};

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtHour(h: number): string {
  return `${String(h).padStart(2, "0")}:00`;
}

/** Basis pill: green for the user's own data, amber for general guidance. */
function BasisPill({ basis }: { basis: Basis }) {
  const own = basis.kind === "own_data";
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${own ? "bg-emerald-500/15 text-emerald-300" : "bg-amber-500/15 text-amber-200"}`}>
      {basis.label}
    </span>
  );
}

const TREND_LABEL: Record<string, string> = {
  growing: "Growing",
  flat: "Flat",
  falling: "Falling",
  new: "New — no earlier snapshot yet",
  unknown: "Too soon to tell",
};

const STATS_STATUS_LABEL: Record<string, string> = {
  live: "Live stats",
  stale: "Last known stats — refresh failed",
  unsupported: "Stats not available for this platform yet",
  no_video_id: "Posted, but no video id was recorded",
  unavailable: "No stats yet — connect the channel and refresh",
};

function TrendPill({ trend }: { trend: string }) {
  const tone = trend === "growing" ? "bg-emerald-500/15 text-emerald-300" : trend === "falling" ? "bg-red-500/15 text-red-300" : "bg-neutral-700/50 text-neutral-300";
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}>{TREND_LABEL[trend] ?? trend}</span>;
}

export default function GrowthPage() {
  const server = useServerStatus();
  const [view, setView] = useState<GrowthView | null>(null);
  const [apiDown, setApiDown] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    const data = await apiGet<GrowthView>("/growth");
    if (data === null) { setApiDown(true); return; }
    setApiDown(false);
    setView(data);
  }, []);

  useEffect(() => { void load(); }, [load]);

  // When the server comes back after being down, reload on our own.
  useEffect(() => { if (server.live) void load(); }, [server.live, load]);

  const refresh = async () => {
    setRefreshing(true);
    const res = await apiPostJson("/growth/refresh");
    setRefreshing(false);
    if (res.ok && res.data) setView(res.data as GrowthView);
    else await load();
  };

  const loading = view === null && !apiDown;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">Growth</h1>
        <span className="text-sm text-neutral-400">What your posted clips are doing — and what to post next.</span>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={refreshing || loading || apiDown}
          className="ml-auto rounded-lg bg-amber-400 px-3 py-2 text-sm font-semibold text-neutral-950 disabled:opacity-50"
        >
          {refreshing ? "Refreshing…" : "Refresh stats"}
        </button>
      </div>

      {loading ? (
        <div className="grid gap-4 md:grid-cols-4"><Skeleton className="h-24" /><Skeleton className="h-24" /><Skeleton className="h-24" /><Skeleton className="h-24" /></div>
      ) : apiDown ? (
        <div className="space-y-3">
          <ReconnectingNotice />
          <Card>
            <p className="font-medium text-neutral-200">Growth numbers come from the local API</p>
            <p className="mt-1 text-sm text-neutral-400">This page reloads itself when the server is back. No demo numbers are shown here: invented view counts would be worse than no data.</p>
          </Card>
        </div>
      ) : view !== null && !view.has_posted_clips ? (
        <>
          <EmptyState
            title="No posted clips yet — post your first clip to start measuring"
            body="Growth advice is built from your own posted clips' real numbers. Approve a clip in the Approval Queue, publish it, and come back here — after 5 posted clips this page can tell you your best posting times, titles and niches."
            action={<Link href="/queue" className="mt-3 inline-block rounded-lg bg-amber-400 px-3 py-2 text-sm font-semibold text-neutral-950">Go to Approval Queue</Link>}
          />
          <GeneralPractice advice={view.advice} />
        </>
      ) : view !== null ? (
        <>
          {view.fetch_error && (
            <Card className="border-amber-500/40">
              <p className="text-sm text-amber-200">Could not fetch fresh stats ({view.fetch_error}). Showing the last known numbers where available — they are marked “stale” below.</p>
            </Card>
          )}
          <div className="grid gap-4 md:grid-cols-4">
            <Card><p className="text-xs text-neutral-500">Posted clips</p><p className="mt-1 text-2xl font-bold">{fmtNum(view.totals.posted_clips)}</p></Card>
            <Card><p className="text-xs text-neutral-500">Clips with stats</p><p className="mt-1 text-2xl font-bold">{fmtNum(view.totals.clips_with_stats)}</p><p className="text-xs text-neutral-500">of {fmtNum(view.totals.posted_clips)} posted</p></Card>
            <Card><p className="text-xs text-neutral-500">Total views</p><p className="mt-1 text-2xl font-bold">{fmtCompact(view.totals.total_views)}</p><p className="text-xs text-neutral-500">{fmtNum(view.totals.total_likes)} likes · {fmtNum(view.totals.total_comments)} comments</p></Card>
            <Card><p className="text-xs text-neutral-500">Avg views / day</p><p className="mt-1 text-2xl font-bold">{view.totals.avg_views_per_day === null ? "—" : fmtNum(view.totals.avg_views_per_day)}</p><p className="text-xs text-neutral-500">across clips with stats</p></Card>
          </div>

          <Card>
            <CardHeader title="Posted clips" hint="Views, likes and views/day come from the platform API (YouTube today) or the last known snapshot." />
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-sm">
                <thead>
                  <tr className="border-b border-neutral-800 text-xs text-neutral-500">
                    <th className="pb-2 pr-3 font-medium">Clip</th>
                    <th className="pb-2 pr-3 font-medium">Posted</th>
                    <th className="pb-2 pr-3 text-right font-medium">Views</th>
                    <th className="pb-2 pr-3 text-right font-medium">Likes</th>
                    <th className="pb-2 pr-3 text-right font-medium">Comments</th>
                    <th className="pb-2 pr-3 text-right font-medium">Views / day</th>
                    <th className="pb-2 pr-3 text-right font-medium">Likes / views</th>
                    <th className="pb-2 font-medium">Trend</th>
                  </tr>
                </thead>
                <tbody>
                  {view.posted_clips.map((c) => (
                    <tr key={c.post_id} className="border-b border-neutral-800/60 last:border-0">
                      <td className="py-3 pr-3">
                        <span className="flex items-center gap-2">
                          <PlatformDot platform={c.platform} />
                          <span className="font-medium text-neutral-100">{c.title || "(untitled clip)"}</span>
                        </span>
                        <span className="mt-0.5 block text-xs text-neutral-500">{platformLabel(c.platform)}{c.niche ? ` · ${c.niche}` : ""}{c.stale ? " · stale" : ""}</span>
                        {c.stats_status !== "live" && <span className="mt-0.5 block text-xs text-amber-200/80">{STATS_STATUS_LABEL[c.stats_status] ?? c.stats_status}</span>}
                      </td>
                      <td className="py-3 pr-3 text-neutral-300">{fmtDate(c.posted_at)}</td>
                      <td className="py-3 pr-3 text-right">{c.views === null ? "—" : fmtNum(c.views)}</td>
                      <td className="py-3 pr-3 text-right">{c.likes === null ? "—" : fmtNum(c.likes)}</td>
                      <td className="py-3 pr-3 text-right">{c.comments === null ? "—" : fmtNum(c.comments)}</td>
                      <td className="py-3 pr-3 text-right font-medium">{c.views_per_day === null ? "—" : fmtNum(c.views_per_day)}</td>
                      <td className="py-3 pr-3 text-right">{c.engagement_pct === null ? "—" : `${c.engagement_pct}%`}</td>
                      <td className="py-3">{c.views === null ? <span className="text-neutral-500">—</span> : <TrendPill trend={c.trend} />}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs text-neutral-500">Stats refreshed {new Date(view.fetched_at).toLocaleString()}. YouTube stats come from the YouTube Data API (connected channel or API key); Instagram, Facebook and TikTok per-video stats are not wired yet — check those platforms&apos; own analytics for now.</p>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader title="Best posting times" hint="UTC hours" action={<BasisPill basis={view.advice.best_posting_times.basis} />} />
              {view.advice.best_posting_times.hours.length > 0 ? (
                <ul className="space-y-2 text-sm">
                  {view.advice.best_posting_times.hours.map((h) => (
                    <li key={h.hour_utc} className="flex justify-between gap-2">
                      <span>{fmtHour(h.hour_utc)} UTC</span>
                      <span className="text-neutral-400">{fmtNum(h.avg_views_per_day)} views/day on average</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-neutral-300">Try <span className="font-medium text-neutral-100">{view.advice.best_posting_times.best_hours_utc.map(fmtHour).join(", ")} UTC</span> as a starting point.</p>
              )}
              <p className="mt-3 text-xs text-neutral-500">{view.advice.best_posting_times.note}</p>
            </Card>

            <Card>
              <CardHeader title="Best-performing titles" hint="Top clips by views/day — copy the pattern" action={<BasisPill basis={view.advice.best_titles.basis} />} />
              {view.advice.best_titles.top.length === 0 ? (
                <p className="text-sm text-neutral-400">No clips with stats yet.</p>
              ) : (
                <ol className="list-decimal space-y-2 pl-5 text-sm">
                  {view.advice.best_titles.top.map((t) => (
                    <li key={t.post_id}>
                      <span className="font-medium text-neutral-100">{t.title || "(untitled clip)"}</span>
                      <span className="block text-xs text-neutral-500">{platformLabel(t.platform)} · {fmtNum(t.views)} views · {fmtNum(t.views_per_day)} views/day</span>
                    </li>
                  ))}
                </ol>
              )}
            </Card>

            <Card>
              <CardHeader title="Which niches are growing" hint="Average views/day per niche" action={<BasisPill basis={view.advice.niches.basis} />} />
              {view.advice.niches.ranking.length === 0 ? (
                <p className="text-sm text-neutral-400">Not enough posted clips with stats to rank niches yet — {view.advice.min_clips_for_own_data} are needed. Post more clips and check back.</p>
              ) : (
                <ul className="space-y-2 text-sm">
                  {view.advice.niches.ranking.map((n) => (
                    <li key={n.niche} className="flex justify-between gap-2">
                      <span className="font-medium text-neutral-100">{n.niche}</span>
                      <span className="text-neutral-400">{fmtNum(n.avg_views_per_day)} views/day · {n.clips} clip{n.clips === 1 ? "" : "s"}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card>
              <CardHeader title="What to post next" />
              <ul className="space-y-3 text-sm">
                {view.advice.what_to_post_next.map((s, i) => (
                  <li key={i} className="rounded-lg bg-neutral-800/60 p-3">
                    <p className="text-neutral-100">{s.text}</p>
                    <p className="mt-1 text-xs text-neutral-500">{s.basis}</p>
                  </li>
                ))}
              </ul>
            </Card>
          </div>

          <GeneralPractice advice={view.advice} />
        </>
      ) : null}
    </div>
  );
}

function GeneralPractice({ advice }: { advice: GrowthAdvice }) {
  return (
    <Card>
      <CardHeader title="General best practice" action={<span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-200">YouTube best practice (general)</span>} />
      <ul className="space-y-2 text-sm">
        {advice.general_best_practice.map((g) => (
          <li key={g.topic}>
            <span className="font-medium text-neutral-100">{g.topic}. </span>
            <span className="text-neutral-400">{g.detail}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
