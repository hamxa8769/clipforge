// Client component: job + candidates + liveness are fetched in the browser
// (useEffect) instead of at render time on the server, so this page can be
// statically exported. Same UI, same API contract (lib/api.ts), same demo
// fallback as before (unknown id falls back to the first demo job, exactly
// like the old server version).
//
// v0.3.0 — workflow visibility: the 8-stage pipeline is rendered above
// the review list with per-stage status (including the reported failed
// stage), a progress bar, the job's error ("run log", from
// GET /jobs/{id}/logs) and a retry action. While the job is still
// processing the page polls every 10s so stages advance live. Retry
// re-runs the SAME job via POST /jobs/{id}/retry (API-CONTRACT.md §2) —
// old candidates are dropped and re-derived, stage history is kept.
"use client";
import { useEffect, useState } from "react";
import { getJob, apiGet, cancelJob, normalizeCandidates, normalizeJob, pauseJob, pipelineProgress, PROCESSING_STATUSES, resumeJob, retryJob, type Candidate, type Job } from "@/lib/api";
import { demoCandidates, demoJobs } from "@/lib/mock";
import { useServerStatus } from "@/components/ServerStatusProvider";
import { Card, DangerButton, ErrorBanner, PrimaryButton, ProgressBar, SecondaryButton } from "@/components/ui";
import PipelineSteps from "@/components/PipelineSteps";
import ReviewClient from "./ReviewClient";

interface LogLine { ts: string | null; stage: string; level: string; message: string; }

export default function JobReviewClient({ id }: { id: string }) {
  const [job, setJob] = useState<Job>(demoJobs.find((j) => j.id === id) ?? demoJobs[0]);
  const [candidates, setCandidates] = useState<Candidate[]>(
    demoCandidates.filter((c) => c.job_id === id || id === "job-001"),
  );
  // Liveness is the shared server status (v0.9.3) — this page no
  // longer probes /health itself.
  const server = useServerStatus();
  const live = server.live;
  const [retryNote, setRetryNote] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [controlling, setControlling] = useState(false);
  const [logs, setLogs] = useState<LogLine[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetchedJob, fetchedCandidates] = await Promise.all([
        getJob(id),
        apiGet<Candidate[]>(`/jobs/${id}/candidates`),
      ]);
      if (cancelled) return;
      if (fetchedJob !== null) setJob(fetchedJob);
      if (fetchedCandidates !== null) setCandidates(normalizeCandidates(fetchedCandidates));
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  // When the server comes back after being down, reload the job on
  // our own instead of showing the demo seed until a manual refresh.
  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    (async () => {
      const [fetchedJob, fetchedCandidates] = await Promise.all([
        getJob(id),
        apiGet<Candidate[]>(`/jobs/${id}/candidates`),
      ]);
      if (cancelled) return;
      if (fetchedJob !== null) setJob(fetchedJob);
      if (fetchedCandidates !== null) setCandidates(normalizeCandidates(fetchedCandidates));
    })();
    return () => {
      cancelled = true;
    };
  }, [live, id]);

  // Poll while the job is still processing so the pipeline advances live.
  useEffect(() => {
    if (!live || !PROCESSING_STATUSES.includes(job.status)) return;
    const t = setInterval(async () => {
      const [fetchedJob, fetchedCandidates] = await Promise.all([
        getJob(id), apiGet<Candidate[]>(`/jobs/${id}/candidates`),
      ]);
      if (fetchedJob !== null) setJob(fetchedJob);
      if (fetchedCandidates !== null) setCandidates(normalizeCandidates(fetchedCandidates));
    }, 10000);
    return () => clearInterval(t);
  }, [live, job.status, id]);

  // Per-stage run log for a failed job (GET /jobs/{id}/logs, contract §2).
  useEffect(() => {
    if (!live || job.status !== "failed") { setLogs(null); return; }
    let cancelled = false;
    apiGet<{ logs?: LogLine[] }>(`/jobs/${encodeURIComponent(id)}/logs`).then((res) => {
      if (!cancelled) setLogs(res?.logs ?? []);
    }).catch(() => { if (!cancelled) setLogs([]); });
    return () => { cancelled = true; };
  }, [live, job.status, id]);

  async function onRetry() {
    setRetrying(true);
    setRetryNote(null);
    if (!live) {
      // Demo mode: simulate the retry locally — no API to call.
      setJob((j) => ({ ...j, status: "pending", error: null, failed_stage: null }));
      setRetryNote("Retry simulated locally (demo mode: server not answering). A live retry re-runs this same job via POST /jobs/{id}/retry.");
      setRetrying(false);
      return;
    }
    const res = await retryJob(job);
    setRetrying(false);
    if (res === null) {
      setRetryNote("Cannot retry: this job has no id.");
      return;
    }
    if (!res.ok) {
      setRetryNote(`Retry failed: ${res.error ?? `HTTP ${res.status}`}.`);
      return;
    }
    if (res.data) setJob(normalizeJob(res.data));
    setRetryNote("Job re-queued — the same job is running again (old candidates dropped and re-derived; stage history kept).");
  }

  // v0.9.0 job control: pause / resume / cancel that actually take
  // effect (API-CONTRACT.md §6b). A running job pauses between stages
  // / clips; a cancel kills the render and the job lands in
  // `cancelled` (the response carries `cancelling: true` until the
  // worker confirms it). After any control action the job is re-fetched
  // so the page shows the real state.
  async function onControl(action: "pause" | "resume" | "cancel") {
    if (!live) {
      setRetryNote(`Demo mode — ${action} needs the server. Connect ClipForge to use job controls.`);
      return;
    }
    setControlling(true);
    setRetryNote(null);
    const res = action === "pause" ? await pauseJob(job.id) : action === "resume" ? await resumeJob(job.id) : await cancelJob(job.id);
    setControlling(false);
    if (!res.ok) {
      setRetryNote(`${action[0].toUpperCase() + action.slice(1)} failed: ${res.error ?? `HTTP ${res.status}`}.`);
      return;
    }
    if (res.data) setJob(normalizeJob(res.data));
    const cancelling = (res.data as Record<string, unknown> | null)?.cancelling === true;
    setRetryNote(
      action === "pause"
        ? "Paused — resume when ready, nothing is lost. The job continues from the next stage / clip."
        : action === "resume"
          ? "Resumed — the job continues from where it stopped."
          : cancelling
            ? "Cancelling — the worker is stopping the job now."
            : "Job cancelled.",
    );
  }

  const failed = job.status === "failed";
  const paused = job.status === "paused";
  const queued = job.status === "queued" || job.status === "pending";
  const running = ["running", "transcribing", "scoring", "rendering"].includes(job.status);
  const cancellable = !["review", "ready", "cancelled"].includes(job.status);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-neutral-400">Job {job.id} · {job.source} · {job.status}</p>
        <span className="ml-auto flex gap-2">
          <SecondaryButton href={`/pipeline?job=${encodeURIComponent(job.id)}`}>Full pipeline view</SecondaryButton>
          <SecondaryButton href="/automation">Automation</SecondaryButton>
          {paused && <PrimaryButton onClick={() => void onControl("resume")} disabled={controlling || !live}>{controlling ? "Working…" : "Resume"}</PrimaryButton>}
          {(queued || running) && <PrimaryButton onClick={() => void onControl("pause")} disabled={controlling || !live}>{controlling ? "Working…" : "Pause"}</PrimaryButton>}
          {cancellable && <DangerButton onClick={() => void onControl("cancel")} disabled={controlling || !live}>{controlling ? "Working…" : "Cancel"}</DangerButton>}
          {failed && <PrimaryButton onClick={onRetry} disabled={retrying}>{retrying ? "Retrying…" : "Retry job"}</PrimaryButton>}
        </span>
      </div>
      <h1 className="sr-only">{job.title}</h1>

      {/* Pipeline visibility */}
      <Card>
        <h2 className="mb-4 text-lg font-semibold">Pipeline</h2>
        <PipelineSteps job={job} />
        <div className="mt-4">
          {/* Prefer the server's real progress (GET /jobs/{id} progress_pct):
              the status-derived estimate said 38% for a finished review job
              whose run was actually 95% done. Fall back to the estimate
              only when the server reports no progress yet (0). */}
          {(() => {
            const progress = job.progress_pct > 0 ? Math.round(job.progress_pct) : pipelineProgress(job.status);
            return (
              <>
                <ProgressBar value={failed ? 0 : progress} tone={failed ? "danger" : progress >= 100 ? "success" : "default"} />
                <p className="mt-1.5 text-xs text-neutral-500">
                  {failed ? "Failed — see the error below." : paused ? "Paused — resume to continue from where it stopped, nothing is re-done." : queued ? (job.queue_position ? `Queued — position ${job.queue_position}, waiting for the worker. Only one job runs at a time.` : "Queued — waiting for the worker. Only one job runs at a time.") : `${progress}% complete · ${job.candidates} clip candidate(s) so far`}
                  {live && PROCESSING_STATUSES.includes(job.status) ? " · live, updating every 10s" : ""}
                </p>
              </>
            );
          })()}
        </div>
        {failed && job.error && (
          <div className="mt-4">
            <ErrorBanner message={`Job failed: ${job.error_title ? `${job.error_title} — ${job.error}` : job.error}`} />
            {job.error_action && (
              <p className="mt-2 text-sm text-neutral-300">{job.error_action}</p>
            )}
            <p className="mt-2 text-xs text-neutral-500">
              {job.failed_stage ? `Failed stage: ${job.failed_stage}. ` : ""}
              {job.duration_s !== null && job.duration_s !== undefined ? `Last run took ${Math.round(job.duration_s)}s. ` : ""}
              Retry re-runs this same job (POST /jobs/{id}/retry); the per-stage log below is the audit trail.
            </p>
            {logs !== null && logs.length > 0 && (
              <ul className="mt-3 max-h-48 space-y-1 overflow-y-auto rounded-lg bg-neutral-950 p-3 font-mono text-xs">
                {logs.map((l, i) => (
                  <li key={i} className={l.level === "error" ? "text-red-300" : "text-neutral-300"}>
                    <span className="text-neutral-500">[{l.stage}]</span> {l.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {retryNote && (
          <p className={`mt-4 rounded-md px-3 py-2 text-sm ${retryNote.includes("failed") ? "bg-red-500/10 text-red-200" : "bg-emerald-500/10 text-emerald-200"}`} role="status">
            {retryNote}
          </p>
        )}
      </Card>

      <ReviewClient candidates={candidates} job={job} demo={!live} />
    </div>
  );
}
