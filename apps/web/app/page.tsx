// Upgraded dashboard (v0.3.0) — high-fi, one screen for the whole operation.
//
// Data: every card fetches from the real backend (jobs, posts, accounts,
// analytics, monetization-with-real-metrics) and polls every 15s while
// the API is live. When the API is unreachable the page falls back to
// clearly-labelled demo data (DemoBadge) — demo numbers are never
// presented as live channel stats. While the first fetch is in flight
// the page shows skeletons, not a blank screen.
//
// One primary action per screen: "Review approval queue (N)".
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  activeStageIndex, apiGet, getAccounts, getAnalytics, getJobs, getMonetizationFor, getPosts,
  latestSnapshotPerPlatform, pipelineProgress, PIPELINE_STAGES, PROCESSING_STATUSES,
  type AnalyticsSnapshot, type Job, type Monetization, type Post, type SocialAccount,
} from "@/lib/api";
import { demoAccounts, demoAnalytics, demoJobs, demoMonetization, demoPosts } from "@/lib/mock";
import { Card, CardHeader, DemoBadge, EmptyState, fmtCompact, fmtNum, PlatformDot, platformLabel, PrimaryButton, ProgressBar, SecondaryButton, Skeleton, StatusPill } from "@/components/ui";

const PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"] as const;

interface DashboardData {
  jobs: Job[]; posts: Post[]; accounts: SocialAccount[];
  analytics: AnalyticsSnapshot[]; monetization: Monetization[];
}

function StatCard({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <Card>
      <p className="text-sm text-neutral-400">{label}</p>
      <p className="mt-1 text-3xl font-bold tracking-tight text-neutral-50">{value}</p>
      <p className="mt-1 text-xs leading-relaxed text-neutral-500">{sub}</p>
    </Card>
  );
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [live, setLive] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const load = useCallback(async () => {
    const [fetchedJobs, fetchedPosts, fetchedAccounts, fetchedAnalytics, health] = await Promise.all([
      getJobs(), getPosts(), getAccounts(), getAnalytics(), apiGet<unknown>("/health"),
    ]);
    const isLive = health !== null;
    setLive(isLive);
    if (!isLive) {
      setData({ jobs: demoJobs, posts: demoPosts, accounts: demoAccounts, analytics: demoAnalytics, monetization: demoMonetization });
    } else {
      const jobs = fetchedJobs ?? [];
      const posts = fetchedPosts ?? [];
      const accounts = fetchedAccounts ?? [];
      const analytics = fetchedAnalytics ?? [];
      // Monetization with real metrics from the latest snapshot per platform —
      // the bare /monetization endpoint defaults every metric to 0.
      const latest = latestSnapshotPerPlatform(analytics);
      const monetization: Monetization[] = [];
      for (const p of PLATFORMS) {
        const s = latest.get(p);
        if (!s) continue;
        const rows = await getMonetizationFor(p, {
          subscribers: s.followers, followers: s.followers,
          watch_hours: s.watch_hours, shorts_views: s.shorts_views_90d, views_30d: s.views,
        });
        if (rows) monetization.push(...rows);
      }
      setData({ jobs, posts, accounts, analytics, monetization });
    }
    setUpdatedAt(new Date());
  }, []);

  useEffect(() => {
    let cancelled = false;
    load().catch(() => { if (!cancelled) setData({ jobs: demoJobs, posts: demoPosts, accounts: demoAccounts, analytics: demoAnalytics, monetization: demoMonetization }); });
    return () => { cancelled = true; };
  }, [load]);

  // Poll while the API is live so running jobs advance on screen.
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => { load().catch(() => {}); }, 15000);
    return () => clearInterval(t);
  }, [live, load]);

  const jobs = data?.jobs ?? [];
  const posts = data?.posts ?? [];
  const accounts = data?.accounts ?? [];
  const analytics = data?.analytics ?? [];
  const monetization = data?.monetization ?? [];

  const pendingApproval = posts.filter((p) => p.status === "pending_approval").length;
  const processing = jobs.filter((j) => PROCESSING_STATUSES.includes(j.status));
  const published = posts.filter((p) => p.status === "published").length;
  const latest = latestSnapshotPerPlatform(analytics);
  const totalViews = [...latest.values()].reduce((n, s) => n + s.views, 0);
  const totalWatchHours = [...latest.values()].reduce((n, s) => n + s.watch_hours, 0);
  const totalFollowers = [...latest.values()].reduce((n, s) => n + s.followers, 0);
  const recentJobs = [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 5);
  const upcoming = [...posts]
    .filter((p) => p.status === "scheduled" || p.status === "approved" || p.status === "pending_approval")
    .filter((p) => p.scheduled_at)
    .sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at))
    .slice(0, 3);
  const hasAnalytics = analytics.length > 0;
  const feb2027 = monetization.find((m) => m.program.includes("1 FEB 2027"));

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        {!live && data !== null && <DemoBadge />}
        <span className="text-sm text-neutral-400">
          API: {data === null ? "checking…" : live ? "connected" : "unreachable — demo data"}
          {updatedAt && <span suppressHydrationWarning> · updated {updatedAt.toLocaleTimeString()}</span>}
        </span>
        <span className="ml-auto flex gap-2">
          <SecondaryButton onClick={() => load()}>Refresh</SecondaryButton>
          <PrimaryButton href="/queue">Review approval queue{pendingApproval > 0 ? ` (${pendingApproval})` : ""}</PrimaryButton>
        </span>
      </div>

      {/* Live stats — or an honest "not connected yet" empty state */}
      {data === null ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-28" />)}
        </div>
      ) : !hasAnalytics ? (
        <EmptyState
          title="No channel connected yet"
          body="Connect a channel to see live views, watch time and followers here. Until then, ClipForge can still clip, score and schedule — it just has no platform stats to show."
          action={<PrimaryButton href="/channels/connect">Connect a channel</PrimaryButton>}
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard label="Total views" value={fmtCompact(totalViews)} sub={[...latest.entries()].map(([p, s]) => `${platformLabel(p)} ${fmtCompact(s.views)}`).join(" · ")} />
          <StatCard label="Watch time" value={`${fmtNum(Math.round(totalWatchHours))} h`} sub="Last 12 months · YouTube only" />
          <StatCard label="Followers / subscribers" value={fmtCompact(totalFollowers)} sub={[...latest.entries()].map(([p, s]) => `${platformLabel(p)} ${fmtCompact(s.followers)}`).join(" · ")} />
          <StatCard label="Published clips" value={String(published)} sub={`${jobs.length} jobs · ${processing.length} processing now`} />
        </div>
      )}

      {/* Channels overview strip */}
      <Card>
        <CardHeader title="Channels" hint={live ? "from GET /accounts" : "demo accounts"} action={<Link href="/channels" className="text-sm font-medium text-amber-300 hover:text-amber-200">All channels →</Link>} />
        {data === null ? (
          <Skeleton className="h-12" />
        ) : accounts.length === 0 ? (
          <EmptyState
            title="No accounts configured"
            body="Open the Connect wizard — it walks you through creating the platform app and pasting the keys, step by step."
            action={<PrimaryButton href="/channels/connect">Open the Connect wizard</PrimaryButton>}
          />
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {accounts.map((a) => (
              <li key={a.id} className="rounded-lg bg-neutral-950 p-4">
                <div className="flex items-center gap-2">
                  <PlatformDot platform={a.provider} />
                  <span className="font-medium">{platformLabel(a.provider)}</span>
                  <span className={`ml-auto rounded-full px-2 py-0.5 text-xs font-medium ${a.token_configured ? "bg-emerald-500/15 text-emerald-300" : "bg-amber-500/15 text-amber-300"}`}>
                    {a.token_configured ? "Connected" : "Token not set"}
                  </span>
                </div>
                <p className="mt-2 truncate text-sm text-neutral-400">{a.label || a.account_ref || "—"}</p>
                <p className="mt-0.5 text-xs text-neutral-500">{a.status || "no status reported"}{a.token_env_var ? ` · ${a.token_env_var}` : ""}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Recent jobs with live stage / progress */}
        <Card>
          <CardHeader title="Recent jobs" hint={live ? "polls every 15s" : "demo jobs"} action={<Link href="/automation" className="text-sm font-medium text-amber-300 hover:text-amber-200">Automation →</Link>} />
          {data === null ? (
            <div className="space-y-3"><Skeleton className="h-14" /><Skeleton className="h-14" /><Skeleton className="h-14" /></div>
          ) : recentJobs.length === 0 ? (
            <EmptyState title="No jobs yet" body="Create your first clip job from a video file or a YouTube link (your own content only). It will appear here with live stage progress." />
          ) : (
            <ul className="divide-y divide-neutral-800">
              {recentJobs.map((j) => (
                <li key={j.id} className="py-3 first:pt-0 last:pb-0">
                  <div className="flex flex-wrap items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title || j.id}</Link>
                      <p className="truncate text-sm text-neutral-400" suppressHydrationWarning>{j.source} · {j.created_at ? new Date(j.created_at).toLocaleString() : ""}</p>
                    </div>
                    <span className="text-sm text-neutral-400">{j.candidates} candidates</span>
                    <StatusPill status={j.status} />
                  </div>
                  {PROCESSING_STATUSES.includes(j.status) ? (
                    <div className="mt-2"><ProgressBar value={pipelineProgress(j.status)} /><p className="mt-1 text-xs text-neutral-500">Currently: {PIPELINE_STAGES[Math.max(0, activeStageIndex(j.status))]} — see the job page for the full pipeline.</p></div>
                  ) : j.status === "failed" && j.error ? (
                    <p className="mt-2 rounded-md bg-red-500/10 px-3 py-2 text-xs text-red-200">{j.error}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* Approval queue summary + upcoming schedule */}
        <div className="space-y-4">
          <Card>
            <CardHeader title="Approval queue" hint={`${pendingApproval} waiting`} action={<Link href="/queue" className="text-sm font-medium text-amber-300 hover:text-amber-200">Open queue →</Link>} />
            {data === null ? <Skeleton className="h-16" /> : pendingApproval === 0 ? (
              <p className="text-sm text-neutral-400">Nothing waiting for approval. Approved clips become scheduled posts.</p>
            ) : (
              <ul className="space-y-2">
                {posts.filter((p) => p.status === "pending_approval").slice(0, 3).map((p) => (
                  <li key={p.id} className="flex items-center gap-3 rounded-lg bg-neutral-950 px-3 py-2">
                    <PlatformDot platform={p.platform} />
                    <span className="text-sm font-medium">{platformLabel(p.platform)}</span>
                    <span className="min-w-0 flex-1 truncate text-sm text-neutral-400">{p.caption}</span>
                    <StatusPill status={p.status} />
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card>
            <CardHeader title="Up next" hint="scheduled posts" action={<Link href="/schedule" className="text-sm font-medium text-amber-300 hover:text-amber-200">Schedule →</Link>} />
            {data === null ? <Skeleton className="h-16" /> : upcoming.length === 0 ? (
              <p className="text-sm text-neutral-400">Nothing scheduled. Approve a clip and it lands here at its platform&apos;s best window.</p>
            ) : (
              <ul className="space-y-2">
                {upcoming.map((p) => (
                  <li key={p.id} className="flex items-center gap-3 rounded-lg bg-neutral-950 px-3 py-2">
                    <PlatformDot platform={p.platform} />
                    <span className="text-sm font-medium" suppressHydrationWarning>{p.scheduled_at ? new Date(p.scheduled_at).toLocaleString() : "unscheduled"}</span>
                    <span className="min-w-0 flex-1 truncate text-sm text-neutral-400">{p.caption}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>

      {/* Monetization progress vs thresholds */}
      <Card>
        <CardHeader title="Monetization" hint="thresholds, not promises — no earnings are guaranteed" action={<Link href="/monetization" className="text-sm font-medium text-amber-300 hover:text-amber-200">Full tracker →</Link>} />
        {data === null ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><Skeleton className="h-20" /><Skeleton className="h-20" /><Skeleton className="h-20" /><Skeleton className="h-20" /></div>
        ) : monetization.length === 0 ? (
          <p className="text-sm text-neutral-400">No monetization data yet — connect a channel and the tracker will show progress against each platform&apos;s threshold.</p>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {PLATFORMS.map((p) => {
              const rows = monetization.filter((m) => m.platform.toLowerCase() === p);
              if (rows.length === 0) return null;
              // Show the flagship program per platform (not the easiest one:
              // Instagram Gifts / Facebook Stars hit 100% far earlier than
              // Subscriptions / CMP, which are the programs that matter).
              const FLAGSHIP: Record<string, string> = {
                youtube: "YPP Full (ads + Premium)",
                instagram: "Subscriptions",
                facebook: "Content Monetization Program (CMP)",
                tiktok: "Creator Rewards Program",
              };
              const best = rows.find((m) => m.program === FLAGSHIP[p]) ?? rows.reduce((a, b) => (a.progress_pct >= b.progress_pct ? a : b));
              return (
                <div key={p} className="rounded-lg bg-neutral-950 p-4">
                  <div className="flex items-center gap-2">
                    <PlatformDot platform={p} />
                    <span className="font-medium">{platformLabel(p)}</span>
                    <span className="ml-auto text-lg font-bold">{best.progress_pct}%</span>
                  </div>
                  <p className="mt-1 truncate text-xs text-neutral-500">{best.program}</p>
                  <ProgressBar value={best.progress_pct} tone={best.progress_pct >= 100 ? "success" : "default"} className="mt-3" />
                  {p === "youtube" && feb2027 && (
                    <p className="mt-2 rounded-md bg-amber-500/10 px-2 py-1.5 text-[11px] leading-snug text-amber-200">
                      From 1 Feb 2027 new YPP applicants need double: {fmtNum(feb2027.threshold.watch_hours_12mo ?? 8000)}h or {fmtCompact(feb2027.threshold.shorts_views_90d_alt ?? feb2027.threshold.shorts_views_90d ?? 20000000)} Shorts views.
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}
