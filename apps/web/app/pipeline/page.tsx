// Full pipeline view (v0.5.0) — the whole process, live, for every job.
//
//   Source (ingest) → Transcribe → Score → Clip review → SEO → Approve
//   → Schedule → Publish
//
// Stage state comes from the server only — GET /jobs/{id} detail
// (`stages`: one event per backend stage with status/progress/message)
// plus GET /jobs/{id}/logs and GET /posts for the Approve/Schedule/
// Publish tail. Nothing is invented: a stage with no event is "Waiting",
// and the Approve/Schedule/Publish stages are derived from the real
// posts of this job's candidates (no posts → still waiting). Failed
// stages show the server's error in plain words plus a Retry button
// that calls the existing POST /jobs/{id}/retry.
"use client";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import {
  PIPELINE_STAGES, apiGet, apiPostJson, cancelJob, getJob, getJobLogs, getJobs,
  getPosts, pauseJob, resumeJob, retryJob,
  type Candidate, type Job, type JobLogEntry, type Post,
} from "@/lib/api";
import {
  Card, CardHeader, EmptyState, ErrorBanner, PrimaryButton, ProgressBar,
  SecondaryButton, Skeleton, StatusPill,
} from "@/components/ui";
import { YtDlpInstallSection } from "@/components/YtDlpInstaller";

// Backend stage name (JobStageEvent.stage) -> display stage index.
// "render" produces the clips the Review stage shows, so it maps there.
const BACKEND_STAGE_INDEX: Record<string, number> = {
  ingest: 0, transcribe: 1, score: 2, render: 3, seo: 4, review: 3,
};

type StageState = "pending" | "running" | "done" | "failed" | "blocked";

interface StageView {
  name: string;
  state: StageState;
  progress: number;
  message: string;
  errorCode: string | null;
  duration_s: number | null;
  logs: JobLogEntry[];
}

const STATE_STYLE: Record<StageState, { pill: string; label: string }> = {
  done: { pill: "bg-emerald-500/15 text-emerald-300", label: "Done" },
  running: { pill: "bg-amber-500/15 text-amber-300", label: "In progress" },
  failed: { pill: "bg-red-500/15 text-red-300", label: "Failed" },
  blocked: { pill: "bg-red-500/15 text-red-300", label: "Blocked" },
  pending: { pill: "bg-neutral-700/50 text-neutral-300", label: "Waiting" },
};

function explainStageError(stage: string, errorCode: string | null): string | null {
  // Plain-language translation of the backend's cryptic error codes.
  // Returns null when there is no known code — the stage's own message
  // (shown above) already says what happened, and repeating it in red
  // would just duplicate the same sentence.
  if (errorCode === "ffmpeg_missing") return "FFmpeg is not installed — install it from Settings, then retry.";
  if (errorCode === "yt_dlp_missing") return "yt-dlp is not installed — install it below, then retry the job.";
  if (errorCode === "transcription_backend_missing") return "No transcription tool is available (Whisper / API key missing) — check Settings, then retry.";
  if (errorCode === "llm_missing") return "No AI key is saved — titles fall back to templates, but this step needs one. Save a key in Settings, then retry.";
  return null;
}

function deriveStages(job: Job, logs: JobLogEntry[], posts: Post[], candidateIds: Set<string>): StageView[] {
  const jobPosts = posts.filter((p) => candidateIds.has(p.clip_id));
  // Latest event per backend stage (events arrive oldest-first).
  const latestByBackend: Record<string, { status: string; progress_pct: number; message: string; error_code: string | null; duration_s: number | null }> = {};
  for (const ev of job.stages) {
    latestByBackend[ev.stage] = {
      status: ev.status, progress_pct: ev.progress_pct, message: ev.message,
      error_code: ev.error_code, duration_s: ev.duration_s,
    };
  }
  const stageLogs = (backendStages: string[]): JobLogEntry[] =>
    logs.filter((l) => backendStages.includes(l.stage));

  const views: StageView[] = PIPELINE_STAGES.map((name, i) => ({
    name, state: "pending" as StageState, progress: 0, message: "", errorCode: null, duration_s: null, logs: [],
  }));

  // Stages 0–4 from backend stage events; fall back to the job status
  // when a stage never wrote an event (job never ran that far).
  const fallbackActive = job.status === "failed" ? -1 : (
    { pending: 0, transcribing: 1, scoring: 2, rendering: 3, review: 3, ready: 5 } as Record<string, number>
  )[job.status] ?? 0;
  for (let i = 0; i <= 4; i++) {
    const backendStages = Object.keys(BACKEND_STAGE_INDEX).filter((b) => BACKEND_STAGE_INDEX[b] === i);
    const view = views[i];
    view.logs = stageLogs(backendStages);
    const events = backendStages.map((b) => latestByBackend[b]).filter(Boolean);
    const latest = events[events.length - 1];
    if (latest) {
      view.progress = latest.progress_pct;
      view.message = latest.message;
      view.errorCode = latest.error_code;
      view.duration_s = latest.duration_s;
      if (latest.status === "done") { view.state = "done"; view.progress = 100; }
      else if (latest.status === "started") view.state = "running";
      else if (latest.status === "failed") view.state = "failed";
      else if (latest.status === "blocked") view.state = "blocked";
      else if (latest.status === "skipped") { view.state = "done"; view.message = `${view.message || "Skipped — not needed for this job."}`; }
    } else if (job.status === "failed" && job.failed_stage && BACKEND_STAGE_INDEX[job.failed_stage] === i) {
      view.state = "failed";
      view.message = job.error ?? "";
    } else if (i < fallbackActive) {
      view.state = "done"; view.progress = 100;
    } else if (i === fallbackActive && job.status !== "pending" && job.status !== "ready") {
      view.state = "running"; view.progress = job.progress_pct;
    }
  }

  // Stages 5–7 from the job's real posts (Approve / Schedule / Publish).
  const has = (statuses: string[]) => jobPosts.some((p) => statuses.includes(p.status));
  views[5].logs = stageLogs(["review"]);
  if (has(["approved", "scheduled", "publishing", "published"])) {
    views[5].state = "done"; views[5].progress = 100;
    views[5].message = `${jobPosts.filter((p) => ["approved", "scheduled", "publishing", "published"].includes(p.status)).length} clip(s) approved.`;
  } else if (has(["pending_approval", "draft"])) {
    views[5].state = "running";
    views[5].message = "Clips are waiting for your approval in the review list.";
  }
  if (has(["scheduled", "publishing", "published"])) {
    views[6].state = "done"; views[6].progress = 100;
    views[6].message = `${jobPosts.filter((p) => ["scheduled", "publishing", "published"].includes(p.status)).length} post(s) scheduled or published.`;
  } else if (has(["approved"])) {
    views[6].state = "running";
    views[6].message = "Approved — waiting to be scheduled.";
  }
  if (has(["published"])) {
    views[7].state = "done"; views[7].progress = 100;
    views[7].message = `${jobPosts.filter((p) => p.status === "published").length} post(s) published.`;
  } else if (has(["publishing"])) {
    views[7].state = "running"; views[7].progress = 50;
    views[7].message = "Publishing now…";
  } else if (has(["scheduled"])) {
    views[7].state = "running";
    views[7].message = "Scheduled — waiting for its publish time.";
  }
  const failedPost = jobPosts.find((p) => p.status === "failed");
  if (failedPost && views[7].state !== "done") {
    views[7].state = "failed";
    views[7].message = failedPost.error ?? "Publishing failed.";
  }
  return views;
}

function PipelinePageInner() {
  const params = useSearchParams();
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(params.get("job"));
  const [job, setJob] = useState<Job | null>(null);
  const [logs, setLogs] = useState<JobLogEntry[]>([]);
  const [posts, setPosts] = useState<Post[]>([]);
  const [candidateIds, setCandidateIds] = useState<Set<string>>(new Set());
  const [loaded, setLoaded] = useState(false);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryMsg, setRetryMsg] = useState<string | null>(null);
  const [controlling, setControlling] = useState(false);
  const [controlMsg, setControlMsg] = useState<string | null>(null);

  const loadJobs = useCallback(async () => {
    const fetched = await getJobs();
    setJobs(fetched);
    setLive(fetched !== null);
    if (fetched && fetched.length > 0) {
      setSelectedId((prev) => (prev && fetched.some((j) => j.id === prev) ? prev : fetched[0].id));
    }
    setLoaded(true);
  }, []);

  useEffect(() => { loadJobs().catch(() => setLoaded(true)); }, [loadJobs]);

  const loadDetail = useCallback(async (id: string) => {
    const [detail, fetchedLogs, fetchedPosts, candidates] = await Promise.all([
      getJob(id),
      getJobLogs(id),
      getPosts(),
      apiGet<Candidate[]>(`/jobs/${encodeURIComponent(id)}/candidates`),
    ]);
    if (detail) {
      setJob(detail);
      setError(null);
    } else {
      setError("Could not load this job — the server did not answer.");
    }
    setLogs(fetchedLogs ?? []);
    setPosts(fetchedPosts ?? []);
    setCandidateIds(new Set((candidates ?? []).map((c) => c.id)));
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    loadDetail(selectedId).catch(() => {});
    // Live refresh while the job is still processing.
    const t = window.setInterval(() => {
      getJob(selectedId).then((j) => {
        if (j && ["pending", "transcribing", "scoring", "rendering", "review"].includes(j.status)) {
          loadDetail(selectedId).catch(() => {});
        }
      }).catch(() => {});
    }, 5000);
    return () => window.clearInterval(t);
  }, [selectedId, loadDetail]);

  const stages = useMemo(
    () => (job ? deriveStages(job, logs, posts, candidateIds) : []),
    [job, logs, posts, candidateIds],
  );
  const doneCount = stages.filter((s) => s.state === "done").length;
  const failedStage = stages.find((s) => s.state === "failed" || s.state === "blocked");
  // Which job controls make sense for the current status (the server
  // 409s the rest — e.g. a job in review / ready cannot be cancelled
  // from here, so Cancel is hidden there instead of failing).
  const canRun = job?.status === "pending";
  const canPause = job ? ["queued", "running", "transcribing", "scoring", "rendering"].includes(job.status) : false;
  const canResume = job?.status === "paused";
  const canCancel = job ? ["pending", "queued", "running", "paused", "transcribing", "scoring", "rendering"].includes(job.status) : false;

  async function onRetry() {
    if (!job) return;
    setRetrying(true);
    setRetryMsg(null);
    const res = await retryJob(job);
    setRetrying(false);
    if (res?.ok) {
      setRetryMsg("Retrying — the job runs again from the start.");
      await loadDetail(job.id);
    } else {
      setRetryMsg(res?.error ?? "Retry failed.");
    }
  }

  // Job controls (v0.9.0): Run / Pause / Resume / Cancel. Run uses
  // wait=false + a refresh — a run takes minutes and the POST helper
  // times out after 5s, so the UI must never block on a synchronous run.
  async function onControl(action: "run" | "pause" | "resume" | "cancel") {
    if (!job) return;
    setControlling(true);
    setControlMsg(null);
    const res = action === "run"
      ? await apiPostJson(`/jobs/${encodeURIComponent(job.id)}/run?wait=false`)
      : action === "pause" ? await pauseJob(job.id)
      : action === "resume" ? await resumeJob(job.id)
      : await cancelJob(job.id);
    setControlling(false);
    if (!res.ok) {
      setControlMsg(res.error ?? `Could not ${action} the job.`);
      return;
    }
    setControlMsg(
      action === "run" ? "Job started — it is now in the queue; jobs run one at a time."
      : action === "pause" ? "Job paused — it stops between stages; resume to continue."
      : action === "resume" ? "Job resumed."
      : "Job cancelled.",
    );
    await loadDetail(job.id);
    await loadJobs();
  }

  if (!loaded) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" /><Skeleton className="h-48" /><Skeleton className="h-64" />
      </div>
    );
  }
  if (!live) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold tracking-tight">Pipeline</h1>
        <ErrorBanner message="The server did not answer, so no pipeline can be shown. Start the server, then reload this page." onRetry={() => loadJobs()} />
      </div>
    );
  }
  if (!jobs || jobs.length === 0) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold tracking-tight">Pipeline</h1>
        <EmptyState
          title="No jobs yet"
          body="The pipeline shows every step a job goes through — ingest, transcribe, score, clip review, SEO, approve, schedule, publish. Ingest a video first and it will appear here."
          action={<PrimaryButton href="/automation">Go to Jobs</PrimaryButton>}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Pipeline</h1>
        <p className="mt-1 text-sm text-neutral-400">
          The full process, live — Source → Transcribe → Score → Clip review → SEO → Approve → Schedule → Publish.
          Every stage shows its real status from the server; nothing here is a demo.
        </p>
      </div>

      {error && <ErrorBanner message={error} onRetry={() => selectedId && loadDetail(selectedId)} />}

      <Card>
        <CardHeader title="Jobs" hint={`${jobs.length} job(s) — pick one to see its pipeline`} />
        <div className="flex flex-wrap gap-2">
          {jobs.map((j) => (
            <button
              key={j.id}
              onClick={() => setSelectedId(j.id)}
              className={`rounded-lg border px-3 py-2 text-left text-sm ${j.id === selectedId ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-800 bg-neutral-950 text-neutral-300 hover:border-neutral-600"}`}
            >
              <span className="block max-w-56 truncate font-medium">{j.title || j.source || j.id}</span>
              <span className="mt-1 flex items-center gap-2 text-xs text-neutral-400">
                <StatusPill status={j.status} />
                <span>{j.progress_pct}%</span>
              </span>
            </button>
          ))}
        </div>
      </Card>

      {job && (
        <>
          <Card>
            <CardHeader
              title={job.title || job.source || job.id}
              hint={job.started_at ? `Started ${new Date(job.started_at).toLocaleString()}` : job.created_at ? `Created ${new Date(job.created_at).toLocaleString()}` : undefined}
              action={<Link href={`/jobs/view/?id=${encodeURIComponent(job.id)}`} className="text-sm text-sky-400 underline">Open job</Link>}
            />
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <StatusPill status={job.status} />
              <span className="text-neutral-400">{doneCount} of {PIPELINE_STAGES.length} stages done</span>
              {job.duration_s !== null && job.duration_s !== undefined && (
                <span className="text-neutral-400">took {Math.round(job.duration_s)}s</span>
              )}
              {job.blocked_by && <span className="text-red-300">Blocked by: {job.blocked_by}</span>}
            </div>
            {(canRun || canPause || canResume || canCancel) && (
              <div className="mt-4 flex flex-wrap items-center gap-2">
                {canRun && <PrimaryButton onClick={() => onControl("run")} disabled={controlling}>{controlling ? "Starting…" : "Run job"}</PrimaryButton>}
                {canPause && <PrimaryButton onClick={() => onControl("pause")} disabled={controlling}>{controlling ? "Working…" : "Pause"}</PrimaryButton>}
                {canResume && <PrimaryButton onClick={() => onControl("resume")} disabled={controlling}>{controlling ? "Working…" : "Resume"}</PrimaryButton>}
                {canCancel && <SecondaryButton onClick={() => onControl("cancel")} disabled={controlling}>{controlling ? "Working…" : "Cancel job"}</SecondaryButton>}
                {canRun && <span className="text-xs text-neutral-400">Jobs don&apos;t start on their own — press Run. They run one at a time.</span>}
                {controlMsg && <span className="text-sm text-neutral-300">{controlMsg}</span>}
              </div>
            )}
            <ProgressBar value={job.progress_pct} className="mt-3" />
            {job.status === "failed" && (
              <div className="mt-4 rounded-lg border border-red-500/40 bg-red-500/10 p-4">
                <p className="text-sm font-medium text-red-200">This job failed{job.failed_stage ? ` at the ${job.failed_stage} step` : ""}.</p>
                {job.error && <p className="mt-1 text-sm text-red-200/80">{job.error}</p>}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <PrimaryButton onClick={onRetry} disabled={retrying}>{retrying ? "Retrying…" : "Retry job"}</PrimaryButton>
                  <span className="text-xs text-neutral-400">Retry runs the same job again from the start.</span>
                </div>
                {retryMsg && <p className="mt-2 text-sm text-neutral-300">{retryMsg}</p>}
              </div>
            )}
            {job.status === "blocked" && (
              <div className="mt-4 rounded-lg border border-red-500/40 bg-red-500/10 p-4">
                <p className="text-sm font-medium text-red-200">This job is blocked{job.failed_stage ? ` at the ${job.failed_stage} step` : ""} — a tool it needs is missing.</p>
                {job.error && <p className="mt-1 text-sm text-red-200/80">{job.error}</p>}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <PrimaryButton onClick={onRetry} disabled={retrying}>{retrying ? "Retrying…" : "Retry job"}</PrimaryButton>
                  <span className="text-xs text-neutral-400">Fix the missing tool on the blocked stage below, then retry — the job runs again from the start.</span>
                </div>
                {retryMsg && <p className="mt-2 text-sm text-neutral-300">{retryMsg}</p>}
              </div>
            )}
          </Card>

          <ol className="space-y-4">
            {stages.map((stage, i) => {
              const style = STATE_STYLE[stage.state];
              return (
                <li key={stage.name}>
                  <Card>
                    <div className="flex flex-wrap items-center gap-3">
                      <span className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-bold ${stage.state === "done" ? "bg-emerald-500 text-neutral-950" : stage.state === "running" ? "bg-amber-400 text-neutral-950" : stage.state === "failed" || stage.state === "blocked" ? "bg-red-500 text-white" : "bg-neutral-800 text-neutral-400"}`}>
                        {stage.state === "done" ? "✓" : stage.state === "failed" || stage.state === "blocked" ? "!" : i + 1}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="font-semibold text-neutral-100">{stage.name}</h3>
                          <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${style.pill}`}>{style.label}</span>
                          {stage.duration_s !== null && stage.duration_s !== undefined && (
                            <span className="text-xs text-neutral-500">{Math.round(stage.duration_s)}s</span>
                          )}
                        </div>
                        {stage.message && <p className="mt-0.5 text-sm text-neutral-400">{stage.message}</p>}
                        {(stage.state === "failed" || stage.state === "blocked") && explainStageError(stage.name, stage.errorCode) && (
                          <p className="mt-1 text-sm text-red-300">{explainStageError(stage.name, stage.errorCode)}</p>
                        )}
                        {(stage.state === "failed" || stage.state === "blocked") && stage.errorCode === "yt_dlp_missing" && (
                          <div className="mt-3 rounded-lg border border-neutral-800 bg-neutral-950 p-3">
                            <YtDlpInstallSection onInstalled={() => { loadDetail(job.id).catch(() => {}); }} />
                            <div className="mt-3 flex flex-wrap items-center gap-2">
                              <PrimaryButton onClick={onRetry} disabled={retrying}>{retrying ? "Retrying…" : "Retry job"}</PrimaryButton>
                              <span className="text-xs text-neutral-400">Install yt-dlp above, then retry — the job runs again from the start.</span>
                            </div>
                            {retryMsg && <p className="mt-2 text-sm text-neutral-300">{retryMsg}</p>}
                          </div>
                        )}
                      </div>
                      <span className="w-24 text-right text-sm text-neutral-300">{Math.round(stage.progress)}%</span>
                    </div>
                    <ProgressBar value={stage.progress} tone={stage.state === "failed" || stage.state === "blocked" ? "danger" : stage.state === "done" ? "success" : "default"} className="mt-3" />
                    {stage.logs.length > 0 && (
                      <details className="mt-3">
                        <summary className="cursor-pointer text-sm text-neutral-400 hover:text-neutral-200">
                          Stage log ({stage.logs.length} line{stage.logs.length === 1 ? "" : "s"})
                        </summary>
                        <ul className="mt-2 max-h-48 space-y-1 overflow-auto rounded-lg bg-neutral-950 p-3 font-mono text-xs leading-relaxed">
                          {stage.logs.map((l, li) => (
                            <li key={li} className={l.level === "error" ? "text-red-300" : "text-neutral-300"}>
                              {l.ts ? <span className="text-neutral-500">{new Date(l.ts).toLocaleTimeString()} </span> : null}
                              {l.message}
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </Card>
                </li>
              );
            })}
          </ol>

          {failedStage && job.status !== "failed" && (
            <p className="text-sm text-neutral-400">
              The {failedStage.name} step failed — fix the cause above, then retry the job from its card.
            </p>
          )}
        </>
      )}
    </div>
  );
}

export default function PipelinePage() {
  return (
    <Suspense fallback={<div className="space-y-4"><Skeleton className="h-8 w-64" /><Skeleton className="h-48" /></div>}>
      <PipelinePageInner />
    </Suspense>
  );
}
