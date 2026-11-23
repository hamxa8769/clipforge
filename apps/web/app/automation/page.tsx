// Automation view (v0.3.0) — the runner state in one place.
//
// - Running jobs (live progress, polled every 10s), queued jobs, what ran
//   (history with outcome + error), what is next (scheduled posts).
// - Retention-feedback feed: the signals GET /analytics returns, which
//   the clip picker should use for the next clip.
// - Automation rules with on/off toggles, stored on the SERVER via
//   GET /automation/rules + PUT /automation/rules (API-CONTRACT.md §1).
//   When the API is unreachable the toggles fall back to this browser's
//   localStorage copy and are labelled demo. The dry-run guard is
//   server-enforced and locked on here; it cannot be switched off
//   from the UI.
// - "Run scheduler now" calls the real POST /scheduler/run (dry-run by
//   default — the same engine the scheduler loop uses).
// - Error state with Retry when a poll fails after the API was live;
//   when the API is unreachable the page shows labelled demo data.
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  getAnalytics, getAutomationRules, getJobs, getPosts, pipelineProgress,
  runScheduler, RUNNING_STATUSES, setAutomationRule, type AnalyticsSnapshot, type Job, type Post,
} from "@/lib/api";
import { demoAnalytics, demoJobs, demoPosts } from "@/lib/mock";
import { Card, CardHeader, DemoBadge, EmptyState, ErrorBanner, PlatformDot, platformLabel, PrimaryButton, ProgressBar, SecondaryButton, Skeleton, StatusPill, Toggle } from "@/components/ui";
import { useServerStatus } from "@/components/ServerStatusProvider";

const RULES_KEY = "clipforge.automation.rules.v1";

const RULE_DEFS = [
  { key: "retention_feedback", label: "Retention-feedback clip picking", hint: "Prefer hooks and lengths that scored well on viewed% / completion in the last analytics snapshot; down-rank patterns that failed." },
  { key: "auto_seo", label: "Auto-SEO variants", hint: "Generate per-platform titles, descriptions and hashtags for every approved clip (YouTube / TikTok / Instagram / Facebook rules)." },
  { key: "staggered_schedule", label: "Staggered scheduling", hint: "Place each platform's post in its own best window (IG 11:00, FB 13:00, YT 15:00, TikTok 19:00) instead of blasting all four at once." },
  { key: "auto_queue_approved", label: "Auto-queue approved clips", hint: "Fan out one approved clip to one pending-approval post per connected platform, respecting the per-platform daily caps. Nothing publishes — posts still wait for approval." },
  { key: "auto_approve_high_score", label: "Auto-approve very high scores", hint: "Off by default and intentionally conservative: only clips scoring 95+ with a hook rating of 9+ skip the human queue. Approval-first stays the default." },
] as const;

const DEFAULT_RULES: Record<string, boolean> = {
  retention_feedback: true,
  auto_seo: true,
  staggered_schedule: true,
  auto_queue_approved: true,
  auto_approve_high_score: false,
};

/** Offline/demo fallback only: the last rules this browser saw, so the
 * toggles still render (labelled demo) when the API is unreachable. */
function loadRules(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(RULES_KEY);
    if (!raw) return DEFAULT_RULES;
    return { ...DEFAULT_RULES, ...(JSON.parse(raw) as Record<string, boolean>) };
  } catch { return DEFAULT_RULES; }
}

export default function AutomationPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [posts, setPosts] = useState<Post[] | null>(null);
  const [analytics, setAnalytics] = useState<AnalyticsSnapshot[] | null>(null);
  // Liveness is the shared server status (v0.9.3) — this page no
  // longer probes /health itself.
  const server = useServerStatus();
  const live = server.live;
  const [pollError, setPollError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [rules, setRules] = useState<Record<string, boolean>>(DEFAULT_RULES);
  const [rulesLive, setRulesLive] = useState(false);
  const [rulesError, setRulesError] = useState<string | null>(null);
  const [runNote, setRunNote] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    const [fetchedJobs, fetchedPosts, fetchedAnalytics, fetchedRules] = await Promise.all([
      getJobs(), getPosts(), getAnalytics(), getAutomationRules(),
    ]);
    const isLive = live;
    if (!isLive) {
      // Demo data only once a probe has actually failed — while the
      // shared status is still "checking", leave the skeletons up so
      // this page never says "unreachable" before the first probe.
      if (server.status === "reconnecting") {
        setJobs(demoJobs); setPosts(demoPosts); setAnalytics(demoAnalytics);
        setRules(loadRules()); setRulesLive(false);
        setPollError(null);
      }
    } else {
      if (fetchedJobs === null || fetchedPosts === null) {
        setPollError("The API stopped answering mid-poll — showing the last good data. Check the server and retry.");
      } else {
        setJobs(fetchedJobs); setPosts(fetchedPosts);
        setAnalytics(fetchedAnalytics ?? []);
        setPollError(null);
      }
      if (fetchedRules !== null) {
        const next: Record<string, boolean> = { ...DEFAULT_RULES };
        for (const r of fetchedRules) next[r.key] = r.enabled;
        setRules(next);
        setRulesLive(true);
        setRulesError(null);
        try { localStorage.setItem(RULES_KEY, JSON.stringify(next)); } catch { /* private mode */ }
      } else {
        setRules(loadRules()); setRulesLive(false);
        setRulesError("Rules could not be read from the server — showing this browser's last copy.");
      }
    }
    setUpdatedAt(new Date());
    // `live` / `server.status` in deps: when the server comes back,
    // load changes identity and the mount effect below re-runs, so
    // this page recovers on its own.
  }, [live, server.status]);

  useEffect(() => {
    let cancelled = false;
    load().catch(() => { if (!cancelled && server.status === "reconnecting") { setJobs(demoJobs); setPosts(demoPosts); setAnalytics(demoAnalytics); setRules(loadRules()); } });
    return () => { cancelled = true; };
  }, [load, server.status]);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => { load().catch(() => {}); }, 10000);
    return () => clearInterval(t);
  }, [live, load]);

  /** Toggle a rule: PUT /automation/rules when the API is live (the
   * server is the source of truth); localStorage only when offline. A
   * failed PUT reverts the toggle and says so — the switch never lies. */
  async function setRule(key: string, value: boolean) {
    const prev = rules[key] ?? DEFAULT_RULES[key];
    setRules((old) => ({ ...old, [key]: value }));
    if (!live || !rulesLive) {
      try { localStorage.setItem(RULES_KEY, JSON.stringify({ ...rules, [key]: value })); } catch { /* private mode */ }
      return;
    }
    const res = await setAutomationRule(key, value);
    if (!res.ok) {
      setRules((old) => ({ ...old, [key]: prev }));
      setRulesError(`Could not save "${key}": ${res.error ?? `HTTP ${res.status}`}. The server value was kept.`);
      return;
    }
    setRulesError(null);
    try { localStorage.setItem(RULES_KEY, JSON.stringify({ ...rules, [key]: value })); } catch { /* private mode */ }
  }

  async function runNow() {
    setRunning(true);
    setRunNote(null);
    const res = await runScheduler(true);
    setRunning(false);
    if (!res.ok) {
      setRunNote(`Scheduler run failed: ${res.error ?? `HTTP ${res.status}`}. Nothing was published.`);
      return;
    }
    const results = (res.data as { results?: Array<{ post_id: string; status: string; message?: string }> } | null)?.results ?? [];
    const done = results.filter((r) => r.status === "published" || r.status === "dry_run" || r.status === "draft_created").length;
    const skipped = results.filter((r) => r.status === "skipped_cap").length;
    const failed = results.length - done - skipped;
    setRunNote(results.length === 0
      ? "Scheduler ran (dry-run): no posts were due right now."
      : `Scheduler ran (dry-run): ${results.length} due post(s) — ${done} processed, ${skipped} skipped (daily cap), ${failed} failed.`);
    load().catch(() => {});
  }

  const loading = jobs === null;
  const runningJobs = (jobs ?? []).filter((j) => RUNNING_STATUSES.includes(j.status));
  const reviewJobs = (jobs ?? []).filter((j) => j.status === "review");
  const queuedJobs = (jobs ?? []).filter((j) => j.status === "pending");
  const history = [...(jobs ?? [])]
    .filter((j) => j.status === "ready" || j.status === "failed")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, 8);
  const nextUp = [...(posts ?? [])]
    .filter((p) => (p.status === "scheduled" || p.status === "approved" || p.status === "pending_approval") && p.scheduled_at)
    .sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at))
    .slice(0, 5);
  const signals = (analytics ?? []).flatMap((s) => s.signals.map((sig) => ({ platform: s.platform, signal: sig })));

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Automation</h1>
        {!live && !loading && <DemoBadge />}
        <span className="text-sm text-neutral-400">
          {live ? "Runner live — polling every 10s" : loading ? "Checking the API…" : "Server not answering — demo data"}
          {updatedAt && <span suppressHydrationWarning> · updated {updatedAt.toLocaleTimeString()}</span>}
        </span>
        <span className="ml-auto flex gap-2">
          <SecondaryButton onClick={() => load()}>Refresh</SecondaryButton>
          <PrimaryButton onClick={runNow} disabled={running || !live}>{running ? "Running…" : "Run scheduler now"}</PrimaryButton>
        </span>
      </div>
      {pollError && <ErrorBanner message={pollError} onRetry={() => load()} />}
      {runNote && (
        <div className={`rounded-xl border p-4 text-sm ${runNote.includes("failed") ? "border-red-500/40 bg-red-500/10 text-red-200" : "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"}`} role="status">
          {runNote}
        </div>
      )}
      {!live && !loading && (
        <p className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-200">
          The scheduler runs on the server (POST /scheduler/run). “Run scheduler now” is disabled until the API is reachable; dry-run is always on unless the server itself is started with live publishing enabled.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Running + queued + waiting for review */}
        <Card>
          <CardHeader title="Running now" hint={`${runningJobs.length} running · ${queuedJobs.length} queued · ${reviewJobs.length} waiting for review`} />
          {loading ? (
            <div className="space-y-3"><Skeleton className="h-16" /><Skeleton className="h-16" /></div>
          ) : runningJobs.length === 0 && queuedJobs.length === 0 && reviewJobs.length === 0 ? (
            <EmptyState title="Runner is idle" body="No jobs are running or queued. New jobs start automatically when created; the scheduler picks up due posts on its own." />
          ) : (
            <ul className="space-y-3">
              {runningJobs.map((j) => (
                <li key={j.id} className="rounded-lg bg-neutral-950 p-4">
                  <div className="flex flex-wrap items-center gap-3">
                    <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="min-w-0 flex-1 truncate font-medium hover:underline">{j.title || j.id}</Link>
                    <StatusPill status={j.status} />
                  </div>
                  <ProgressBar value={pipelineProgress(j.status)} className="mt-3" />
                  <p className="mt-1.5 text-xs text-neutral-500">{pipelineProgress(j.status)}% · {j.candidates} candidates so far</p>
                </li>
              ))}
              {queuedJobs.map((j) => (
                <li key={j.id} className="flex items-center gap-3 rounded-lg bg-neutral-950 px-4 py-3">
                  <span className="min-w-0 flex-1 truncate text-sm text-neutral-300">{j.title || j.id}</span>
                  <span className="text-xs text-neutral-500">Queued — starts next</span>
                  <StatusPill status={j.status} />
                </li>
              ))}
              {reviewJobs.map((j) => (
                <li key={j.id} className="flex items-center gap-3 rounded-lg bg-neutral-950 px-4 py-3">
                  <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="min-w-0 flex-1 truncate text-sm text-neutral-300 hover:underline">{j.title || j.id}</Link>
                  <span className="text-xs text-neutral-500">Waiting for review — {j.candidates} candidates</span>
                  <StatusPill status={j.status} />
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* What is next */}
        <Card>
          <CardHeader title="What is next" hint="scheduled, approved & awaiting approval" action={<Link href="/schedule" className="text-sm font-medium text-amber-300 hover:text-amber-200">Schedule →</Link>} />
          {loading ? (
            <div className="space-y-3"><Skeleton className="h-12" /><Skeleton className="h-12" /><Skeleton className="h-12" /></div>
          ) : nextUp.length === 0 ? (
            <EmptyState title="Nothing scheduled" body="Approve a clip and it is scheduled into its platform's best window. The scheduler publishes due posts automatically — dry-run first." />
          ) : (
            <ul className="space-y-2">
              {nextUp.map((p) => (
                <li key={p.id} className="flex items-center gap-3 rounded-lg bg-neutral-950 px-3 py-2.5">
                  <PlatformDot platform={p.platform} />
                  <span className="text-sm font-medium" suppressHydrationWarning>{p.scheduled_at ? new Date(p.scheduled_at).toLocaleString() : "unscheduled"}</span>
                  <span className="min-w-0 flex-1 truncate text-sm text-neutral-400">{p.caption}</span>
                  <StatusPill status={p.status} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {/* History */}
      <Card>
        <CardHeader title="What ran" hint="latest finished jobs — outcome and error, if any" />
        {loading ? (
          <div className="space-y-3"><Skeleton className="h-12" /><Skeleton className="h-12" /><Skeleton className="h-12" /></div>
        ) : history.length === 0 ? (
          <EmptyState title="No finished jobs yet" body="Finished jobs land here with their outcome. Failed jobs keep their error message and a retry action on the job page." />
        ) : (
          <ul className="divide-y divide-neutral-800">
            {history.map((j) => (
              <li key={j.id} className="flex flex-wrap items-center gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title || j.id}</Link>
                  <p className="truncate text-sm text-neutral-400" suppressHydrationWarning>{j.created_at ? new Date(j.created_at).toLocaleString() : ""} · {j.candidates} candidates</p>
                  {j.status === "failed" && j.error && <p className="mt-1 rounded-md bg-red-500/10 px-2 py-1 text-xs text-red-200">{j.error}</p>}
                </div>
                <StatusPill status={j.status} />
                <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="rounded-md bg-neutral-800 px-3 py-1.5 text-sm hover:bg-neutral-700">{j.status === "failed" ? "Retry" : "Review"}</Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* Retention feedback */}
      <Card>
        <CardHeader title="Retention feedback" hint="from GET /analytics — what the last clips taught us" />
        {loading ? <Skeleton className="h-16" /> : signals.length === 0 ? (
          <p className="text-sm text-neutral-400">No retention signals yet — publish, then re-check viewed% / completion before cutting the next clip.</p>
        ) : (
          <ul className="space-y-2">
            {signals.slice(0, 6).map((s, i) => (
              <li key={i} className="flex items-start gap-3 rounded-lg bg-neutral-950 px-3 py-2.5 text-sm">
                <PlatformDot platform={s.platform} className="mt-1.5" />
                <span><strong className="font-medium">{platformLabel(s.platform)}:</strong> <span className="text-neutral-300">{s.signal}</span></span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* Automation rules */}
      <Card>
        <CardHeader title="Automation rules" hint={rulesLive ? "stored on the server — GET/PUT /automation/rules" : "Server not answering — this browser's copy only (demo)"} />
        {rulesError && <p className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200" role="status">{rulesError}</p>}
        <ul className="space-y-5">
          {RULE_DEFS.map((r) => (
            <li key={r.key} className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-800 pb-5 last:border-0 last:pb-0">
              <Toggle checked={rules[r.key] ?? DEFAULT_RULES[r.key]} onChange={(v) => void setRule(r.key, v)} label={r.label} hint={r.hint} />
            </li>
          ))}
          <li className="flex flex-wrap items-start justify-between gap-3">
            <Toggle checked disabled label="Dry-run guard (server-enforced)" hint="Nothing publishes for real while the server runs with CLIPFORGE_DRY_RUN=true. This cannot be switched off from the UI — change the server setting instead." />
          </li>
        </ul>
        <p className="mt-4 text-xs text-neutral-500">
          {rulesLive
            ? "These switches are the server's rules: the clip picker, SEO generator and scheduler read them from the server on every run."
            : "The server could not be reached, so these switches are this browser's copy only — they take effect once the API is back and the page reloads."}
        </p>
      </Card>
    </div>
  );
}
