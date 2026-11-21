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
import { getJob, apiGet, normalizeCandidates, normalizeJob, pipelineProgress, PROCESSING_STATUSES, retryJob, type Candidate, type Job } from "@/lib/api";
import { demoCandidates, demoJobs } from "@/lib/mock";
import { Card, ErrorBanner, PrimaryButton, ProgressBar, SecondaryButton } from "@/components/ui";
import PipelineSteps from "@/components/PipelineSteps";
import ReviewClient from "./ReviewClient";

interface LogLine { ts: string | null; stage: string; level: string; message: string; }

export default function JobReviewClient({ id }: { id: string }) {
  const [job, setJob] = useState<Job>(demoJobs.find((j) => j.id === id) ?? demoJobs[0]);
  const [candidates, setCandidates] = useState<Candidate[]>(
    demoCandidates.filter((c) => c.job_id === id || id === "job-001"),
  );
  const [live, setLive] = useState(false);
  const [retryNote, setRetryNote] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [logs, setLogs] = useState<LogLine[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetchedJob, fetchedCandidates, health] = await Promise.all([
        getJob(id),
        apiGet<Candidate[]>(`/jobs/${id}/candidates`),
        apiGet<unknown>("/health"),
      ]);
      if (cancelled) return;
      if (fetchedJob !== null) setJob(fetchedJob);
      if (fetchedCandidates !== null) setCandidates(normalizeCandidates(fetchedCandidates));
      setLive(health !== null);
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

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
      setRetryNote("Retry simulated locally (demo mode: API unreachable). A live retry re-runs this same job via POST /jobs/{id}/retry.");
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

  const failed = job.status === "failed";
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-neutral-400">Job {job.id} · {job.source} · {job.status}</p>
        <span className="ml-auto flex gap-2">
          <SecondaryButton href={`/pipeline?job=${encodeURIComponent(job.id)}`}>Full pipeline view</SecondaryButton>
          <SecondaryButton href="/automation">Automation</SecondaryButton>
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
                  {failed ? "Failed — see the error below." : `${progress}% complete · ${job.candidates} clip candidate(s) so far`}
                  {live && PROCESSING_STATUSES.includes(job.status) ? " · live, updating every 10s" : ""}
                </p>
              </>
            );
          })()}
        </div>
        {failed && job.error && (
          <div className="mt-4">
            <ErrorBanner message={`Job failed: ${job.error}`} />
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
