// API client + clearly-labelled demo fallback.
// Contract (FastAPI, base http://localhost:8000):
//   GET  /health -> { status, dry_run }
//   GET  /jobs -> Job[] ; GET /jobs/{id} -> Job ; GET /jobs/{id}/candidates -> Candidate[]
//   POST /jobs/{id}/candidates/{n}/approve | reject | trim {start_s, end_s}
//   GET  /media/clips/{job_id}/{filename} -> rendered 9:16 clip file
//   GET  /media/uploads/{filename} -> source upload file
//   GET  /posts -> Post[] (approval queue) ; POST /posts/{id}/approve ; POST /posts/{id}/publish
//   GET  /monetization -> Monetization[]
//   GET  /analytics -> AnalyticsSummary
// When the API is unreachable, callers use mock data and must show a "Demo data" badge.
//
// Job status contract: the backend sends the JobStatus enum
// (src/clipforge/db/__init__.py) — "pending" is the first state; there is
// NO "queued" value on the wire. Older frontend code assumed "queued",
// which silently broke the "Processing" count and the status pill. Every
// job payload from the API goes through normalizeJobStatus() so a stray
// "queued" (or any unknown value) can never leak into the UI unmapped.
export function resolveApiUrl(value: string | undefined): string {
  // Empty string is meaningful: it means "same origin" (relative fetches
  // like "/jobs"), which is what the desktop app needs — the static web
  // export and the API are served from the same origin/port, so no host
  // or port may be baked in. Only an *unset* variable falls back to the
  // dev default. (A `??` fallback would wrongly turn "" into localhost.)
  return value === undefined ? "http://localhost:8000" : value;
}

export const API_URL = resolveApiUrl(process.env.NEXT_PUBLIC_API_URL);

/** Canonical job statuses, exactly as the backend JobStatus enum sends them. */
export type JobStatus =
  | "pending"
  | "transcribing"
  | "scoring"
  | "rendering"
  | "review"
  | "ready"
  | "failed";

export const JOB_STATUSES: readonly JobStatus[] = [
  "pending",
  "transcribing",
  "scoring",
  "rendering",
  "review",
  "ready",
  "failed",
];

/** Statuses that count as "still processing" on the dashboard. */
export const PROCESSING_STATUSES: readonly JobStatus[] = [
  "pending",
  "transcribing",
  "scoring",
  "rendering",
  "review",
];

/**
 * Statuses that count as "running right now" on the Automation page:
 * the worker is actively doing something. "pending" is NOT here — a
 * pending job has not started yet (it is "queued"), and "review" is
 * NOT here either — a job in review has finished rendering and is
 * waiting for a human, so calling it "running" would mislead.
 */
export const RUNNING_STATUSES: readonly JobStatus[] = [
  "transcribing",
  "scoring",
  "rendering",
];

/**
 * Normalise a raw job status from the wire (or from mock/demo data) to a
 * canonical JobStatus. Type guard: always returns a valid JobStatus.
 *
 * - "queued" is a legacy alias the old frontend invented; the backend has
 *   only ever sent "pending" for a not-yet-started job, so it maps here.
 * - Known backend values pass through unchanged.
 * - Anything else (unknown future value, null, non-string) falls back to
 *   "pending" — the safest display state — rather than rendering raw text
 *   the UI has no style or meaning for.
 */
export function normalizeJobStatus(raw: unknown): JobStatus {
  if (typeof raw !== "string") return "pending";
  if (raw === "queued") return "pending";
  if ((JOB_STATUSES as readonly string[]).includes(raw)) return raw as JobStatus;
  return "pending";
}

/** Post statuses, exactly as the backend PostStatus enum sends them. */
export type PostStatus =
  | "draft"
  | "pending_approval"
  | "approved"
  | "scheduled"
  | "publishing"
  | "published"
  | "failed"
  | "dry_run";

export const POST_STATUSES: readonly PostStatus[] = [
  "draft",
  "pending_approval",
  "approved",
  "scheduled",
  "publishing",
  "published",
  "failed",
  "dry_run",
];

/**
 * Normalise a raw post status. The old demo data used "pending", which is
 * not a PostStatus — it means the post is waiting for approval.
 */
export function normalizePostStatus(raw: unknown): PostStatus {
  if (typeof raw !== "string") return "pending_approval";
  if (raw === "pending" || raw === "queued") return "pending_approval";
  if ((POST_STATUSES as readonly string[]).includes(raw)) return raw as PostStatus;
  return "pending_approval";
}

/** One job, exactly as GET /jobs / GET /jobs/{id} returns it. Detail fields
 * (stage/progress/failed_stage/run timing/stages/worker_runs) are only
 * present on GET /jobs/{id} (detail=True) — see docs/API-CONTRACT.md §2. */
export interface StageEvent { stage: string; status: string; progress_pct: number; message: string; error_code: string | null; duration_s: number | null; created_at: string | null; }
export interface WorkerRun { id: string; status: string; outcome: string; error: string | null; duration_s: number | null; started_at: string | null; finished_at: string | null; }
export interface Job {
  id: string; title: string; source: string; status: JobStatus; created_at: string; candidates: number;
  /** Last error, when the backend marked the job failed. */ error: string | null;
  /** "local" | "youtube" — from GET /jobs. */ source_type: string;
  /** Own-content declaration on the original job. */ own_content: boolean;
  /** Live-workflow fields (v0.3.0, GET /jobs/{id} detail): current stage, progress, ETA, demo flag, blocker. */
  stage: string | null; progress_pct: number; eta_s: number | null; demo: boolean; blocked_by: string | null;
  /** Failed stage + run timing (null when the job never ran). */ failed_stage: string | null;
  started_at: string | null; finished_at: string | null; duration_s: number | null;
  stages: StageEvent[]; worker_runs: WorkerRun[];
}
export interface SeoVariant { title: string; description: string; hashtags: string[]; }
export interface Candidate {
  id: string; job_id: string; n: number; score: number; hook_rating: number; hook_line: string;
  reason: string; transcript_excerpt: string; start_s: number; end_s: number;
  status: "pending" | "approved" | "rejected";
  /** Server-side rendered clip path (e.g. data/clips/<job>/clip_01.mp4), if rendered. */
  file_path: string | null;
  seo: Record<string, SeoVariant>;
}
export interface Post { id: string; clip_id: string; platform: string; caption: string; scheduled_at: string; status: PostStatus; /** Last publish error, when the backend marked the post failed. */ error: string | null; /** Publish attempts so far. */ attempts: number; }
export interface Monetization { platform: string; program: string; current: Record<string, number>; threshold: Record<string, number>; progress_pct: number; note?: string; }
export interface Health { status: string; dry_run: boolean; version?: string }

/**
 * A connected social account, exactly as GET /accounts returns it
 * (see _account_out in apps/api/clipforge_api/app.py). `token_configured`
 * only says whether the named env var is set on the server — the UI
 * never sees, asks for, or stores a token value.
 */
export interface SocialAccount {
  id: string; provider: string; label: string; account_ref: string;
  scopes: string; token_env_var: string; status: string;
  daily_cap_override: number | null; token_configured: boolean;
}

/**
 * One analytics snapshot, exactly as GET /analytics returns it
 * (list_analytics in apps/api/clipforge_api/app.py). `signals` are the
 * retention-feedback strings from retention_signals() — they feed the
 * "what to fix in the next clip" UI on the automation page.
 */
export interface AnalyticsSnapshot {
  account_id: string | null; platform: string; date: string | null;
  views: number; followers: number; watch_hours: number;
  shorts_views_90d: number; completion_pct: number | null;
  viewed_pct: number | null; signals: string[];
}

/** Normalise one raw job payload (unknown wire shape) into a typed Job. */
export function normalizeStageEvent(raw: unknown): StageEvent {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    stage: String(r.stage ?? ""),
    status: String(r.status ?? ""),
    progress_pct: typeof r.progress_pct === "number" ? r.progress_pct : 0,
    message: String(r.message ?? ""),
    error_code: typeof r.error_code === "string" ? r.error_code : null,
    duration_s: typeof r.duration_s === "number" ? r.duration_s : null,
    created_at: typeof r.created_at === "string" ? r.created_at : null,
  };
}

export function normalizeWorkerRun(raw: unknown): WorkerRun {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    id: String(r.id ?? ""),
    status: String(r.status ?? ""),
    outcome: String(r.outcome ?? ""),
    error: typeof r.error === "string" ? r.error : null,
    duration_s: typeof r.duration_s === "number" ? r.duration_s : null,
    started_at: typeof r.started_at === "string" ? r.started_at : null,
    finished_at: typeof r.finished_at === "string" ? r.finished_at : null,
  };
}

function asOptString(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function asOptNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function normalizeJob(raw: unknown): Job {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    id: String(r.id ?? ""),
    title: String(r.title ?? ""),
    source: String(r.source ?? ""),
    status: normalizeJobStatus(r.status),
    created_at: String(r.created_at ?? ""),
    candidates: typeof r.candidates === "number" ? r.candidates : 0,
    error: typeof r.error === "string" ? r.error : null,
    source_type: typeof r.source_type === "string" ? r.source_type : "local",
    own_content: r.own_content === true,
    stage: asOptString(r.stage),
    progress_pct: typeof r.progress_pct === "number" ? r.progress_pct : 0,
    eta_s: asOptNumber(r.eta_s),
    demo: r.demo === true,
    blocked_by: asOptString(r.blocked_by),
    failed_stage: asOptString(r.failed_stage),
    started_at: asOptString(r.started_at),
    finished_at: asOptString(r.finished_at),
    duration_s: asOptNumber(r.duration_s),
    stages: Array.isArray(r.stages) ? r.stages.map(normalizeStageEvent) : [],
    worker_runs: Array.isArray(r.worker_runs) ? r.worker_runs.map(normalizeWorkerRun) : [],
  };
}

export function normalizeJobs(raw: unknown): Job[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeJob);
}

/** Normalise a single raw post payload into a typed Post. */
export function normalizePost(raw: unknown): Post {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    id: String(r.id ?? ""),
    clip_id: String(r.clip_id ?? r.clip_candidate_id ?? ""),
    platform: String(r.platform ?? ""),
    caption: String(r.caption ?? ""),
    scheduled_at: String(r.scheduled_at ?? r.scheduled_for ?? ""),
    status: normalizePostStatus(r.status),
    error: typeof r.error === "string" ? r.error : null,
    attempts: typeof r.attempts === "number" ? r.attempts : 0,
  };
}

export function normalizePosts(raw: unknown): Post[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizePost);
}

/** Fetch jobs with statuses normalised (see normalizeJobStatus). */
export async function getJobs(): Promise<Job[] | null> {
  const raw = await apiGet<unknown>("/jobs");
  return raw === null ? null : normalizeJobs(raw);
}

/** Fetch one job with its status normalised. */
export async function getJob(id: string): Promise<Job | null> {
  const raw = await apiGet<unknown>(`/jobs/${encodeURIComponent(id)}`);
  return raw === null ? null : normalizeJob(raw);
}

/** Fetch posts with statuses normalised. */
export async function getPosts(): Promise<Post[] | null> {
  const raw = await apiGet<unknown>("/posts");
  return raw === null ? null : normalizePosts(raw);
}

/** Normalise one raw account payload (GET /accounts) into a SocialAccount. */
export function normalizeAccount(raw: unknown): SocialAccount {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    id: String(r.id ?? ""),
    provider: String(r.provider ?? ""),
    label: String(r.label ?? ""),
    account_ref: String(r.account_ref ?? ""),
    scopes: String(r.scopes ?? ""),
    token_env_var: String(r.token_env_var ?? ""),
    status: String(r.status ?? ""),
    daily_cap_override: typeof r.daily_cap_override === "number" ? r.daily_cap_override : null,
    token_configured: r.token_configured === true,
  };
}

export function normalizeAccounts(raw: unknown): SocialAccount[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeAccount);
}

/** GET /accounts — connected social accounts (empty array when none are configured). */
export async function getAccounts(): Promise<SocialAccount[] | null> {
  const raw = await apiGet<unknown>("/accounts");
  return raw === null ? null : normalizeAccounts(raw);
}

function asNumber(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** Normalise one raw analytics snapshot (GET /analytics) into an AnalyticsSnapshot. */
export function normalizeAnalyticsSnapshot(raw: unknown): AnalyticsSnapshot {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    account_id: typeof r.account_id === "string" ? r.account_id : null,
    platform: String(r.platform ?? ""),
    date: typeof r.date === "string" ? r.date : null,
    views: asNumber(r.views),
    followers: asNumber(r.followers),
    watch_hours: asNumber(r.watch_hours),
    shorts_views_90d: asNumber(r.shorts_views_90d),
    completion_pct: typeof r.completion_pct === "number" ? r.completion_pct : null,
    viewed_pct: typeof r.viewed_pct === "number" ? r.viewed_pct : null,
    signals: Array.isArray(r.signals) ? r.signals.filter((s): s is string => typeof s === "string") : [],
  };
}

export function normalizeAnalytics(raw: unknown): AnalyticsSnapshot[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeAnalyticsSnapshot);
}

/** GET /analytics — stored snapshots, newest first (empty array when no ingest has run). */
export async function getAnalytics(): Promise<AnalyticsSnapshot[] | null> {
  const raw = await apiGet<unknown>("/analytics");
  return raw === null ? null : normalizeAnalytics(raw);
}

/** Latest snapshot per platform (the endpoint returns newest first, so the first row per platform wins). */
export function latestSnapshotPerPlatform(snaps: AnalyticsSnapshot[]): Map<string, AnalyticsSnapshot> {
  const out = new Map<string, AnalyticsSnapshot>();
  for (const s of snaps) {
    if (s.platform && !out.has(s.platform)) out.set(s.platform, s);
  }
  return out;
}

/** GET /monetization — threshold progress per platform (query params default to 0, so pass real numbers when known). */
export async function getMonetization(): Promise<Monetization[] | null> {
  return apiGet<Monetization[]>("/monetization");
}

/**
 * GET /monetization?platform=… with real metrics (from the latest
 * analytics snapshot) so the returned progress_pct reflects the channel,
 * not the endpoint's all-zero defaults. Returns the rows for that one
 * platform (YouTube returns three rows: YPP Early, YPP Full, and the
 * Feb-2027 doubled-threshold row).
 */
export async function getMonetizationFor(
  platform: string,
  metrics: { subscribers?: number; followers?: number; watch_hours?: number; shorts_views?: number; views_30d?: number },
): Promise<Monetization[] | null> {
  const q = new URLSearchParams({ platform });
  if (metrics.subscribers !== undefined) q.set("subscribers", String(metrics.subscribers));
  if (metrics.followers !== undefined) q.set("followers", String(metrics.followers));
  if (metrics.watch_hours !== undefined) q.set("watch_hours", String(metrics.watch_hours));
  if (metrics.shorts_views !== undefined) q.set("shorts_views", String(metrics.shorts_views));
  if (metrics.views_30d !== undefined) q.set("views_30d", String(metrics.views_30d));
  return apiGet<Monetization[]>(`/monetization?${q.toString()}`);
}

/** POST /scheduler/run — run due posts now. Dry-run by default (same as the scheduler loop). */
export async function runScheduler(dryRun = true): Promise<ApiResult> {
  return apiPostJson(`/scheduler/run?dry_run=${dryRun ? "true" : "false"}`);
}

/**
 * Retry a failed / blocked job in place: POST /jobs/{id}/retry resets it
 * to pending and runs it again on the same job (docs/API-CONTRACT.md §2).
 * Returns the ApiResult so the caller can surface the backend's 409
 * ("only failed / blocked jobs can be retried") or refresh the job detail
 * from `data`. Null when the job has no id.
 */
export async function retryJob(job: Job): Promise<ApiResult | null> {
  if (!job.id) return null;
  return apiPostJson(`/jobs/${encodeURIComponent(job.id)}/retry`);
}

/** One automation rule, exactly as GET /automation/rules returns it. */
export interface AutomationRule {
  id: string; key: string; kind: string; label: string;
  enabled: boolean; description: string;
}

export function normalizeAutomationRule(raw: unknown): AutomationRule | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.key !== "string" || typeof r.enabled !== "boolean") return null;
  return {
    id: String(r.id ?? ""),
    key: r.key,
    kind: String(r.kind ?? r.key),
    label: String(r.label ?? r.key),
    enabled: r.enabled,
    description: String(r.description ?? ""),
  };
}

/** GET /automation/rules — all server-side automation rules (contract §1). */
export async function getAutomationRules(): Promise<AutomationRule[] | null> {
  const raw = await apiGet<unknown>("/automation/rules");
  if (raw === null) return null;
  const rules = (raw as { rules?: unknown }).rules;
  if (!Array.isArray(rules)) return [];
  return rules
    .map(normalizeAutomationRule)
    .filter((r): r is AutomationRule => r !== null);
}

async function apiPutJson(path: string, body: unknown = {}): Promise<ApiResult> {
  try {
    const res = await fetch(`${API_URL}${path}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(5000) });
    let data: unknown = null;
    try { data = await res.json(); } catch { /* non-JSON error body */ }
    const detail = (data as { detail?: unknown } | null)?.detail;
    return { ok: res.ok, status: res.status, data, error: res.ok ? undefined : (typeof detail === "string" ? detail : `Request failed (${res.status})`) };
  } catch {
    return { ok: false, status: 0, data: null, error: "API unreachable" };
  }
}

/**
 * PUT /automation/rules — set one rule on/off by its frontend key
 * (retention_feedback, auto_seo, staggered_schedule, auto_queue_approved,
 * auto_approve_high_score). 409 when the key is server-locked
 * (dry_run_guard is controlled by the CLIPFORGE_DRY_RUN server setting).
 */
export async function setAutomationRule(key: string, enabled: boolean): Promise<ApiResult> {
  return apiPutJson("/automation/rules", { key, enabled });
}

// ---------------------------------------------------------------------------
// Pipeline visibility: the 8-stage pipeline (research §2) mapped onto the
// backend JobStatus enum. Used by the job detail page and the dashboard's
// live job rows so every screen shows the same stage names and order.
// ---------------------------------------------------------------------------

export const PIPELINE_STAGES = [
  "Ingest",
  "Transcribe",
  "Score clips",
  "Review",
  "SEO variants",
  "Approve",
  "Schedule",
  "Publish",
] as const;

export type PipelineStageState = "done" | "active" | "failed" | "pending";

/**
 * Map a job status to the index of its currently active pipeline stage.
 * - pending: nothing has run yet — Ingest is next.
 * - transcribing/scoring/rendering: the matching middle stage is active
 *   (rendering produces the clips the Review stage shows).
 * - review: candidates are waiting for a human.
 * - ready: clips + SEO are done; the job waits on Approve.
 * - failed: no stage index — the failed stage comes from the job's
 *   `failed_stage` detail field (docs/API-CONTRACT.md §2), never guessed.
 */
export function activeStageIndex(status: JobStatus): number {
  switch (status) {
    case "pending": return 0;
    case "transcribing": return 1;
    case "scoring": return 2;
    case "rendering": return 3;
    case "review": return 3;
    case "ready": return 5;
    case "failed": return -1;
    default: return 0;
  }
}

/**
 * Backend stage name (GET /jobs/{id} `failed_stage`: ingest, transcribe,
 * score, render, seo, review) → index into PIPELINE_STAGES. -1 when the
 * stage is unknown or absent, so the UI never marks a stage it cannot prove.
 */
const FAILED_STAGE_INDEX: Record<string, number> = {
  ingest: 0, transcribe: 1, score: 2, render: 3, seo: 4, review: 3,
};

export function failedStageIndex(failedStage: string | null | undefined): number {
  if (!failedStage) return -1;
  return FAILED_STAGE_INDEX[failedStage] ?? -1;
}

/**
 * Per-stage state for a job: stages before the active one are done; a
 * failed job marks its reported `failed_stage` (when the backend named
 * one) and leaves the rest unmarked — the error banner + retry carry the
 * failure, and no stage is ever blamed without evidence.
 */
export function pipelineStageStates(status: JobStatus, failedStage: string | null = null): PipelineStageState[] {
  if (status === "failed") {
    const failedIdx = failedStageIndex(failedStage);
    if (failedIdx < 0) return PIPELINE_STAGES.map(() => "pending");
    return PIPELINE_STAGES.map((_, i) => (i < failedIdx ? "done" : i === failedIdx ? "failed" : "pending"));
  }
  const active = activeStageIndex(status);
  return PIPELINE_STAGES.map((_, i) => {
    if (i < active) return "done";
    if (i === active) return "active";
    return "pending";
  });
}

/** Approximate progress (0–100) for a live progress bar, from the active stage. */
export function pipelineProgress(status: JobStatus): number {
  if (status === "ready") return 100;
  if (status === "failed") return 0;
  return Math.round((activeStageIndex(status) / PIPELINE_STAGES.length) * 100);
}

export async function apiGet<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${API_URL}${path}`, { cache: "no-store", signal: AbortSignal.timeout(3000) });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch { return null; }
}

export interface ApiResult { ok: boolean; status: number; data: unknown; error?: string; }

export async function apiPostJson(path: string, body: unknown = {}): Promise<ApiResult> {
  try {
    const res = await fetch(`${API_URL}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(5000) });
    let data: unknown = null;
    try { data = await res.json(); } catch { /* non-JSON error body */ }
    const detail = (data as { detail?: unknown } | null)?.detail;
    return { ok: res.ok, status: res.status, data, error: res.ok ? undefined : (typeof detail === "string" ? detail : `Request failed (${res.status})`) };
  } catch {
    return { ok: false, status: 0, data: null, error: "API unreachable" };
  }
}

export async function apiPost(path: string, body: unknown = {}): Promise<boolean> {
  return (await apiPostJson(path, body)).ok;
}

/** POST /jobs/{jobId}/candidates/{n}/approve — the real endpoint (docs/api.md). */
export async function approveCandidate(jobId: string, n: number): Promise<ApiResult> {
  return apiPostJson(`/jobs/${encodeURIComponent(jobId)}/candidates/${n}/approve`);
}

/** POST /jobs/{jobId}/candidates/{n}/reject — the real endpoint (docs/api.md). */
export async function rejectCandidate(jobId: string, n: number): Promise<ApiResult> {
  return apiPostJson(`/jobs/${encodeURIComponent(jobId)}/candidates/${n}/reject`);
}

/**
 * POST /jobs/{jobId}/candidates/{n}/trim {start_s, end_s}.
 * The backend requires the range to lie inside the candidate's original
 * span (otherwise 422). Returns the ApiResult so callers can surface the
 * 422 message instead of silently failing.
 */
export async function trimCandidate(jobId: string, n: number, start_s: number, end_s: number): Promise<ApiResult> {
  return apiPostJson(`/jobs/${encodeURIComponent(jobId)}/candidates/${n}/trim`, { start_s, end_s });
}

/** Basename of a server-side path (candidates carry e.g. data/clips/<job>/clip_01.mp4). */
function basename(p: string): string {
  return p.replace(/\\/g, "/").split("/").pop() ?? p;
}

/**
 * Playable URL for a candidate's rendered 9:16 clip, served by the API's
 * GET /media/clips/{job_id}/{filename}. Null when the candidate has no
 * rendered file yet (job still rendering, or render failed).
 */
export function clipVideoUrl(c: Pick<Candidate, "job_id" | "file_path">): string | null {
  if (!c.file_path) return null;
  const name = basename(c.file_path);
  if (!name) return null;
  return `${API_URL}/media/clips/${encodeURIComponent(c.job_id)}/${encodeURIComponent(name)}`;
}

/**
 * Playable URL for a job's source upload, served by GET /media/uploads/{filename}.
 * Only usable when the source is a plain file under the server's uploads dir
 * (e.g. a local job created from data/uploads/sample_60s.mp4); absolute paths
 * outside the uploads dir have no media URL and return null.
 */
export function sourceVideoUrl(job: Pick<Job, "source">): string | null {
  if (!job.source) return null;
  // Only a bare filename can plausibly live in the uploads dir; a path with
  // directories is a server-local path the media endpoint must not expose.
  if (job.source.includes("/") || job.source.includes("\\")) return null;
  return `${API_URL}/media/uploads/${encodeURIComponent(job.source)}`;
}

export async function getHealth(): Promise<Health | null> { return apiGet<Health>("/health"); }

/** Channel status, exactly as GET /channels returns it (clipforge.auth.channels). */
export type ChannelStatusValue = "connected" | "expired" | "not_connected";
export interface ChannelStatus {
  platform: string; display_name: string; status: ChannelStatusValue;
  account_name: string; avatar_url: string; account_ref: string;
  scopes: string[]; daily_cap: number; expires_at: string | null;
  source: "oauth" | "env" | "none"; review_notice: string; fallback: string;
  can_refresh: boolean;
}

export function normalizeChannelStatus(raw: unknown): ChannelStatus {
  const r = (raw ?? {}) as Record<string, unknown>;
  const status = String(r.status ?? "not_connected");
  return {
    platform: String(r.platform ?? ""),
    display_name: String(r.display_name ?? r.platform ?? ""),
    status: (status === "connected" || status === "expired" ? status : "not_connected") as ChannelStatusValue,
    account_name: String(r.account_name ?? ""),
    avatar_url: String(r.avatar_url ?? ""),
    account_ref: String(r.account_ref ?? ""),
    scopes: Array.isArray(r.scopes) ? r.scopes.map(String) : [],
    daily_cap: typeof r.daily_cap === "number" ? r.daily_cap : 0,
    expires_at: typeof r.expires_at === "string" ? r.expires_at : null,
    source: (r.source === "oauth" || r.source === "env" ? r.source : "none") as ChannelStatus["source"],
    review_notice: String(r.review_notice ?? ""),
    fallback: String(r.fallback ?? ""),
    can_refresh: r.can_refresh === true,
  };
}

export function normalizeChannels(raw: unknown): ChannelStatus[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeChannelStatus);
}

/** GET /channels — one status card per platform from the real backend. */
export async function getChannels(): Promise<ChannelStatus[] | null> {
  const raw = await apiGet<unknown>("/channels");
  return raw === null ? null : normalizeChannels(raw);
}

/** GET /auth/{platform}/start — authorize URL for the Connect button to open. */
export async function startOAuth(platform: string): Promise<{ authorize_url: string; redirect_uri: string } | null> {
  const raw = await apiGet<unknown>(`/auth/${encodeURIComponent(platform)}/start`);
  if (raw === null) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.authorize_url !== "string") return null;
  return { authorize_url: r.authorize_url, redirect_uri: String(r.redirect_uri ?? "") };
}

/**
 * The exact redirect URI to register in a platform's developer app,
 * derived from the API the UI is actually talking to — never a hardcoded
 * localhost guess. The API server's own port is the redirect base
 * (providers.redirect_base): in dev the UI talks to NEXT_PUBLIC_API_URL
 * (default localhost:8000); in the desktop app the UI is served by the
 * API server itself, so the page's own port is the API port.
 * Returns null when no port can be determined (should not happen in the
 * desktop app or dev — both always have a port).
 */
export function deriveRedirectUri(platform: string): string | null {
  let port = "";
  try {
    if (API_URL) port = new URL(API_URL).port;
  } catch { /* API_URL is relative/empty in the desktop build — fall through */ }
  if (!port && typeof window !== "undefined") port = window.location.port;
  if (!port) return null;
  return `http://127.0.0.1:${port}/auth/callback/${platform}`;
}

/**
 * Redirect URI for a platform, from the API when possible.
 * GET /auth/{platform}/start returns the authoritative URI — but only
 * once the platform's client id is saved (it 409s before that, which is
 * exactly when the wizard needs the URI most). So: use the API's answer
 * when available, otherwise the URI derived from the connected API's
 * own port (same value the API computes — see deriveRedirectUri).
 * `authoritative` is false for the derived value; the wizard says so.
 */
export async function getRedirectUri(platform: string): Promise<{ uri: string; authoritative: boolean } | null> {
  const started = await startOAuth(platform);
  if (started && started.redirect_uri) return { uri: started.redirect_uri, authoritative: true };
  const derived = deriveRedirectUri(platform);
  return derived ? { uri: derived, authoritative: false } : null;
}

/** One per-stage log line, exactly as GET /jobs/{id}/logs returns it. */
export interface JobLogEntry { ts: string | null; stage: string; level: string; message: string; }
export function normalizeJobLogEntry(raw: unknown): JobLogEntry {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    ts: typeof r.ts === "string" ? r.ts : null,
    stage: String(r.stage ?? ""),
    level: String(r.level ?? "info"),
    message: String(r.message ?? ""),
  };
}

/** GET /jobs/{id}/logs — per-stage run log for a job, oldest first. */
export async function getJobLogs(jobId: string): Promise<JobLogEntry[] | null> {
  const raw = await apiGet<unknown>(`/jobs/${encodeURIComponent(jobId)}/logs`);
  if (raw === null) return null;
  const logs = (raw as { logs?: unknown }).logs;
  if (!Array.isArray(logs)) return [];
  return logs.map(normalizeJobLogEntry);
}

/**
 * Turn a backend / provider error string into plain language plus the
 * next step. Unknown messages pass through unchanged (the backend
 * already writes plain sentences for most failures) — this only
 * translates the cryptic OAuth/provider codes a user cannot act on.
 */
export function explainConnectionError(message: string | undefined | null): { summary: string; nextStep: string } {
  const raw = (message ?? "").trim();
  const lower = raw.toLowerCase();
  if (!raw) return { summary: "The connection check failed, but the server gave no reason.", nextStep: "Try again. If it keeps failing, check the server is running and try the Connect wizard again." };
  if (lower.includes("llm") && (lower.includes("not saved") || lower.includes("no api key") || lower.includes("no llm"))) return { summary: "No AI key saved yet.", nextStep: "Paste your API key in the box above and press Save key — or skip it: ClipForge works without a key and writes titles from built-in templates instead." };
  if (lower.includes("client id is not saved") || lower.includes("not saved yet") || lower.includes("not been entered") || lower.includes("credentials have not been")) return { summary: "The app keys for this platform are not saved yet.", nextStep: "Open step 3 of the Connect wizard, paste the Client ID and Client Secret from the platform's developer app, save them, then press Connect." };
  if (lower.includes("not connected")) return { summary: "This channel is not connected yet.", nextStep: "Use the Connect button on the Channels page — sign in on the platform's own page and grant access." };
  if (lower.includes("expired")) return { summary: "The saved sign-in has expired.", nextStep: "Press Reconnect on the Channels page and sign in again." };
  if (lower.includes("redirect_uri_mismatch") || lower.includes("redirect uri")) return { summary: "The platform refused the redirect address (redirect URI mismatch).", nextStep: "Copy the redirect URI shown in the Connect wizard exactly — no extra spaces, no trailing slash — into the platform app's redirect URIs and save it there, then try again." };
  if (lower.includes("invalid_client") || lower.includes("unauthorized_client")) return { summary: "The platform did not recognise the client ID or secret.", nextStep: "Open the Connect wizard, re-copy the client ID and client secret from the platform's developer app, and save them again. Make sure no extra spaces were pasted." };
  if (lower.includes("access_denied") || lower.includes("user denied")) return { summary: "Access was declined on the platform's sign-in page.", nextStep: "Press Connect again and choose Allow / Grant access on the platform's page." };
  if (lower.includes("not configured") || lower.includes("not set up")) return { summary: "The platform app is not set up in ClipForge yet.", nextStep: "Open the Connect wizard for this platform — it walks through creating the developer app and pasting the client ID and secret." };
  if (lower.includes("api unreachable") || lower.includes("unreachable")) return { summary: "ClipForge could not reach its own server.", nextStep: "Check the server is running (the status bar at the top shows this), then try again." };
  return { summary: raw, nextStep: "Try the Connect wizard again — it checks each step and shows the exact error at the step that failed." };
}

/** POST /channels/{platform}/refresh — refresh / extend the stored token. */
export async function refreshChannel(platform: string): Promise<ApiResult> {
  return apiPostJson(`/channels/${encodeURIComponent(platform)}/refresh`);
}

/** POST /channels/{platform}/disconnect — delete the stored token. */
export async function disconnectChannel(platform: string): Promise<ApiResult> {
  return apiPostJson(`/channels/${encodeURIComponent(platform)}/disconnect`);
}

/** Masked credential view, exactly as GET /settings returns it. */
export interface MaskedCredential { configured: boolean; last4: string; masked: string; source: "stored" | "env" | "none"; }
export interface AppSettingsView {
  dry_run: boolean; dry_run_source: "settings" | "env";
  default_privacy: Record<string, string>; default_platforms: string[];
  llm: { provider: string; base_url: string; model: string; api_key: MaskedCredential };
  credentials: Record<string, { client_id: MaskedCredential; client_secret: MaskedCredential }>;
  data_dir: string; secret_backend: string;
  ffmpeg: { found: boolean; path: string; install_url: string };
  monetization_thresholds: { platform: string; program: string; threshold: Record<string, number>; note: string }[];
}

function normalizeMasked(raw: unknown): MaskedCredential {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    configured: r.configured === true,
    last4: typeof r.last4 === "string" ? r.last4 : "",
    masked: typeof r.masked === "string" ? r.masked : "",
    source: (r.source === "stored" || r.source === "env" ? r.source : "none") as MaskedCredential["source"],
  };
}

export function normalizeSettings(raw: unknown): AppSettingsView | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.dry_run !== "boolean") return null;
  const creds = (r.credentials ?? {}) as Record<string, Record<string, unknown>>;
  const llm = (r.llm ?? {}) as Record<string, unknown>;
  const ffmpeg = (r.ffmpeg ?? {}) as Record<string, unknown>;
  return {
    dry_run: r.dry_run,
    dry_run_source: r.dry_run_source === "settings" ? "settings" : "env",
    default_privacy: (r.default_privacy ?? {}) as Record<string, string>,
    default_platforms: Array.isArray(r.default_platforms) ? r.default_platforms.map(String) : [],
    llm: {
      provider: String(llm.provider ?? ""),
      base_url: String(llm.base_url ?? ""),
      model: String(llm.model ?? ""),
      api_key: normalizeMasked(llm.api_key),
    },
    credentials: Object.fromEntries(
      Object.entries(creds).map(([platform, c]) => [
        platform,
        { client_id: normalizeMasked(c.client_id), client_secret: normalizeMasked(c.client_secret) },
      ]),
    ),
    data_dir: String(r.data_dir ?? ""),
    secret_backend: String(r.secret_backend ?? ""),
    ffmpeg: {
      found: ffmpeg.found === true,
      path: String(ffmpeg.path ?? ""),
      install_url: String(ffmpeg.install_url ?? ""),
    },
    monetization_thresholds: Array.isArray(r.monetization_thresholds)
      ? (r.monetization_thresholds as Record<string, unknown>[]).map((t) => ({
          platform: String(t.platform ?? ""),
          program: String(t.program ?? ""),
          threshold: (t.threshold ?? {}) as Record<string, number>,
          note: String(t.note ?? ""),
        }))
      : [],
  };
}

/** GET /settings — masked credentials + preferences + environment status. */
export async function getSettings(): Promise<AppSettingsView | null> {
  const raw = await apiGet<unknown>("/settings");
  return raw === null ? null : normalizeSettings(raw);
}

/** PUT /settings — partial update; omitted = keep, "" on a secret = clear. */
export async function saveSettings(body: Record<string, unknown>): Promise<ApiResult> {
  try {
    const res = await fetch(`${API_URL}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(5000) });
    let data: unknown = null;
    try { data = await res.json(); } catch { /* non-JSON error body */ }
    const detail = (data as { detail?: unknown } | null)?.detail;
    return { ok: res.ok, status: res.status, data, error: res.ok ? undefined : (typeof detail === "string" ? detail : `Request failed (${res.status})`) };
  } catch {
    return { ok: false, status: 0, data: null, error: "API unreachable" };
  }
}

/** POST /settings/test-connection/{service} — live auth check, exact result. */
export async function testConnection(service: string): Promise<ApiResult> {
  return apiPostJson(`/settings/test-connection/${encodeURIComponent(service)}`);
}

/** Mask a secret for display (client-side helper): only the last 4 chars show. */
export function maskSecret(value: string | null | undefined): string {
  if (!value) return "";
  if (value.length <= 4) return "•".repeat(value.length);
  return "•".repeat(8) + value.slice(-4);
}

// ---------------------------------------------------------------------------
// Guided connect (v0.4.0): GET /connect/guide/{platform} — the API worker's
// endpoint. The guide (steps, console, redirect URI, review note) comes
// from the running server, so the wizard never hardcodes a localhost
// guess and never drifts from the backend. The local guide in
// lib/connect.ts is only a fallback for an older server that lacks the
// endpoint.
// ---------------------------------------------------------------------------

export interface ConnectGuideStep { step: number; title: string; detail: string; }
export interface ConnectGuide {
  platform: string; display_name: string;
  console_name: string; console_url: string;
  steps: ConnectGuideStep[];
  credentials: { key: string; label: string; saved: boolean }[];
  redirect_uri: string;
  scopes: string[];
  review_notice: string; fallback: string; daily_cap: number;
}

export function normalizeConnectGuide(raw: unknown): ConnectGuide | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.platform !== "string" || typeof r.redirect_uri !== "string") return null;
  const steps = Array.isArray(r.steps)
    ? (r.steps as Record<string, unknown>[]).map((s, i) => ({
        step: typeof s.step === "number" ? s.step : i + 1,
        title: String(s.title ?? ""),
        detail: String(s.detail ?? ""),
      }))
    : [];
  const creds = Array.isArray(r.credentials)
    ? (r.credentials as Record<string, unknown>[]).map((c) => ({
        key: String(c.key ?? ""),
        label: String(c.label ?? c.key ?? ""),
        saved: c.saved === true,
      }))
    : [];
  return {
    platform: r.platform,
    display_name: String(r.display_name ?? r.platform),
    console_name: String(r.console_name ?? ""),
    console_url: String(r.console_url ?? ""),
    steps,
    credentials: creds,
    redirect_uri: r.redirect_uri,
    scopes: Array.isArray(r.scopes) ? r.scopes.map(String) : [],
    review_notice: String(r.review_notice ?? ""),
    fallback: String(r.fallback ?? ""),
    daily_cap: typeof r.daily_cap === "number" ? r.daily_cap : 0,
  };
}

/** GET /connect/guide/{platform} — server-side guide + exact redirect URI. Null when the server lacks the endpoint. */
export async function getConnectGuide(platform: string): Promise<ConnectGuide | null> {
  const raw = await apiGet<unknown>(`/connect/guide/${encodeURIComponent(platform)}`);
  return raw === null ? null : normalizeConnectGuide(raw);
}

// ---------------------------------------------------------------------------
// First-run onboarding progress (v0.4.0): GET/PUT /onboarding — server
// prefs stored in settings.json. The web UI also keeps a localStorage
// flag (see components/Onboarding.tsx) so the wizard never nags even
// when the server is unreachable on a later launch.
// ---------------------------------------------------------------------------

export interface OnboardingState {
  ffmpeg_acknowledged: boolean;
  llm: "" | "skipped" | "set";
  first_channel_connected: boolean;
  sample_workflow_run: boolean;
  completed: boolean;
}

export function normalizeOnboarding(raw: unknown): OnboardingState {
  const r = (raw ?? {}) as Record<string, unknown>;
  const llm = r.llm === "skipped" || r.llm === "set" ? r.llm : "";
  return {
    ffmpeg_acknowledged: r.ffmpeg_acknowledged === true,
    llm,
    first_channel_connected: r.first_channel_connected === true,
    sample_workflow_run: r.sample_workflow_run === true,
    completed: r.completed === true,
  };
}

/** GET /onboarding — first-run progress (defaults when never started). */
export async function getOnboarding(): Promise<OnboardingState | null> {
  const raw = await apiGet<unknown>("/onboarding");
  return raw === null ? null : normalizeOnboarding(raw);
}

/** PUT /onboarding — partial progress update; omitted = keep. */
export async function updateOnboarding(patch: Partial<OnboardingState>): Promise<ApiResult> {
  return apiPutJson("/onboarding", patch);
}
