// Dashboard (v0.9.0) — the home screen, rebuilt dashboard-first: the
// daily flow happens here without tab-hopping — start a clip run, watch
// live jobs with real controls, approve clips inline, see what needs
// attention. Deep pages stay for detail, but dashboard → run → approve
// → post is doable from here.
//
// Data: every number comes from the real backend (channels, jobs, posts,
// analytics, monetization, health, ffmpeg/status, requirements/status,
// worker/queue, agent/status) and polls every 15s while the API is live.
//
// Liveness is the shared server status (v0.9.3, useServerStatus) —
// this page no longer decides "live" from its own /health fetch, so
// it can never show demo data while the header says "Live". When the
// API is unreachable and no live data was ever loaded, the page shows
// a reconnecting state with a Retry now button — not a fake live page
// — and reloads itself when the server comes back. Demo data is only
// shown behind an explicit "Show demo preview" choice, clearly
// labelled (DemoBadge), with every control disabled. When live data
// was loaded and the server then drops, the last data stays visible
// under a "Reconnecting — showing last data" banner, never presented
// as current.
// A follower count the API does not return shows as "—", never an
// invented number. While the first fetch is in flight the page shows
// skeletons, not a blank screen.
//
// Job controls — all real, wired to the v0.9.0 job queue engine:
// - Pause: real (POST /jobs/{id}/pause) for queued / running jobs — a
//   running job finishes its current clip, then waits in `paused`.
// - Resume: real (POST /jobs/{id}/resume) for paused jobs — continues
//   from the next stage, nothing is re-done.
// - Cancel: real (POST /jobs/{id}/cancel) for anything not terminal —
//   a running job reports `cancelling` until the worker confirms it.
//   A job in review / ready cannot be cancelled from here (409), so
//   its Cancel is disabled with that reason, not a dead button.
// - Retry: real (POST /jobs/{id}/retry) for failed / blocked jobs.
// - Approve (posts): real (POST /posts/{id}/approve).
// - Reject (posts): NO endpoint exists in this build, so it is disabled
//   with the reason shown.
// - Notifications: the bell in the top bar (GET /api/notifications).
//
// Rights guardrail (FIXED, never changes): clips only from own /
// Creative Commons / public-domain videos; CC-BY attribution is added
// automatically. The guardrail notice stays visible at the top of this
// page and on the New clip run card.
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  activeStageIndex, approvePost, cancelJob, getAgentStatus, getAnalytics, getChannels,
  getFfmpegInstallStatus, getJobs, getMonetizationFor, getPosts, getWorkerQueue,
  latestSnapshotPerPlatform, pipelineProgress, PIPELINE_STAGES, PROCESSING_STATUSES,
  pauseJob, refreshChannel, resumeJob, retryJob,
  type AgentStatus, type AnalyticsSnapshot, type ChannelStatus, type FfmpegInstallStatus,
  type Job, type Monetization, type Post, type WorkerQueueSummary,
} from "@/lib/api";
import { demoAnalytics, demoChannels, demoJobs, demoMonetization, demoPosts } from "@/lib/mock";
import { useFfmpegInstaller } from "@/components/FfmpegInstaller";
import NewClipRun from "@/components/NewClipRun";
import { RequirementsPanel } from "@/components/RequirementsPanel";
import { ReconnectingNotice, useServerStatus } from "@/components/ServerStatusProvider";
import {
  Avatar, Badge, Card, CardHeader, DemoBadge, EmptyState, fmtCompact, fmtNum, PageHeader,
  PlatformDot, platformLabel, PrimaryButton, ProgressBar, SecondaryButton, Skeleton, StatCard,
  StatusPill, Toast,
} from "@/components/ui";

const PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"] as const;

interface DashboardData {
  jobs: Job[]; posts: Post[]; channels: ChannelStatus[];
  analytics: AnalyticsSnapshot[]; monetization: Monetization[];
  queue: WorkerQueueSummary | null; agent: AgentStatus | null;
}

interface ActivityEvent { time: string | null; text: string; tone: "success" | "warning" | "danger" | "info" | "neutral"; }

interface AttentionItem { id: string; tone: "danger" | "warning" | "info"; text: string; href?: string; actionLabel?: string }

function channelStatusBadge(status: ChannelStatus["status"], health?: ChannelStatus["health"]) {
  if (health === "expiring_soon") return <Badge tone="warning">● Expiring soon</Badge>;
  if (health === "disconnected") return <Badge tone="danger">● Disconnected</Badge>;
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

/** The server cancels anything not terminal — review / ready are the
 *  only states it refuses (409). */
function cancellable(job: Job): boolean {
  return !["review", "ready", "cancelled"].includes(job.status);
}

/** The server pauses queued / running jobs (pending counts as queued). */
function pausable(job: Job): boolean {
  return ["pending", "queued", "running", "transcribing", "scoring", "rendering"].includes(job.status);
}

export default function Dashboard() {
  const server = useServerStatus();
  const live = server.live;
  const health = server.health;
  const [data, setData] = useState<DashboardData | null>(null);
  // Demo data is opt-in while the server is down (the "Show demo
  // preview" button) — never the default, never shown as if live.
  const [showDemo, setShowDemo] = useState(false);
  const [ffmpeg, setFfmpeg] = useState<FfmpegInstallStatus | null>(null);
  // Whether the FFmpeg status has ever been checked (v0.9.3 bug B): while
  // the first check is still in flight, `ffmpeg` is null — which used to
  // render as "Missing — clipping needs it" with an Install button for a
  // tool that was merely unchecked. Unknown must show "Checking…".
  const [ffmpegChecked, setFfmpegChecked] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [toast, setToast] = useState<{ message: string; tone: "success" | "danger" | "info" } | null>(null);
  const [busyJob, setBusyJob] = useState<string | null>(null);
  const [busyPost, setBusyPost] = useState<string | null>(null);
  const [busyChannel, setBusyChannel] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [fetchedJobs, fetchedPosts, fetchedChannels, fetchedAnalytics, fetchedFfmpeg, fetchedQueue, fetchedAgent] = await Promise.all([
      getJobs(), getPosts(), getChannels(), getAnalytics(), getFfmpegInstallStatus(), getWorkerQueue(), getAgentStatus(),
    ]);
    const isLive = live;
    setFfmpeg(fetchedFfmpeg);
    setFfmpegChecked(true);
    if (!isLive) {
      // Server down: never overwrite live data with demo numbers.
      // Demo data appears only behind the explicit preview choice.
      if (showDemo) {
        setData({ jobs: demoJobs, posts: demoPosts, channels: demoChannels, analytics: demoAnalytics, monetization: demoMonetization, queue: null, agent: null });
      }
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
      setData({ jobs, posts, channels, analytics, monetization, queue: fetchedQueue, agent: fetchedAgent });
    }
    setUpdatedAt(new Date());
  }, [live, showDemo]);

  // Load on mount, and again whenever liveness flips — `load` changes
  // identity when `live` changes, so this effect re-runs when the
  // server comes back after being down and the page recovers on its
  // own, no manual Refresh needed. (v0.9.2 only polled while live, so
  // a page loaded during downtime never recovered by itself.)
  useEffect(() => {
    let cancelled = false;
    load().catch(() => {});
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
  const agent = data?.agent ?? null;

  const pendingApproval = posts.filter((p) => p.status === "pending_approval").length;
  const processing = jobs.filter((j) => PROCESSING_STATUSES.includes(j.status));
  const failedJobs = jobs.filter((j) => j.status === "failed" || j.status === "blocked");
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

  // What needs attention — the most important thing on the page after
  // the run card. Every row is real state with a real next step.
  const attention: AttentionItem[] = [];
  for (const c of channels) {
    // Health (v0.9.0) is the honest state: "expired" alone used to
    // mean "reconnect from scratch", but a refreshable token just
    // needs a refresh — the health reason + next step say which.
    if (c.health === "disconnected") {
      attention.push({ id: `disconnected-${c.platform}`, tone: "danger", text: `${platformLabel(c.platform)} disconnected — ${c.health_reason} ${c.health_next_step}`, href: "/channels", actionLabel: "Open Channels" });
    } else if (c.health === "expiring_soon") {
      attention.push({ id: `expiring-${c.platform}`, tone: "warning", text: `${platformLabel(c.platform)}: ${c.health_reason} ${c.health_next_step}`, href: "/channels", actionLabel: "Open Channels" });
    } else if (c.status === "expired") {
      attention.push({ id: `expired-${c.platform}`, tone: "danger", text: `${platformLabel(c.platform)} token expired — reconnect it before clips can publish there.`, href: `/channels/connect?platform=${c.platform.toLowerCase()}`, actionLabel: "Reconnect" });
    }
  }
  for (const j of failedJobs.slice(0, 2)) {
    attention.push({ id: `failed-${j.id}`, tone: "danger", text: `Job failed: ${j.title || j.id}${j.error ? ` — ${j.error}` : ""}`, href: `/jobs/view/?id=${encodeURIComponent(j.id)}`, actionLabel: "View job" });
  }
  if (agent?.last_error) {
    attention.push({ id: "agent-error", tone: "warning", text: `Agent stopped with an error: ${agent.last_error}`, href: "/agent", actionLabel: "Open Agent" });
  }
  if (!ffmpegFound && live && ffmpegChecked) {
    attention.push({ id: "ffmpeg", tone: "warning", text: "FFmpeg is missing — clipping cannot run until it is installed." });
  }
  if (pendingApproval > 0) {
    attention.push({ id: "approval", tone: "info", text: `${pendingApproval} clip${pendingApproval === 1 ? "" : "s"} waiting for your approval below.`, href: "#approval", actionLabel: "Review" });
  }

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

  async function onCancelJob(job: Job) {
    setBusyJob(job.id);
    const res = await cancelJob(job.id);
    setBusyJob(null);
    if (!res.ok) { setToast({ message: res.error ?? "Could not cancel the job.", tone: "danger" }); return; }
    const cancelling = (res.data as Record<string, unknown> | null)?.cancelling === true;
    setToast({ message: cancelling ? `Cancelling ${job.title || job.id} — the worker will stop it shortly.` : `Job cancelled: ${job.title || job.id}`, tone: "success" });
    await load();
  }

  async function onPauseJob(job: Job) {
    setBusyJob(job.id);
    const res = await pauseJob(job.id);
    setBusyJob(null);
    if (!res.ok) { setToast({ message: res.error ?? "Could not pause the job.", tone: "danger" }); return; }
    setToast({ message: `Paused: ${job.title || job.id} — resume when ready, nothing is lost.`, tone: "success" });
    await load();
  }

  async function onResumeJob(job: Job) {
    setBusyJob(job.id);
    const res = await resumeJob(job.id);
    setBusyJob(null);
    if (!res.ok) { setToast({ message: res.error ?? "Could not resume the job.", tone: "danger" }); return; }
    setToast({ message: `Resumed: ${job.title || job.id}`, tone: "success" });
    await load();
  }

  async function onRetryJob(job: Job) {
    setBusyJob(job.id);
    const res = await retryJob(job);
    setBusyJob(null);
    if (res === null || !res.ok) { setToast({ message: res?.error ?? "Could not retry the job.", tone: "danger" }); return; }
    setToast({ message: `Retrying job: ${job.title || job.id}`, tone: "success" });
    await load();
  }

  async function onApprovePost(post: Post) {
    setBusyPost(post.id);
    const res = await approvePost(post.id);
    setBusyPost(null);
    if (!res.ok) { setToast({ message: res.error ?? "Could not approve this clip.", tone: "danger" }); return; }
    setToast({ message: "Approved — ready to schedule.", tone: "success" });
    await load();
  }

  async function onRefreshChannel(platform: string) {
    setBusyChannel(platform);
    const res = await refreshChannel(platform);
    setBusyChannel(null);
    if (!res.ok) { setToast({ message: res.error ?? `Could not refresh ${platformLabel(platform)}.`, tone: "danger" }); return; }
    setToast({ message: `${platformLabel(platform)} channel refreshed.`, tone: "success" });
    await load();
  }

  const agentLine = agent
    ? agent.running
      ? `Running${agent.current_job ? ` · ${agent.current_job}` : ""}`
      : agent.enabled
        ? `On — idle${agent.last_error ? " · last run errored" : ""}`
        : "Off"
    : processing.length > 0
      ? `${processing.length} processing now`
      : "Idle";

  // Server down, nothing live ever loaded, no demo preview asked for:
  // a reconnecting state, not a fake live page. The rights notice
  // stays visible (it is not data), then the notice + demo opt-in.
  if (!live && data === null && !showDemo && server.reconnecting) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Dashboard"
          intro="Start a clip run, watch jobs live, approve clips — the daily flow, all on this page."
          actions={
            <span className="text-sm text-neutral-400">
              {`Reconnecting… (attempt ${server.attempts})`}
            </span>
          }
        />
        <p className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm leading-snug text-amber-200" role="note">
          <span className="font-semibold">Rights notice.</span>{" "}
          Make sure you have the right to use the videos you clip — your own video, a licensed video, or a use YouTube permits. You are responsible for the rights to what you post.
          CC-BY attribution is added to the description automatically.{" "}
          <Link href="/trending" className="font-medium underline hover:text-amber-100">Find licensed videos →</Link>
        </p>
        <ReconnectingNotice />
        <p className="text-sm text-neutral-400">
          Your clips, jobs and settings are safe on the server — this page will reload itself when it is back.
          Want to look around meanwhile?{" "}
          <button className="font-medium text-amber-300 underline hover:text-amber-200" onClick={() => setShowDemo(true)}>
            Show demo preview
          </button>{" "}
          (sample data, clearly labelled — nothing here is your channel).
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        intro="Start a clip run, watch jobs live, approve clips — the daily flow, all on this page."
        actions={
          <>
            {!live && data !== null && showDemo && <DemoBadge />}
            {showDemo && !live && data !== null && (
              <SecondaryButton onClick={() => { setShowDemo(false); setData(null); }}>Hide demo preview</SecondaryButton>
            )}
            <span className="text-sm text-neutral-400">
              {data === null
                ? server.live
                  ? "Loading…"
                  : server.checking
                    ? "Checking server…"
                    : `Reconnecting… (attempt ${Math.max(1, server.attempts)})`
                : live
                  ? "Live"
                  : showDemo
                    ? "Demo preview — sample data, not your channel"
                    : "Reconnecting — showing last data"}
              {updatedAt && <span suppressHydrationWarning> · updated {updatedAt.toLocaleTimeString()}</span>}
            </span>
            <SecondaryButton onClick={() => load()}>Refresh</SecondaryButton>
            <PrimaryButton href="/queue">Review approval queue{pendingApproval > 0 ? ` (${pendingApproval})` : ""}</PrimaryButton>
          </>
        }
      />

      {!live && data !== null && !showDemo && <ReconnectingNotice variant="inline" />}

      {/* Rights notice (advisory since v0.9.1) — always visible, never changes. */}
      <p className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm leading-snug text-amber-200" role="note">
        <span className="font-semibold">Rights notice.</span>{" "}
        Make sure you have the right to use the videos you clip — your own video, a licensed video, or a use YouTube permits. You are responsible for the rights to what you post.
        CC-BY attribution is added to the description automatically.{" "}
        <Link href="/trending" className="font-medium underline hover:text-amber-100">Find licensed videos →</Link>
      </p>

      {/* Health strip — is the app itself OK? */}
      <Card className="!p-0">
        <div className="grid gap-px overflow-hidden rounded-xl bg-neutral-800 sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-neutral-900 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-neutral-500">Server</p>
            <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
              <span className={`h-2 w-2 rounded-full ${live ? "live-dot bg-emerald-400" : server.checking ? "bg-amber-400" : "bg-red-400"}`} aria-hidden="true" />
              {live ? `Running${port ? ` · port ${port}` : ""}` : server.checking ? "Checking server…" : `Reconnecting… (attempt ${Math.max(1, server.attempts)})`}
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
            ) : !live ? (
              <>
                <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
                  <span className="h-2 w-2 rounded-full bg-amber-400" aria-hidden="true" /> Unknown — waiting for server
                </p>
                <p className="mt-0.5 text-xs text-neutral-500">FFmpeg is checked when the server is back.</p>
              </>
            ) : !ffmpegChecked ? (
              <>
                <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
                  <span className="h-2 w-2 rounded-full bg-amber-400" aria-hidden="true" /> Checking…
                </p>
                <p className="mt-0.5 text-xs text-neutral-500">FFmpeg is being checked — this settles in a moment.</p>
              </>
            ) : (
              <>
                <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
                  <span className="h-2 w-2 rounded-full bg-amber-400" aria-hidden="true" /> Missing — clipping needs it
                </p>
                <button
                  onClick={() => startInstall()}
                  disabled={installStarting || ffmpegInstalling || !live}
                  title={!live ? "The server is not answering — FFmpeg installs when the API is back" : undefined}
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
            <p className="mt-1.5 flex items-center gap-2 text-sm font-medium text-neutral-100">
              {agent?.running && <span className="live-dot h-2 w-2 rounded-full bg-emerald-400" aria-hidden="true" />}
              {agentLine}
            </p>
            <p className="mt-0.5 text-xs text-neutral-500">
              {queue ? `${queue.running.length} running · ${queue.queued.length} queued` : "No job waiting in the queue."}
            </p>
          </div>
        </div>
      </Card>

      {/* Requirements — verified before any job starts, so a run never
          starts a download it cannot finish. */}
      <Card>
        <CardHeader title="Requirements" hint="yt-dlp · FFmpeg · ffprobe · Whisper — each verified by running it" />
        <RequirementsPanel />
      </Card>

      {/* New clip run — the daily flow starts here. */}
      {data === null ? (
        <Skeleton className="h-64" />
      ) : (
        <NewClipRun live={live} onJobStarted={() => { load().catch(() => {}); }} />
      )}

      {/* Needs attention — only when something actually needs you. */}
      {data !== null && attention.length > 0 && (
        <Card>
          <CardHeader title="Needs attention" hint={`${attention.length} item${attention.length === 1 ? "" : "s"}`} />
          <ul className="space-y-2">
            {attention.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center gap-3 rounded-lg bg-neutral-950 px-3 py-2.5">
                <span className={`h-2 w-2 shrink-0 rounded-full ${a.tone === "danger" ? "bg-red-400" : a.tone === "warning" ? "bg-amber-400" : "bg-sky-400"}`} aria-hidden="true" />
                <span className="min-w-0 flex-1 text-sm text-neutral-200">{a.text}</span>
                {a.href && (
                  <Link href={a.href} className="shrink-0 text-sm font-medium text-amber-300 hover:text-amber-200">{a.actionLabel ?? "Open"} →</Link>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

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

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Live jobs — progress plus real controls. */}
        <Card>
          <CardHeader title="Live jobs" hint={live ? "polls every 15s" : "demo jobs — controls need the server"} action={<Link href="/automation" className="text-sm font-medium text-amber-300 hover:text-amber-200">Automation →</Link>} />
          {data === null ? (
            <div className="space-y-3"><Skeleton className="h-14" /><Skeleton className="h-14" /><Skeleton className="h-14" /></div>
          ) : processing.length === 0 && failedJobs.length === 0 && (queue?.queued.length ?? 0) === 0 ? (
            <EmptyState title="Nothing running right now" body="Start a clip run above — from your own video or a Creative Commons video. It will appear here with live stage progress and controls." />
          ) : (
            <div className="space-y-4">
              {processing.map((j) => {
                const pct = j.progress_pct > 0 ? j.progress_pct : pipelineProgress(j.status);
                const busy = busyJob === j.id;
                const paused = j.status === "paused";
                const queuedPos = queue?.queued.find((q) => q.id === j.id)?.queue_position ?? null;
                const stageLine = paused
                  ? "Paused — resume to continue from where it stopped, nothing is re-done."
                  : j.status === "queued" || j.status === "pending"
                    ? queuedPos ? `Queued — position ${queuedPos}, waiting for the worker.` : "Queued — waiting for the worker."
                    : j.status === "running"
                      ? "Running — starting the next stage."
                      : `Currently: ${PIPELINE_STAGES[Math.max(0, activeStageIndex(j.status))]} · ${Math.round(pct)}%`;
                return (
                  <div key={j.id}>
                    <div className="flex flex-wrap items-center gap-3">
                      <div className="min-w-0 flex-1">
                        <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title || j.id}</Link>
                        <p className="truncate text-sm text-neutral-400" suppressHydrationWarning>{j.source} · {j.created_at ? new Date(j.created_at).toLocaleString() : ""}</p>
                      </div>
                      <StatusPill status={j.status} />
                    </div>
                    <div className="mt-2"><ProgressBar value={pct} /><p className="mt-1 text-xs text-neutral-500">{stageLine}</p></div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {paused ? (
                        <button
                          onClick={() => void onResumeJob(j)}
                          disabled={!live || busy}
                          title={!live ? "Demo data — controls work when the server is connected." : "Resume this job from where it stopped"}
                          className="rounded-md bg-amber-400 px-3 py-1.5 text-xs font-semibold text-neutral-950 hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {busy ? "Working…" : "Resume"}
                        </button>
                      ) : (
                        <button
                          onClick={() => void onPauseJob(j)}
                          disabled={!live || busy || !pausable(j)}
                          title={!live ? "Demo data — controls work when the server is connected." : !pausable(j) ? "Only queued or running jobs can be paused." : "Pause this job — it waits here until resumed"}
                          className="rounded-md bg-neutral-800 px-3 py-1.5 text-xs font-medium text-neutral-200 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {busy ? "Working…" : "Pause"}
                        </button>
                      )}
                      <button
                        onClick={() => void onCancelJob(j)}
                        disabled={!live || busy || !cancellable(j)}
                        title={!live ? "Demo data — controls work when the server is connected." : !cancellable(j) ? "Jobs in review can't be cancelled from here — approve or reject the clips first." : "Cancel this job"}
                        className="rounded-md bg-neutral-800 px-3 py-1.5 text-xs font-medium text-neutral-200 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {busy ? "Working…" : "Cancel"}
                      </button>
                    </div>
                  </div>
                );
              })}
              {failedJobs.slice(0, 2).map((j) => (
                <div key={j.id} className="border-t border-neutral-800 pt-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title || j.id}</Link>
                      <p className="truncate text-sm text-neutral-400">{j.error ?? "Job failed."}</p>
                    </div>
                    <StatusPill status={j.status} />
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      onClick={() => void onRetryJob(j)}
                      disabled={!live || busyJob === j.id}
                      title={!live ? "Demo data — controls work when the server is connected." : "Retry this job"}
                      className="rounded-md bg-neutral-800 px-3 py-1.5 text-xs font-medium text-neutral-200 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {busyJob === j.id ? "Working…" : "Retry"}
                    </button>
                    <button
                      onClick={() => void onCancelJob(j)}
                      disabled={!live || busyJob === j.id}
                      title={!live ? "Demo data — controls work when the server is connected." : "Cancel this job"}
                      className="rounded-md bg-neutral-800 px-3 py-1.5 text-xs font-medium text-neutral-200 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ))}
              {recentJobs.filter((j) => !PROCESSING_STATUSES.includes(j.status) && j.status !== "failed" && j.status !== "blocked").slice(0, 2).map((j) => (
                <div key={j.id} className="flex flex-wrap items-center gap-3 border-t border-neutral-800 pt-3">
                  <div className="min-w-0 flex-1">
                    <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title || j.id}</Link>
                    <p className="truncate text-sm text-neutral-400">{j.candidates} candidates</p>
                  </div>
                  <StatusPill status={j.status} />
                </div>
              ))}
              <p className="border-t border-neutral-800 pt-3 text-sm text-neutral-400">
                {queue ? `${queue.queued.length} job${queue.queued.length === 1 ? "" : "s"} queued${queue.paused.length > 0 ? ` · ${queue.paused.length} paused` : ""}${queue.next ? ` — up next: ${queue.next.title || "a clip job"}` : ""}` : `${jobs.length} jobs total · ${published} clips published`}
              </p>
            </div>
          )}
        </Card>

        {/* Approval queue inline — approve without leaving the dashboard. */}
        <Card>
          <div id="approval" className="scroll-mt-6">
            <CardHeader title="Approval queue" hint={`${pendingApproval} waiting`} action={<Link href="/queue" className="text-sm font-medium text-amber-300 hover:text-amber-200">Open queue →</Link>} />
          </div>
          {data === null ? <Skeleton className="h-16" /> : pendingApproval === 0 ? (
            <p className="text-sm text-neutral-400">Nothing waiting for approval. Approved clips become scheduled posts.</p>
          ) : (
            <ul className="space-y-3">
              {posts.filter((p) => p.status === "pending_approval").slice(0, 4).map((p) => (
                <li key={p.id} className="rounded-lg bg-neutral-950 px-3 py-3">
                  <div className="flex items-center gap-3">
                    <PlatformDot platform={p.platform} />
                    <span className="text-sm font-medium">{platformLabel(p.platform)}</span>
                    <span className="ml-auto"><StatusPill status={p.status} /></span>
                  </div>
                  <p className="mt-1.5 line-clamp-2 text-sm text-neutral-300">{p.caption}</p>
                  {p.scheduled_at && <p className="mt-1 text-xs text-neutral-500" suppressHydrationWarning>Scheduled {fmtTime(p.scheduled_at)}</p>}
                  <div className="mt-2.5 flex flex-wrap gap-2">
                    <button
                      onClick={() => void onApprovePost(p)}
                      disabled={!live || busyPost === p.id}
                      title={!live ? "Demo data — approval works when the server is connected." : "Approve this clip"}
                      className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {busyPost === p.id ? "Working…" : "Approve"}
                    </button>
                    <button
                      disabled
                      title="Reject isn't available yet — the server has no reject endpoint for posts."
                      className="rounded-md bg-neutral-800 px-3 py-1.5 text-xs font-medium text-neutral-400 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Reject
                    </button>
                    <Link href={`/jobs/view/?id=${encodeURIComponent(p.clip_id)}`} className="rounded-md bg-neutral-800 px-3 py-1.5 text-xs font-medium text-neutral-200 hover:bg-neutral-700">View clip</Link>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {pendingApproval > 4 && (
            <p className="mt-3 text-sm text-neutral-400">
              <Link href="/queue" className="font-medium text-amber-300 hover:text-amber-200">See all {pendingApproval} waiting clips →</Link>
            </p>
          )}
          <p className="mt-3 text-xs text-neutral-500">Nothing publishes without your approval — approved clips go to the schedule.</p>
        </Card>
      </div>

      {/* Connected accounts — live status, followers, last post, refresh */}
      <section aria-label="Connected accounts">
        <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-lg font-semibold text-neutral-100">Connected accounts</h2>
          <span className="text-xs text-neutral-500">{live ? "from GET /channels — token status is live" : "demo accounts"}</span>
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
                <Card key={p} className="card-hover flex flex-col">
                  <div className="flex items-start gap-3">
                    <Avatar name={c?.account_name || platformLabel(p)} src={c?.avatar_url} platform={p} />
                    <div className="min-w-0 flex-1">
                      <p className="flex items-center gap-2 font-semibold text-neutral-50">
                        <PlatformDot platform={p} /> {platformLabel(p)}
                      </p>
                      <p className="truncate text-sm text-neutral-400">{c?.account_name || "Not connected yet"}</p>
                    </div>
                  </div>
                  <div className="mt-3">{channelStatusBadge(c?.status ?? "not_connected", c?.health)}</div>
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
                    {connected && c?.expires_at && (
                      <div className="flex justify-between gap-2">
                        <dt className="text-neutral-500">Token expires</dt>
                        <dd className="truncate font-medium text-neutral-100" suppressHydrationWarning>{fmtTime(c.expires_at)}</dd>
                      </div>
                    )}
                    <div>
                      <dt className="text-neutral-500">Automation</dt>
                      <dd className="mt-0.5 font-medium text-neutral-100">{waiting} waiting · {ready} ready · {pub} published</dd>
                    </div>
                  </dl>
                  {c?.review_notice && <p className="mt-3 rounded-md bg-amber-500/10 px-2.5 py-2 text-xs leading-snug text-amber-200">{c.review_notice}</p>}
                  <div className="mt-auto flex gap-2 pt-4">
                    {connected ? (
                      <>
                        <SecondaryButton href="/channels">Manage</SecondaryButton>
                        <button
                          onClick={() => void onRefreshChannel(p)}
                          disabled={!live || busyChannel === p}
                          title={!live ? "Demo data — refresh works when the server is connected." : "Refresh this channel's token status"}
                          className="rounded-md bg-neutral-800 px-3 py-1.5 text-sm font-medium text-neutral-200 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {busyChannel === p ? "Refreshing…" : "Refresh"}
                        </button>
                      </>
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

        {/* Monetization progress vs thresholds + up next */}
        <div className="space-y-4">
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

      {toast && <Toast message={toast.message} tone={toast.tone} onClose={() => setToast(null)} />}
    </div>
  );
}
