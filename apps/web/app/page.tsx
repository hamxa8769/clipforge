// Dashboard (v0.5.0) — the home screen: live connected accounts with
// automation progress on each, running jobs, recent activity, and a
// health strip (server / FFmpeg / dry-run / version).
//
// Data: every number comes from the real backend (channels, jobs, posts,
// analytics, monetization, health, ffmpeg/status, worker/queue) and polls
// every 15s while the API is live. When the API is unreachable the page
// falls back to clearly-labelled demo data (DemoBadge) — demo numbers are
// never presented as live channel stats. A follower count the API does
// not return shows as "—", never an invented number. While the first
// fetch is in flight the page shows skeletons, not a blank screen.
//
// One primary action per screen: "Review approval queue (N)".
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  activeStageIndex, getAnalytics, getChannels, getFfmpegInstallStatus, getHealth, getJobs,
  getMonetizationFor, getPosts, getWorkerQueue, latestSnapshotPerPlatform, pipelineProgress,
  PIPELINE_STAGES, PROCESSING_STATUSES,
  type AnalyticsSnapshot, type ChannelStatus, type FfmpegInstallStatus, type Health, type Job,
  type Monetization, type Post, type WorkerQueueSummary,
} from "@/lib/api";
import { demoAnalytics, demoChannels, demoJobs, demoMonetization, demoPosts } from "@/lib/mock";
import { useFfmpegInstaller } from "@/components/FfmpegInstaller";
import {
  Avatar, Badge, Card, CardHeader, DemoBadge, EmptyState, fmtCompact, fmtNum, PageHeader,
  PlatformDot, platformLabel, PrimaryButton, ProgressBar, SecondaryButton, Skeleton, StatCard,
  StatusPill, Toast,
} from "@/components/ui";

const PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"] as const;

interface DashboardData {
  jobs: Job[]; posts: Post[]; channels: ChannelStatus[];
  analytics: AnalyticsSnapshot[]; monetization: Monetization[];
  queue: WorkerQueueSummary | null;
}

interface ActivityEvent { time: string | null; text: string; tone: "success" | "warning" | "danger" | "info" | "neutral"; }

function channelStatusBadge(status: ChannelStatus["status"]) {
  if (status === "connected") return <Badge tone="success">● Connected</Badge>;
  if (status === "expired") return <Badge tone="danger">● Expired — reconnect</Badge>;
  return <Badge tone="warning">○ Not connected</Badge>;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

/** FFmpeg reports its version as the full first line of `ffmpeg -version`
 *  ("ffmpeg version 8.1.2 Copyright (c) ...") — show just the number. */
function shortFfmpegVersion(version: string): string {
  const m = version.match(/(\d+\.\d+(?:\.\d+)?)/);
  return m ? m[1] : version.split("\n")[0].slice(0, 30);
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [live, setLive] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [ffmpeg, setFfmpeg] = useState<FfmpegInstallStatus | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [toast, setToast] = useState<{ message: string; tone: "success" | "danger" | "info" } | null>(null);

  const load = useCallback(async () => {
    const [fetchedJobs, fetchedPosts, fetchedChannels, fetchedAnalytics, fetchedHealth, fetchedFfmpeg, fetchedQueue] = await Promise.all([
      getJobs(), getPosts(), getChannels(), getAnalytics(), getHealth(), getFfmpegInstallStatus(), getWorkerQueue(),
    ]);
    const isLive = fetchedHealth !== null;
    setLive(isLive);
    setHealth(fetchedHealth);
    setFfmpeg(fetchedFfmpeg);
    if (!isLive) {
      setData({ jobs: demoJobs, posts: demoPosts, channels: demoChannels, analytics: demoAnalytics, monetization: demoMonetization, queue: null });
    } else {
      const jobs = fetchedJobs ?? [];
      const posts = fetchedPosts ?? [];
      const channels = fetchedChannels ?? [];
      const analytics = fetchedAnalytics ?? [];
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
      setData({ jobs, posts, channels, analytics, monetization, queue: fetchedQueue });
    }
    setUpdatedAt(new Date());
  }, []);

  useEffect(() => {
    let cancelled = false;
    load().catch(() => { if (!cancelled) setData({ jobs: demoJobs, posts: demoPosts, channels: demoChannels, analytics: demoAnalytics, monetization: demoMonetization, queue: null }); });
    return () => { cancelled = true; };
  }, [load]);

  // Poll while the API is live so running jobs advance on screen.
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => { load().catch(() => {}); }, 15000);
    return () => clearInterval(t);
  }, [live, load]);

  const { status: installStatus, start: startInstall, starting: installStarting } = useFfmpegInstaller(() => {
    setToast({ message: "FFmpeg installed — clipping will work now.", tone: "success" });
    load().catch(() => {});
  });
  const ffmpegFound = (installStatus ?? ffmpeg)?.found === true;
  const ffmpegVersion = shortFfmpegVersion((installStatus ?? ffmpeg)?.version ?? "");
  const ffmpegInstalling = installStatus !== null && ["downloading", "verifying", "extracting", "installing"].includes(installStatus.state);

  const jobs = data?.jobs ?? [];
  const posts = data?.posts ?? [];
  const channels = data?.channels ?? [];
  const analytics = data?.analytics ?? [];
  const monetization = data?.monetization ?? [];
  const queue = data?.queue ?? null;

  const pendingApproval = posts.filter((p) => p.status === "pending_approval").length;
  const processing = jobs.filter((j) => PROCESSING_STATUSES.includes(j.status));
  const published = posts.filter((p) => p.status === "published").length;
  const latest = latestSnapshotPerPlatform(analytics);
  const totalViews = [...latest.values()].reduce((n, s) => n + s.views, 0);
  const totalWatchHours = [...latest.values()].reduce((n, s) => n + s.watch_hours, 0);
  const totalFollowers = [...latest.values()].reduce((n, s) => n + s.followers, 0);
  const hasAnalytics = analytics.length > 0;
  const feb2027 = monetization.find((m) => m.program.includes("1 FEB 2027"));

  // One card per platform, in a fixed order — a platform the API does not
  // report yet still gets a card ("Not connected yet"), never silence.
  const channelByPlatform = new Map(channels.map((c) => [c.platform.toLowerCase(), c]));
  const postsByPlatform = (p: string) => posts.filter((x) => x.platform.toLowerCase() === p);
  const lastPublished = (p: string) =>
    postsByPlatform(p).filter((x) => x.status === "published").sort((a, b) => (b.scheduled_at || "").localeCompare(a.scheduled_at || ""))[0];

  // Recent activity, newest first — real events only: worker runs (with
  // the job title joined in), finished/failed jobs, published posts.
  const jobById = new Map(jobs.map((j) => [j.id, j]));
  const activity: ActivityEvent[] = [];
  for (const r of queue?.recent_runs ?? []) {
    const title = jobById.get(r.job_id)?.title || r.job_id || "A clip job";
    const done = r.status === "finished" || r.status === "completed" || r.status === "success";
    activity.push({
      time: r.finished_at ?? r.started_at,
      text: done
        ? `Clip job finished: ${title}`
        : r.status === "failed" || r.outcome === "failed"
          ? `Clip job failed: ${title}${r.error ? ` — ${r.error}` : ""}`
          : `Clip job ${r.status}: ${title}`,
      tone: done ? "success" : r.status === "failed" || r.outcome === "failed" ? "danger" : "info",
    });
  }
  for (const j of jobs.filter((x) => x.status === "failed" && x.error).slice(0, 3)) {
    activity.push({ time: j.finished_at ?? j.created_at, text: `Job failed: ${j.title || j.id} — ${j.error}`, tone: "danger" });
  }
  for (const p of posts.filter((x) => x.status === "published").slice(0, 3)) {
    activity.push({ time: p.scheduled_at, text: `Published to ${platformLabel(p.platform)}: ${p.caption || "clip"}`, tone: "success" });
  }
  activity.sort((a, b) => (b.time ?? "").localeCompare(a.time ?? ""));
  const recentActivity = activity.slice(0, 8);

  const recentJobs = [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 5);
  const upcoming = [...posts]
    .filter((p) => p.status === "scheduled" || p.status === "approved" || p.status === "pending_approval")
    .filter((p) => p.scheduled_at)
    .sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at))
    .slice(0, 3);

  const port = (() => {
    try { if (typeof window !== "undefined") return window.location.port; } catch { /* server render */ }
    return "";
  })();

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        intro="Your connected accounts, what automation is doing right now, and whether the app itself is healthy."
        actions={
          <>
            {!live && data !== null && <DemoBadge />}
            <span className="text-sm text-neutral-400">
              {data === null ? "Checking server…" : live ? "Live" : "API unreachable — demo data"}
              {updatedAt && <span suppressHydrationWarning> · updated {updatedAt.toLocaleTimeString()}</span>}
            </span>
            <SecondaryButton onClick={() => load()}>Refresh</SecondaryButton>
            <PrimaryButton href="/queue">Review approval queue{pendingApproval > 0 ? ` (${pendingApproval})` : ""}</PrimaryButton>
          </>
        }
      />

      {/* Health strip — is the app itself OK? */}
      <Card className="!p-0">
        <div className="grid gap-px overflow-hidden rounded-xl bg-neutral-800 sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-neutral-900 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-neutral-500">Server</p>
            <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
              <span className={`h-2 w-2 rounded-full ${live ? "bg-emerald-400" : "bg-red-400"}`} aria-hidden="true" />
              {live ? `Running${port ? ` · port ${port}` : ""}` : "Unreachable — start the server"}
            </p>
            <p className="mt-0.5 text-xs text-neutral-500">{health?.version ? `ClipForge v${health.version}` : "Version unknown"}</p>
          </div>
          <div className="bg-neutral-900 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-neutral-500">FFmpeg</p>
            {ffmpegFound ? (
              <>
                <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
                  <span className="h-2 w-2 rounded-full bg-emerald-400" aria-hidden="true" /> Found{ffmpegVersion ? ` · ${ffmpegVersion}` : ""}
                </p>
                <p className="mt-0.5 text-xs text-neutral-500">Clipping will work.</p>
              </>
            ) : (
              <>
                <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
                  <span className="h-2 w-2 rounded-full bg-amber-400" aria-hidden="true" /> Missing — clipping needs it
                </p>
                <button
                  onClick={() => startInstall()}
                  disabled={installStarting || ffmpegInstalling || !live}
                  className="mt-2 rounded-md bg-amber-400 px-3 py-1 text-xs font-semibold text-neutral-950 hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {ffmpegInstalling ? `Installing… ${Math.round(installStatus?.progress_pct ?? 0)}%` : installStarting ? "Starting…" : "Install FFmpeg"}
                </button>
              </>
            )}
          </div>
          <div className="bg-neutral-900 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-neutral-500">Dry-run</p>
            <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
              <span className={`h-2 w-2 rounded-full ${health?.dry_run === false ? "bg-emerald-400" : "bg-amber-400"}`} aria-hidden="true" />
              {health == null ? "Unknown" : health.dry_run ? "On — nothing publishes" : "Off — publishing is live"}
            </p>
            <p className="mt-0.5 text-xs text-neutral-500">Approval is always required first.</p>
          </div>
          <div className="bg-neutral-900 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-neutral-500">Automation</p>
            <p className="mt-1.5 text-sm font-medium text-neutral-100">
              {queue ? `${queue.running.length} running · ${queue.queued.length} queued` : processing.length > 0 ? `${processing.length} processing now` : "Idle"}
            </p>
            <p className="mt-0.5 text-xs text-neutral-500">
              {queue?.next ? `Up next: ${queue.next.title || "a clip job"}` : "No job waiting in the queue."}
            </p>
          </div>
        </div>
      </Card>

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

      {/* Connected accounts — live status, followers, last post, automation counts */}
      <section aria-label="Connected accounts">
        <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-lg font-semibold text-neutral-100">Connected accounts</h2>
          <span className="text-xs text-neutral-500">{live ? "from GET /channels" : "demo accounts"}</span>
          <Link href="/channels" className="ml-auto text-sm font-medium text-amber-300 hover:text-amber-200">All channels →</Link>
        </div>
        {data === null ? (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4"><Skeleton className="h-44" /><Skeleton className="h-44" /><Skeleton className="h-44" /><Skeleton className="h-44" /></div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {PLATFORMS.map((p) => {
              const c = channelByPlatform.get(p);
              const snap = latest.get(p);
              const pPosts = postsByPlatform(p);
              const waiting = pPosts.filter((x) => x.status === "pending_approval").length;
              const ready = pPosts.filter((x) => x.status === "approved").length;
              const pub = pPosts.filter((x) => x.status === "published").length;
              const last = lastPublished(p);
              const connected = c?.status === "connected";
              return (
                <Card key={p} className="flex flex-col">
                  <div className="flex items-start gap-3">
                    <Avatar name={c?.account_name || platformLabel(p)} src={c?.avatar_url} platform={p} />
                    <div className="min-w-0 flex-1">
                      <p className="flex items-center gap-2 font-semibold text-neutral-50">
                        <PlatformDot platform={p} /> {platformLabel(p)}
                      </p>
                      <p className="truncate text-sm text-neutral-400">{c?.account_name || "Not connected yet"}</p>
                    </div>
                  </div>
                  <div className="mt-3">{channelStatusBadge(c?.status ?? "not_connected")}</div>
                  <dl className="mt-3 space-y-1.5 text-sm">
                    <div className="flex justify-between gap-2">
                      <dt className="text-neutral-500">Followers</dt>
                      <dd className="font-medium text-neutral-100">{snap ? fmtCompact(snap.followers) : "—"}</dd>
                    </div>
                    <div className="flex justify-between gap-2">
                      <dt className="text-neutral-500">Last post</dt>
                      <dd className="truncate font-medium text-neutral-100" suppressHydrationWarning>
                        {last ? fmtTime(last.scheduled_at) : "No posts yet"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-neutral-500">Automation</dt>
                      <dd className="mt-0.5 font-medium text-neutral-100">{waiting} waiting · {ready} ready · {pub} published</dd>
                    </div>
                  </dl>
                  {c?.review_notice && <p className="mt-3 rounded-md bg-amber-500/10 px-2.5 py-2 text-xs leading-snug text-amber-200">{c.review_notice}</p>}
                  <div className="mt-auto pt-4">
                    {connected ? (
                      <SecondaryButton href="/channels">Manage channel</SecondaryButton>
                    ) : (
                      <PrimaryButton href={`/channels/connect?platform=${p}`}>Connect {platformLabel(p)}</PrimaryButton>
                    )}
                  </div>
                </Card>
              );
            })}
          </div>
        )}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Automation in progress — live progress bars */}
        <Card>
          <CardHeader title="Automation in progress" hint={live ? "polls every 15s" : "demo jobs"} action={<Link href="/automation" className="text-sm font-medium text-amber-300 hover:text-amber-200">Automation →</Link>} />
          {data === null ? (
            <div className="space-y-3"><Skeleton className="h-14" /><Skeleton className="h-14" /><Skeleton className="h-14" /></div>
          ) : processing.length === 0 && (queue?.queued.length ?? 0) === 0 ? (
            <EmptyState title="Nothing running right now" body="Create a clip job from a video file or a YouTube link (your own content only). It will appear here with live stage progress." action={<PrimaryButton href="/automation">New clip job</PrimaryButton>} />
          ) : (
            <div className="space-y-4">
              {processing.map((j) => {
                const pct = j.progress_pct > 0 ? j.progress_pct : pipelineProgress(j.status);
                return (
                  <div key={j.id}>
                    <div className="flex flex-wrap items-center gap-3">
                      <div className="min-w-0 flex-1">
                        <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title || j.id}</Link>
                        <p className="truncate text-sm text-neutral-400" suppressHydrationWarning>{j.source} · {j.created_at ? new Date(j.created_at).toLocaleString() : ""}</p>
                      </div>
                      <StatusPill status={j.status} />
                    </div>
                    <div className="mt-2"><ProgressBar value={pct} /><p className="mt-1 text-xs text-neutral-500">Currently: {PIPELINE_STAGES[Math.max(0, activeStageIndex(j.status))]} · {Math.round(pct)}%</p></div>
                    {j.status === "failed" && j.error && <p className="mt-2 rounded-md bg-red-500/10 px-3 py-2 text-xs text-red-200">{j.error}</p>}
                  </div>
                );
              })}
              {recentJobs.filter((j) => !PROCESSING_STATUSES.includes(j.status)).slice(0, 2).map((j) => (
                <div key={j.id} className="flex flex-wrap items-center gap-3 border-t border-neutral-800 pt-3">
                  <div className="min-w-0 flex-1">
                    <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title || j.id}</Link>
                    <p className="truncate text-sm text-neutral-400">{j.candidates} candidates</p>
                  </div>
                  <StatusPill status={j.status} />
                </div>
              ))}
              <p className="border-t border-neutral-800 pt-3 text-sm text-neutral-400">
                {queue ? `${queue.queued.length} job${queue.queued.length === 1 ? "" : "s"} queued${queue.next ? ` — up next: ${queue.next.title || "a clip job"}` : ""}` : `${jobs.length} jobs total · ${published} clips published`}
              </p>
            </div>
          )}
        </Card>

        {/* Recent activity — real events from the server */}
        <Card>
          <CardHeader title="Recent activity" hint="jobs, runs & publishes" action={<Link href="/automation" className="text-sm font-medium text-amber-300 hover:text-amber-200">History →</Link>} />
          {data === null ? (
            <div className="space-y-3"><Skeleton className="h-10" /><Skeleton className="h-10" /><Skeleton className="h-10" /></div>
          ) : recentActivity.length === 0 ? (
            <p className="text-sm text-neutral-400">No activity yet. Jobs you run and clips you publish will show up here.</p>
          ) : (
            <ul className="space-y-3">
              {recentActivity.map((a, i) => (
                <li key={i} className="flex items-start gap-3">
                  <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${a.tone === "success" ? "bg-emerald-400" : a.tone === "danger" ? "bg-red-400" : a.tone === "warning" ? "bg-amber-400" : a.tone === "info" ? "bg-sky-400" : "bg-neutral-500"}`} aria-hidden="true" />
                  <div className="min-w-0">
                    <p className="text-sm leading-snug text-neutral-200">{a.text}</p>
                    {a.time && <p className="text-xs text-neutral-500" suppressHydrationWarning>{fmtTime(a.time)}</p>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
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

        {/* Monetization progress vs thresholds */}
        <Card>
          <CardHeader title="Monetization" hint="thresholds, not promises — no earnings are guaranteed" action={<Link href="/monetization" className="text-sm font-medium text-amber-300 hover:text-amber-200">Full tracker →</Link>} />
          {data === null ? (
            <div className="grid gap-3 sm:grid-cols-2"><Skeleton className="h-20" /><Skeleton className="h-20" /><Skeleton className="h-20" /><Skeleton className="h-20" /></div>
          ) : monetization.length === 0 ? (
            <p className="text-sm text-neutral-400">No monetization data yet — connect a channel and the tracker will show progress against each platform&apos;s threshold.</p>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              {PLATFORMS.map((p) => {
                const rows = monetization.filter((m) => m.platform.toLowerCase() === p);
                if (rows.length === 0) return null;
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

      {toast && <Toast message={toast.message} tone={toast.tone} onClose={() => setToast(null)} />}
    </div>
  );
}
