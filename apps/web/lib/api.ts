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
  | "failed"
  | "blocked";

export const JOB_STATUSES: readonly JobStatus[] = [
  "pending",
  "transcribing",
  "scoring",
  "rendering",
  "review",
  "ready",
  "failed",
  "blocked",
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
  /** Licence record of the job's source (v0.7.0): canonical key, original
   * title/channel, and the ready-made attribution credit (null for own
   * content — no credit needed — and for unknown licences). */
  source_license: VideoLicense;
  source_title: string; source_channel: string; attribution_text: string | null;
}
export interface SeoVariant { title: string; description: string; hashtags: string[]; }
export interface Candidate {
  id: string; job_id: string; n: number; score: number; hook_rating: number; hook_line: string;
  reason: string; transcript_excerpt: string; start_s: number; end_s: number;
  status: "pending" | "approved" | "rejected";
  /** Server-side rendered clip path (e.g. data/clips/<job>/clip_01.mp4), if rendered. */
  file_path: string | null;
  seo: Record<string, SeoVariant>;
  /** Generated thumbnail for the clip (v0.7.0, CC/live work): URL served by the API, or null until rendered. */
  thumbnail_url: string | null;
  /** Stored path of the selected thumbnail variant, when the API sends one. */
  thumbnail_path: string | null;
  /** Generated thumbnail variants, exactly as the API sends them
   * (`thumbnails: [{variant, filename, url}]`, variant 1 = default). */
  thumbnails: ThumbnailVariant[];
  /** Variant URLs (derived from `thumbnails`) for the picker UI. */
  thumbnail_variants: string[];
  /** Licence of the clip's source video (v0.7.0): "cc" | "standard" | "own" | "unknown". */
  source_license: VideoLicense;
  /** Source video the clip was cut from (URL + title + channel), for the attribution block. */
  source_url: string; source_title: string; source_channel: string;
  /** Ready-to-publish attribution credit for CC sources; null for own/standard sources. */
  attribution_text: string | null;
}

/** One generated thumbnail variant, exactly as the API sends it
 * (`thumbnails` on a candidate, GET /clips/{id}/thumbnail): variant 1
 * is the default thumbnail. */
export interface ThumbnailVariant { variant: number; filename: string; url: string; }

export function normalizeThumbnailVariants(raw: unknown): ThumbnailVariant[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((v) => {
    const q = (v ?? {}) as Record<string, unknown>;
    if (typeof v === "string") return { variant: 0, filename: "", url: v };
    return {
      variant: typeof q.variant === "number" ? q.variant : 0,
      filename: typeof q.filename === "string" ? q.filename : "",
      url: typeof q.url === "string" ? q.url : "",
    };
  }).filter((t) => t.url !== "");
}

/** Normalise one raw candidate payload (unknown wire shape) into a typed Candidate. */
export function normalizeCandidate(raw: unknown): Candidate {
  const r = (raw ?? {}) as Record<string, unknown>;
  const seoRaw = (r.seo ?? {}) as Record<string, unknown>;
  const seo: Record<string, SeoVariant> = {};
  for (const [k, v] of Object.entries(seoRaw)) {
    const s = (v ?? {}) as Record<string, unknown>;
    seo[k] = {
      title: String(s.title ?? ""),
      description: String(s.description ?? ""),
      hashtags: Array.isArray(s.hashtags) ? s.hashtags.map(String) : [],
    };
  }
  const variants = normalizeThumbnailVariants(r.thumbnails);
  if (variants.length === 0 && Array.isArray(r.thumbnail_variants)) {
    // Older / demo shape: plain URL strings.
    for (const u of r.thumbnail_variants) if (typeof u === "string" && u) variants.push({ variant: 0, filename: "", url: u });
  }
  return {
    id: String(r.id ?? ""),
    job_id: String(r.job_id ?? ""),
    n: typeof r.n === "number" ? r.n : 0,
    score: typeof r.score === "number" ? r.score : 0,
    hook_rating: typeof r.hook_rating === "number" ? r.hook_rating : 0,
    hook_line: String(r.hook_line ?? ""),
    reason: String(r.reason ?? ""),
    transcript_excerpt: String(r.transcript_excerpt ?? ""),
    start_s: typeof r.start_s === "number" ? r.start_s : 0,
    end_s: typeof r.end_s === "number" ? r.end_s : 0,
    status: r.status === "approved" || r.status === "rejected" ? r.status : "pending",
    file_path: typeof r.file_path === "string" ? r.file_path : null,
    seo,
    thumbnail_url: typeof r.thumbnail_url === "string" && r.thumbnail_url ? r.thumbnail_url : null,
    thumbnail_path: typeof r.thumbnail_path === "string" && r.thumbnail_path ? r.thumbnail_path : null,
    thumbnails: variants,
    thumbnail_variants: variants.map((t) => t.url),
    source_license: normalizeVideoLicense(r.source_license ?? r.license),
    source_url: typeof r.source_url === "string" ? r.source_url : "",
    source_title: typeof r.source_title === "string" ? r.source_title : "",
    source_channel: typeof r.source_channel === "string" ? r.source_channel : "",
    attribution_text: typeof r.attribution_text === "string" && r.attribution_text ? r.attribution_text : null,
  };
}

export function normalizeCandidates(raw: unknown): Candidate[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeCandidate);
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
    source_license: normalizeVideoLicense(r.source_license),
    source_title: typeof r.source_title === "string" ? r.source_title : "",
    source_channel: typeof r.source_channel === "string" ? r.source_channel : "",
    attribution_text: typeof r.attribution_text === "string" && r.attribution_text ? r.attribution_text : null,
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

// ---------------------------------------------------------------------------
// Worker queue (v0.5.0): GET /worker/queue — what the worker is doing,
// what is queued, what ran last. The dashboard's activity feed is built
// from this + jobs + posts; no new server endpoint was needed.
export interface QueuedJobSummary { id: string; title: string; source_type: string; status: string; created_at: string | null; }
export interface WorkerRunSummary extends WorkerRun { job_id: string; }
export interface WorkerQueueSummary {
  running: WorkerRunSummary[];
  queued: QueuedJobSummary[];
  next: { id: string; title: string } | null;
  recent_runs: WorkerRunSummary[];
}

export function normalizeWorkerRunSummary(raw: unknown): WorkerRunSummary {
  const r = (raw ?? {}) as Record<string, unknown>;
  return { ...normalizeWorkerRun(raw), job_id: String(r.job_id ?? "") };
}

export function normalizeWorkerQueue(raw: unknown): WorkerQueueSummary | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (!Array.isArray(r.running) && !Array.isArray(r.queued) && !Array.isArray(r.recent_runs)) return null;
  const queued = (Array.isArray(r.queued) ? r.queued : []).map((q) => {
    const j = (q ?? {}) as Record<string, unknown>;
    return {
      id: String(j.id ?? ""),
      title: String(j.title ?? ""),
      source_type: String(j.source_type ?? ""),
      status: String(j.status ?? ""),
      created_at: typeof j.created_at === "string" ? j.created_at : null,
    } satisfies QueuedJobSummary;
  });
  const next = (r.next ?? null) as Record<string, unknown> | null;
  return {
    running: (Array.isArray(r.running) ? r.running : []).map(normalizeWorkerRunSummary),
    queued,
    next: next ? { id: String(next.id ?? ""), title: String(next.title ?? "") } : null,
    recent_runs: (Array.isArray(r.recent_runs) ? r.recent_runs : []).map(normalizeWorkerRunSummary),
  };
}

/** GET /worker/queue — running + queued jobs and recent runs. */
export async function getWorkerQueue(): Promise<WorkerQueueSummary | null> {
  const raw = await apiGet<unknown>("/worker/queue");
  return raw === null ? null : normalizeWorkerQueue(raw);
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

/** One-click FFmpeg install state, exactly as GET /ffmpeg/status returns it. */
export type FfmpegInstallState =
  | "idle" | "downloading" | "verifying" | "extracting" | "installing"
  | "done" | "error" | "unsupported";
export interface FfmpegInstallStatus {
  state: FfmpegInstallState;
  progress_pct: number;
  downloaded_bytes: number;
  total_bytes: number | null;
  message: string;
  error: string;
  version: string;
  path: string;
  found: boolean;
  download_url: string;
  platform: string;
  path_added: boolean | null;
  checksum_verified: boolean | null;
  /** Step that failed ("" when nothing failed): download / verify / extract / install. */
  failed_step: string;
  /** Non-fatal Windows PATH warning on an otherwise successful install ("" when none). */
  path_warning: string;
}

const FFMPEG_INSTALL_STATES: FfmpegInstallState[] = [
  "idle", "downloading", "verifying", "extracting", "installing", "done", "error", "unsupported",
];

export function normalizeFfmpegInstallStatus(raw: unknown): FfmpegInstallStatus | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.state !== "string" || !(FFMPEG_INSTALL_STATES as string[]).includes(r.state)) return null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    state: r.state as FfmpegInstallState,
    progress_pct: num(r.progress_pct),
    downloaded_bytes: num(r.downloaded_bytes),
    total_bytes: typeof r.total_bytes === "number" ? r.total_bytes : null,
    message: typeof r.message === "string" ? r.message : "",
    error: typeof r.error === "string" ? r.error : "",
    version: typeof r.version === "string" ? r.version : "",
    path: typeof r.path === "string" ? r.path : "",
    found: r.found === true,
    download_url: typeof r.download_url === "string" ? r.download_url : "",
    platform: typeof r.platform === "string" ? r.platform : "",
    path_added: typeof r.path_added === "boolean" ? r.path_added : null,
    checksum_verified: typeof r.checksum_verified === "boolean" ? r.checksum_verified : null,
    failed_step: typeof r.failed_step === "string" ? r.failed_step : "",
    path_warning: typeof r.path_warning === "string" ? r.path_warning : "",
  };
}

/** GET /ffmpeg/status — pollable install progress (returns null when the API is unreachable). */
export async function getFfmpegInstallStatus(): Promise<FfmpegInstallStatus | null> {
  return normalizeFfmpegInstallStatus(await apiGet<unknown>("/ffmpeg/status"));
}

/** POST /ffmpeg/install — start the one-click install (returns immediately; poll the status). */
export async function installFfmpeg(force = false): Promise<ApiResult> {
  return apiPostJson(`/ffmpeg/install?force=${force ? "true" : "false"}`);
}

/** True while an install run is in progress (download / verify / extract / install). */
export function ffmpegInstallRunning(status: FfmpegInstallStatus | null): boolean {
  return status !== null && ["downloading", "verifying", "extracting", "installing"].includes(status.state);
}

/** One-click yt-dlp install state, exactly as GET /yt-dlp/status returns it. */
export type YtDlpInstallState =
  | "idle" | "downloading" | "verifying" | "installing"
  | "done" | "error" | "unsupported";
export interface YtDlpInstallStatus {
  state: YtDlpInstallState;
  progress_pct: number;
  downloaded_bytes: number;
  total_bytes: number | null;
  message: string;
  error: string;
  version: string;
  path: string;
  found: boolean;
  download_url: string;
  platform: string;
  path_added: boolean | null;
  checksum_verified: boolean | null;
  /** Step that failed ("" when nothing failed): download / verify / install. */
  failed_step: string;
  /** Non-fatal Windows PATH warning on an otherwise successful install ("" when none). */
  path_warning: string;
}

const YTDLP_INSTALL_STATES: YtDlpInstallState[] = [
  "idle", "downloading", "verifying", "installing", "done", "error", "unsupported",
];

export function normalizeYtDlpInstallStatus(raw: unknown): YtDlpInstallStatus | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.state !== "string" || !(YTDLP_INSTALL_STATES as string[]).includes(r.state)) return null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    state: r.state as YtDlpInstallState,
    progress_pct: num(r.progress_pct),
    downloaded_bytes: num(r.downloaded_bytes),
    total_bytes: typeof r.total_bytes === "number" ? r.total_bytes : null,
    message: typeof r.message === "string" ? r.message : "",
    error: typeof r.error === "string" ? r.error : "",
    version: typeof r.version === "string" ? r.version : "",
    path: typeof r.path === "string" ? r.path : "",
    found: r.found === true,
    download_url: typeof r.download_url === "string" ? r.download_url : "",
    platform: typeof r.platform === "string" ? r.platform : "",
    path_added: typeof r.path_added === "boolean" ? r.path_added : null,
    checksum_verified: typeof r.checksum_verified === "boolean" ? r.checksum_verified : null,
    failed_step: typeof r.failed_step === "string" ? r.failed_step : "",
    path_warning: typeof r.path_warning === "string" ? r.path_warning : "",
  };
}

/** GET /yt-dlp/status — pollable install progress (returns null when the API is unreachable). */
export async function getYtDlpInstallStatus(): Promise<YtDlpInstallStatus | null> {
  return normalizeYtDlpInstallStatus(await apiGet<unknown>("/yt-dlp/status"));
}

/** POST /yt-dlp/install — start the one-click install (returns immediately; poll the status). */
export async function installYtDlp(force = false): Promise<ApiResult> {
  return apiPostJson(`/yt-dlp/install?force=${force ? "true" : "false"}`);
}

/** True while an install run is in progress (download / verify / install). */
export function ytdlpInstallRunning(status: YtDlpInstallStatus | null): boolean {
  return status !== null && ["downloading", "verifying", "installing"].includes(status.state);
}

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
  ffmpeg: { found: boolean; path: string; version: string; install_url: string };
  yt_dlp: { found: boolean; path: string; version: string; install_url: string };
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
  const ytDlp = (r.yt_dlp ?? {}) as Record<string, unknown>;
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
      version: String(ffmpeg.version ?? ""),
      install_url: String(ffmpeg.install_url ?? ""),
    },
    yt_dlp: {
      found: ytDlp.found === true,
      path: String(ytDlp.path ?? ""),
      version: String(ytDlp.version ?? ""),
      install_url: String(ytDlp.install_url ?? ""),
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

// ---------------------------------------------------------------------------
// Agent + Gemini LLM (v0.5.0, worker 2 owns the backend engine; this file
// only speaks its contract — see the task handoff):
//   GET  /api/agent/status -> {enabled, running, current_job, accounts[]}
//   GET/PUT /api/agent/config -> {enabled, source_type, source_url, niche,
//                                  auto_post (default false), rights_confirmed}
//   POST /api/agent/start | /api/agent/stop -> {ok, state}
//   GET  /api/llm/providers -> [{id, label, configured, ...}] (masked)
//   POST /api/llm/gemini/test {api_key} -> {ok, model, message}
//   POST /api/llm/gemini/save {api_key}
//
// The engine may not have landed yet on this branch: every getter returns
// null when the endpoint is absent (apiGet returns null for any non-2xx),
// and every action returns {ok:false} with a plain error — the pages
// render an honest "not available in this build yet" state instead of
// crashing or faking an agent. Nothing here invents agent activity.
// ---------------------------------------------------------------------------

/** Niche presets offered by the Agent page + setup wizard (free text also allowed). */
export const NICHE_PRESETS = ["gaming", "tech", "vlogging", "education", "finance", "fitness", "cooking", "music"] as const;

export interface AgentAccountStatus {
  platform: string; account: string; status: string;
  progress_pct: number; queued: number; approved: number; published: number;
}

export interface AgentStatus {
  enabled: boolean; running: boolean;
  current_job: string | null;
  accounts: AgentAccountStatus[];
  // Run outcome — the Agent page needs these to show the TRUE state.
  // Before v0.8.1 normalizeAgentStatus dropped them, so the page could
  // only show Enabled/Idle while a run had silently errored: the
  // toggle looked broken because ON did nothing visible and gave no
  // reason. Fields default safely when an older server omits them.
  state: string;
  progress_pct: number;
  queued: number;
  approved: number;
  published: number;
  engine: string;
  demo: boolean;
  last_error: string | null;
  last_run_at: string | null;
  niche: string;
  auto_post: boolean;
  rights_confirmed: boolean;
}

export function normalizeAgentAccount(raw: unknown): AgentAccountStatus {
  const r = (raw ?? {}) as Record<string, unknown>;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    platform: String(r.platform ?? ""),
    account: String(r.account ?? ""),
    status: String(r.status ?? ""),
    progress_pct: num(r.progress_pct),
    queued: num(r.queued),
    approved: num(r.approved),
    published: num(r.published),
  };
}

export function normalizeAgentStatus(raw: unknown): AgentStatus | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.enabled !== "boolean" || typeof r.running !== "boolean") return null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    enabled: r.enabled,
    running: r.running,
    current_job: typeof r.current_job === "string" ? r.current_job : null,
    accounts: Array.isArray(r.accounts) ? r.accounts.map(normalizeAgentAccount) : [],
    state: typeof r.state === "string" ? r.state : r.running ? "running" : "idle",
    progress_pct: num(r.progress_pct),
    queued: num(r.queued),
    approved: num(r.approved),
    published: num(r.published),
    engine: typeof r.engine === "string" ? r.engine : "",
    demo: r.demo === true,
    last_error: typeof r.last_error === "string" ? r.last_error : null,
    last_run_at: typeof r.last_run_at === "string" ? r.last_run_at : null,
    niche: typeof r.niche === "string" ? r.niche : "",
    auto_post: r.auto_post === true,
    rights_confirmed: r.rights_confirmed === true,
  };
}

/** GET /api/agent/status — null when the agent engine has not landed yet. */
export async function getAgentStatus(): Promise<AgentStatus | null> {
  const raw = await apiGet<unknown>("/api/agent/status");
  return raw === null ? null : normalizeAgentStatus(raw);
}

export type AgentSourceType = "video" | "channel";

export interface AgentConfig {
  enabled: boolean;
  source_type: AgentSourceType;
  source_url: string;
  niche: string;
  auto_post: boolean;
  rights_confirmed: boolean;
}

export function normalizeAgentConfig(raw: unknown): AgentConfig | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.enabled !== "boolean") return null;
  return {
    enabled: r.enabled,
    source_type: r.source_type === "channel" ? "channel" : "video",
    source_url: typeof r.source_url === "string" ? r.source_url : "",
    niche: typeof r.niche === "string" ? r.niche : "",
    auto_post: r.auto_post === true,
    rights_confirmed: r.rights_confirmed === true,
  };
}

/** GET /api/agent/config — null when the agent engine has not landed yet. */
export async function getAgentConfig(): Promise<AgentConfig | null> {
  const raw = await apiGet<unknown>("/api/agent/config");
  return raw === null ? null : normalizeAgentConfig(raw);
}

/** PUT /api/agent/config — the agent cannot be enabled without rights_confirmed. */
export async function saveAgentConfig(config: Partial<AgentConfig>): Promise<ApiResult> {
  return apiPutJson("/api/agent/config", config);
}

/** POST /api/agent/start | /api/agent/stop. */
export async function startAgent(): Promise<ApiResult> {
  return apiPostJson("/api/agent/start");
}

export async function stopAgent(): Promise<ApiResult> {
  return apiPostJson("/api/agent/stop");
}

export interface LlmProvider {
  id: string; label: string; configured: boolean; model: string;
  /** True when this is the engine the agent would use right now. */
  active: boolean;
}

export function normalizeLlmProvider(raw: unknown): LlmProvider | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.id !== "string" || !r.id) return null;
  // The wire sends `name` ("Gemini", "OpenAI-compatible"); older shapes
  // sent `label`. Either works; the id is the last resort.
  const label = typeof r.label === "string" && r.label
    ? r.label
    : typeof r.name === "string" && r.name ? r.name : r.id;
  return {
    id: r.id,
    label,
    configured: r.configured === true,
    model: typeof r.model === "string" ? r.model : "",
    active: r.active === true,
  };
}

export function normalizeLlmProviders(raw: unknown): LlmProvider[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeLlmProvider).filter((p): p is LlmProvider => p !== null);
}

/** GET /api/llm/providers — null when the LLM service has not landed yet. */
export async function getLlmProviders(): Promise<LlmProvider[] | null> {
  const raw = await apiGet<unknown>("/api/llm/providers");
  return raw === null ? null : normalizeLlmProviders(raw);
}

/** POST /api/llm/gemini/test {api_key} — plain {ok, model, message} result. */
export async function testGeminiKey(apiKey: string): Promise<ApiResult> {
  return apiPostJson("/api/llm/gemini/test", { api_key: apiKey });
}

/** POST /api/llm/gemini/save {api_key} — the key is stored server-side only. */
export async function saveGeminiKey(apiKey: string): Promise<ApiResult> {
  return apiPostJson("/api/llm/gemini/save", { api_key: apiKey });
}

/** Which engine is actually writing titles / picking clips right now.
 * Prefers the backend's `active` flag; falls back to configured flags.
 * NOTE: the "rules" provider reports configured=true always (it needs
 * no key), so it must never count as an AI engine — only Gemini /
 * OpenAI configured counts, and with neither it is the rule engine. */
export function activeEngineName(providers: LlmProvider[] | null): string {
  if (providers === null) return "rule-based engine";
  const active = providers.find((p) => p.active);
  if (active) {
    if (active.id === "gemini") return "Gemini";
    if (active.id === "openai") return "OpenAI";
    return "rule-based engine";
  }
  const gemini = providers.find((p) => p.id === "gemini");
  if (gemini?.configured) return "Gemini";
  const openai = providers.find((p) => p.id === "openai");
  if (openai?.configured) return "OpenAI";
  return "rule-based engine";
}

// ---------------------------------------------------------------------------
// Setup-wizard state (v0.5.0): GET/POST /api/setup/state — persisted
// server-side in <data_dir>/setup.json (apps/api/clipforge_api/setup.py).
// Falls back to null on an older server without the endpoint; the wizard
// then keeps its progress in memory + the /onboarding prefs only.
// ---------------------------------------------------------------------------

export const SETUP_STEP_KEYS = ["ffmpeg", "gemini", "channel", "niche_source", "agent"] as const;
export type SetupStepKey = (typeof SETUP_STEP_KEYS)[number];

export const SETUP_STEP_LABELS: Record<SetupStepKey, string> = {
  ffmpeg: "FFmpeg",
  gemini: "Gemini key",
  channel: "First channel",
  niche_source: "Niche & source",
  agent: "Turn on the agent",
};

export interface SetupState {
  current_step: number;
  steps: Record<SetupStepKey, boolean>;
  niche: string;
  source_type: AgentSourceType;
  source_url: string;
  completed: boolean;
}

export function normalizeSetupState(raw: unknown): SetupState {
  const r = (raw ?? {}) as Record<string, unknown>;
  const stepsRaw = (r.steps ?? {}) as Record<string, unknown>;
  const steps = {} as Record<SetupStepKey, boolean>;
  for (const key of SETUP_STEP_KEYS) steps[key] = stepsRaw[key] === true;
  const current = typeof r.current_step === "number" && Number.isFinite(r.current_step) ? Math.trunc(r.current_step) : 0;
  return {
    current_step: Math.max(0, Math.min(SETUP_STEP_KEYS.length - 1, current)),
    steps,
    niche: typeof r.niche === "string" ? r.niche : "",
    source_type: r.source_type === "channel" ? "channel" : "video",
    source_url: typeof r.source_url === "string" ? r.source_url : "",
    completed: r.completed === true,
  };
}

/** Partial setup-state update: any subset of fields, and any subset of steps. */
export type SetupStatePatch = Omit<Partial<SetupState>, "steps"> & {
  steps?: Partial<Record<SetupStepKey, boolean>>;
};
export async function getSetupState(): Promise<SetupState | null> {
  const raw = await apiGet<unknown>("/api/setup/state");
  return raw === null ? null : normalizeSetupState(raw);
}

/** POST /api/setup/state — partial update; omitted = keep. */
export async function saveSetupState(patch: SetupStatePatch): Promise<ApiResult> {
  return apiPostJson("/api/setup/state", patch);
}

// ---------------------------------------------------------------------------
// Video licences + the CC clipping guardrail (v0.7.0).
//
// The API reports a licence per video (`license` on trending/live results,
// `source_license` on clip records) and a plain `can_clip` verdict with a
// `reason`. Values seen on the wire: YouTube's "creativeCommons" / "youtube"
// (see the YouTube Data API `status.license`), plus the API's own "own"
// for the user's own videos. Everything normalises to VideoLicense below
// so the UI never has to compare raw strings.
//
// Guardrail (unchanged from v0.6.0, now explicit): a clip may be made from
// the user's OWN video or a Creative Commons licensed video — never from a
// standard-licence video by another creator. When the API does not report
// a licence at all, the video is treated as NOT clippable (fail closed).
// ---------------------------------------------------------------------------

/** Normalised licence of a video. "own" = the user's own video.
 * Wire values seen (v0.7.0, Workers A/B): YouTube's raw `status.license`
 * ("creativeCommon" / "youtube" / "" = not checked) on trending, live and
 * licence-check results, and the canonical keys ("cc-by-4.0", "cc-by-3.0",
 * "cc-by-sa-4.0", "cc0", "public-domain", "own", "standard") on clip/job
 * records (clipforge.licensing). */
export type VideoLicense = "cc" | "standard" | "own" | "public-domain" | "unknown";

export function normalizeVideoLicense(raw: unknown): VideoLicense {
  if (typeof raw !== "string") return "unknown";
  const v = raw.trim().toLowerCase();
  if (!v) return "unknown";
  if (v === "cc" || v === "cc0" || v.startsWith("cc-") || v.startsWith("cc ") || v.startsWith("creativecommon") || v.startsWith("creative common")) return "cc";
  if (v === "public-domain" || v === "public domain") return "public-domain";
  if (v === "own" || v === "mine" || v === "owned" || v === "own content") return "own";
  if (v === "standard" || v === "youtube" || v === "std" || v === "all rights reserved" || v === "standard youtube license" || v === "youtube standard license") return "standard";
  return "unknown";
}

/** Short badge label for a licence, for the Trending / Live / review UI. */
export function licenseBadgeLabel(license: VideoLicense): string {
  if (license === "cc") return "Creative Commons";
  if (license === "own") return "Your video";
  if (license === "public-domain") return "Public domain";
  if (license === "standard") return "Standard licence";
  return "Licence unknown";
}

/** Can this video be clipped? The API's `can_clip` wins when present;
// otherwise derive it from the licence (own + CC + public-domain = yes,
// anything else = no). */
export function deriveCanClip(license: VideoLicense, canClip: boolean | null): { canClip: boolean; reason: string } {
  if (canClip === true) return { canClip: true, reason: license === "own" ? "Your own video." : license === "cc" ? "Creative Commons licence — free to reuse with attribution." : license === "public-domain" ? "Public domain — free to reuse." : "" };
  if (canClip === false) {
    return {
      canClip: false,
      reason: license === "standard"
        ? "Standard YouTube licence — this creator did not allow reuse, so it cannot be clipped."
        : license === "unknown"
          ? "No licence information — treated as not clippable until the licence is confirmed."
          : "Not clippable.",
    };
  }
  if (license === "cc") return { canClip: true, reason: "Creative Commons licence — free to reuse with attribution." };
  if (license === "own") return { canClip: true, reason: "Your own video." };
  if (license === "public-domain") return { canClip: true, reason: "Public domain — free to reuse." };
  if (license === "standard") return { canClip: false, reason: "Standard YouTube licence — this creator did not allow reuse, so it cannot be clipped." };
  return { canClip: false, reason: "No licence information — treated as not clippable until the licence is confirmed." };
}

/** Attribution credit for a CC / public-domain source, read-only and
 * always shown in the description preview for such clips. Falls back to
 * a generated credit from the source fields when the API did not send
 * ready-made text (the server builds the same block verbatim). */
export function attributionForClip(c: Pick<Candidate, "source_license" | "source_title" | "source_channel" | "source_url" | "attribution_text">): string | null {
  if (c.source_license !== "cc" && c.source_license !== "public-domain") return null;
  if (c.attribution_text) return c.attribution_text;
  const title = c.source_title || "Untitled video";
  const channel = c.source_channel || "Unknown creator";
  const url = c.source_url ? ` — ${c.source_url}` : "";
  if (c.source_license === "public-domain") {
    return `Source: "${title}" by ${channel}${url} — public domain. Please keep this credit in the description.`;
  }
  return `Source: "${title}" by ${channel}${url} — licensed under Creative Commons (CC BY). Please keep this credit in the description.`;
}

/** The `source_license` value to send when creating a clip job from a
 * video (POST /jobs). The server only accepts canonical keys: YouTube's
 * raw "creativeCommon" licence is CC BY, so it maps to "cc-by-3.0"
 * (the key the server's own tests use for YouTube CC videos); a
 * canonical CC key passes through unchanged. */
export function jobSourceLicense(video: Pick<TrendingVideo, "license" | "license_key">): string {
  if (video.license === "own") return "own";
  if (video.license === "public-domain") return "public-domain";
  if (video.license === "cc") {
    const key = (video.license_key || "").trim().toLowerCase();
    if (key.startsWith("cc-") || key === "cc0") return key;
    return "cc-by-3.0";
  }
  return "standard";
}

/** POST /jobs — create a clip job from a YouTube video (v0.7.0 CC
 * pipeline). This is the correct path for clipping a CC / own video:
 * POST /api/trending/run refuses the trending video itself as a source.
 * The server gates on the licence: standard-licence sources are refused
 * (422), CC / public-domain / own sources are accepted. Returns the
 * ApiResult so the caller can read the new job id (`data.id`) or show
 * the server's plain-language 422. */
export async function createYoutubeClipJob(input: {
  source_url: string; title: string; source_license: string;
  source_title?: string; source_channel?: string; own_content_declaration?: boolean;
}): Promise<ApiResult> {
  return apiPostJson("/jobs", {
    source_type: "youtube",
    source_url: input.source_url,
    title: input.title,
    source_license: input.source_license,
    source_title: input.source_title ?? "",
    source_channel: input.source_channel ?? "",
    own_content_declaration: input.own_content_declaration ?? false,
  });
}

/** GET /clips/{candidateId}/thumbnail — generated thumbnails for a clip. */
export async function getClipThumbnails(candidateId: string): Promise<{ thumbnail_url: string | null; thumbnails: ThumbnailVariant[] } | null> {
  const raw = await apiGet<unknown>(`/clips/${encodeURIComponent(candidateId)}/thumbnail`);
  if (raw === null) return null;
  const r = raw as Record<string, unknown>;
  return {
    thumbnail_url: typeof r.thumbnail_url === "string" ? r.thumbnail_url : null,
    thumbnails: normalizeThumbnailVariants(r.thumbnails),
  };
}

/** POST /clips/{candidateId}/thumbnail — ask the API to regenerate a
 * clip's thumbnail variant(s) from the rendered clip file. `variant`
 * regenerates one variant (1 or 2); omitted, both are regenerated.
 * Returns the ApiResult (with the updated variant list) so callers can
 * surface the honest states: 404 (no such clip), 409 (no rendered
 * video yet — run the job first). */
export async function regenerateThumbnail(candidateId: string, variant?: number): Promise<ApiResult> {
  return apiPostJson(`/clips/${encodeURIComponent(candidateId)}/thumbnail`, variant ? { variant } : {});
}

/** POST /clips/{candidateId}/thumbnail/select — pick which generated
 * variant is the clip's thumbnail. */
export async function selectThumbnailVariant(candidateId: string, variant: number): Promise<ApiResult> {
  return apiPostJson(`/clips/${encodeURIComponent(candidateId)}/thumbnail/select`, { variant });
}

// ---------------------------------------------------------------------------
// Live niche trending search + trending-to-agent loop (v0.6.0, Worker B).
// Contract: GET /api/trending/niches | /credentials | /search,
// POST /api/trending/api-key | /match | /run, GET /api/trending/status
// and /api/trending/jobs/{id} (apps/api/clipforge_api/trending.py).
//
// Rights guardrail: trending shows what topics are working — clips are
// made from YOUR videos. A trending video URL is never a clip source;
// the backend refuses it with a plain 422, and the page states it.
// ---------------------------------------------------------------------------

export interface TrendingVideo {
  video_id: string;
  title: string;
  channel_title: string;
  channel_id: string;
  published_at: string;
  view_count: number;
  like_count: number;
  comment_count: number;
  trend_score: number;
  url: string;
  /** Licence (v0.7.0): "cc" | "standard" | "own" | "public-domain" | "unknown". */
  license: VideoLicense;
  /** Raw licence value from the API ("creativeCommon", "cc-by-4.0", ...),
   * kept so job creation can send the server a canonical key. */
  license_key: string;
  /** False when the server never checked this video's licence: trending
   * search only verifies licences for `licensed_only` runs ("" on the
   * wire). Unchecked videos show "check before clipping", not a verdict. */
  license_checked: boolean;
  /** Licence details page, when the API sends one. */
  license_url: string | null;
  /** Whether this video may be clipped (API verdict, or derived from the licence). */
  can_clip: boolean;
  /** Plain-language reason for the can_clip verdict. */
  reason: string;
  /** Live videos only (v0.7.0): current concurrent viewers. */
  concurrent_viewers: number | null;
  /** Live videos only: when the stream started (ISO). */
  started_at: string | null;
  /** "live" for live streams, "video" otherwise. */
  event_type: string;
}

export function normalizeTrendingVideo(raw: unknown): TrendingVideo | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.video_id !== "string" || !r.video_id) return null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  // Licence: `license` on trending / licence-check results, `licence`
  // on live results (live.py spells it with a 'c'). Own videos are
  // flagged by `is_own` on live results / the licence check.
  const rawLicense = typeof r.license === "string" ? r.license : (typeof r.licence === "string" ? r.licence : "");
  const isOwn = r.is_own === true;
  const license = isOwn ? "own" : normalizeVideoLicense(rawLicense);
  const licenseChecked = rawLicense.trim() !== "" || typeof r.can_clip === "boolean";
  const verdict = deriveCanClip(license, typeof r.can_clip === "boolean" ? r.can_clip : null);
  const reasonRaw = typeof r.reason === "string" && r.reason ? r.reason : (typeof r.clip_reason === "string" ? r.clip_reason : "");
  return {
    video_id: r.video_id,
    title: typeof r.title === "string" ? r.title : "",
    channel_title: typeof r.channel_title === "string" ? r.channel_title : "",
    channel_id: typeof r.channel_id === "string" ? r.channel_id : "",
    published_at: typeof r.published_at === "string" ? r.published_at : "",
    view_count: num(r.view_count),
    like_count: num(r.like_count),
    comment_count: num(r.comment_count),
    trend_score: num(r.trend_score),
    url: typeof r.url === "string" && r.url ? r.url : `https://www.youtube.com/watch?v=${r.video_id}`,
    license,
    license_key: rawLicense,
    license_checked: licenseChecked,
    license_url: typeof r.license_url === "string" && r.license_url ? r.license_url : null,
    can_clip: verdict.canClip,
    reason: reasonRaw || verdict.reason,
    concurrent_viewers: typeof r.concurrent_viewers === "number" ? r.concurrent_viewers : (typeof r.viewers === "number" ? r.viewers : null),
    started_at: typeof r.started_at === "string" ? r.started_at : null,
    event_type: typeof r.event_type === "string" ? r.event_type : ("viewers" in r || "licence" in r ? "live" : "video"),
  };
}

export function normalizeTrendingVideos(raw: unknown): TrendingVideo[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeTrendingVideo).filter((v): v is TrendingVideo => v !== null);
}

export interface TrendingCredentials {
  oauth_connected: boolean;
  oauth_expired: boolean;
  oauth_account: string;
  api_key_configured: boolean;
  api_key_last4: string;
  api_key_source: string;
  usable: boolean;
  message: string;
}

export function normalizeTrendingCredentials(raw: unknown): TrendingCredentials | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  const oauth = (r.oauth ?? {}) as Record<string, unknown>;
  const key = (r.api_key ?? {}) as Record<string, unknown>;
  if (typeof r.usable !== "boolean") return null;
  return {
    oauth_connected: oauth.connected === true,
    oauth_expired: oauth.expired === true,
    oauth_account: typeof oauth.account_name === "string" ? oauth.account_name : "",
    api_key_configured: key.configured === true,
    api_key_last4: typeof key.last4 === "string" ? key.last4 : "",
    api_key_source: typeof key.source === "string" ? key.source : "none",
    usable: r.usable,
    message: typeof r.message === "string" ? r.message : "",
  };
}

/** GET /api/trending/credentials — null when the endpoint has not landed yet. */
export async function getTrendingCredentials(): Promise<TrendingCredentials | null> {
  const raw = await apiGet<unknown>("/api/trending/credentials");
  return raw === null ? null : normalizeTrendingCredentials(raw);
}

/** POST /api/trending/api-key — the key is stored server-side only, never echoed. */
export async function saveYoutubeApiKey(apiKey: string): Promise<ApiResult> {
  return apiPostJson("/api/trending/api-key", { api_key: apiKey });
}

export interface TrendingSearchResult {
  results: TrendingVideo[];
  source: string;
  account: string;
  notice: string;
}

/** GET with the error detail preserved (apiGet drops it): the Trending
 * page needs the 422 "connect your channel" message for its empty state. */
export async function apiGetResult(path: string): Promise<ApiResult> {
  try {
    const res = await fetch(`${API_URL}${path}`, { cache: "no-store", signal: AbortSignal.timeout(10000) });
    let data: unknown = null;
    try { data = await res.json(); } catch { /* non-JSON error body */ }
    const detail = (data as { detail?: unknown } | null)?.detail;
    return { ok: res.ok, status: res.status, data, error: res.ok ? undefined : (typeof detail === "string" ? detail : `Request failed (${res.status})`) };
  } catch {
    return { ok: false, status: 0, data: null, error: "API unreachable" };
  }
}

/** GET /api/trending/search — live YouTube results, or the plain-language
 * 422 (no connection / no key) as `error` so the page can show it.
 * v0.7.0: `licensedOnly` adds `licensed_only=true` (CC / licensed videos
 * only, licences verified per video). Without it the server does NOT
 * verify licences (wire `license` is "" = not checked) — the page then
 * offers a per-video licence check instead of guessing a verdict. */
export async function searchTrending(niche: string, maxResults = 10, opts: { licensedOnly?: boolean } = {}): Promise<{ result: TrendingSearchResult | null; error: string | null }> {
  const q = new URLSearchParams({ niche, max_results: String(maxResults) });
  if (opts.licensedOnly) q.set("licensed_only", "true");
  const res = await apiGetResult(`/api/trending/search?${q.toString()}`);
  if (!res.ok) return { result: null, error: res.error ?? "Trending search failed." };
  const r = (res.data ?? {}) as Record<string, unknown>;
  return {
    result: {
      results: normalizeTrendingVideos(r.results),
      source: typeof r.source === "string" ? r.source : "",
      account: typeof r.account === "string" ? r.account : "",
      notice: typeof r.notice === "string" ? r.notice : "",
    },
    error: null,
  };
}

/** GET /api/trending/live — live streams in a niche, right now (v0.7.0,
 * separate endpoint, NOT event_type on search). Each result carries
 * `viewers` (concurrent), `started_at`, `licence` and `can_clip` —
 * true only for the user's own live streams and CC licensed ones. */
export async function searchLive(niche: string, maxResults = 10, opts: { licensedOnly?: boolean } = {}): Promise<{ result: TrendingSearchResult | null; error: string | null }> {
  const q = new URLSearchParams({ niche, max_results: String(maxResults) });
  if (opts.licensedOnly) q.set("licensed_only", "true");
  const res = await apiGetResult(`/api/trending/live?${q.toString()}`);
  if (!res.ok) return { result: null, error: res.error ?? "Live search failed." };
  const r = (res.data ?? {}) as Record<string, unknown>;
  return {
    result: {
      results: normalizeTrendingVideos(r.results),
      source: typeof r.source === "string" ? r.source : "",
      account: typeof r.account === "string" ? r.account : "",
      notice: typeof r.notice === "string" ? r.notice : "",
    },
    error: null,
  };
}

/** Licence check for one pasted YouTube URL (v0.7.0).
 * Contract (apps/api/clipforge_api/trending.py, videos_router):
 * GET /api/videos/license?url=... -> {video_id, title, channel,
 * channel_id, license, license_url, can_clip, reason}. `license` is the
 * raw YouTube value ("creativeCommon" / "youtube" / ""); `can_clip` is
 * true only for a CC video or the user's own video. Null when the
 * endpoint has not landed yet (the page says so instead of inventing a
 * verdict). */
export interface LicenseCheck {
  video_id: string; title: string; channel: string;
  license: VideoLicense; license_key: string; license_url: string | null;
  can_clip: boolean; reason: string; url: string;
}

export function normalizeLicenseCheck(raw: unknown, fallbackUrl = ""): LicenseCheck | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.video_id !== "string" || !r.video_id) return null;
  const rawLicense = typeof r.license === "string" ? r.license : (typeof r.licence === "string" ? r.licence : "");
  // The licence check matches the video against the connected channel:
  // a standard-licence video that IS the user's own still returns
  // can_clip=true, so own-ness comes from the verdict + reason, not the
  // licence value alone.
  const isOwn = r.is_own === true || (typeof r.can_clip === "boolean" && r.can_clip === true && normalizeVideoLicense(rawLicense) === "standard" && typeof r.reason === "string" && /own video/i.test(r.reason));
  const license = isOwn ? "own" : normalizeVideoLicense(rawLicense);
  const verdict = deriveCanClip(license, typeof r.can_clip === "boolean" ? r.can_clip : null);
  return {
    video_id: r.video_id,
    title: typeof r.title === "string" ? r.title : "",
    channel: typeof r.channel === "string" ? r.channel : (typeof r.channel_title === "string" ? r.channel_title : ""),
    license,
    license_key: rawLicense,
    license_url: typeof r.license_url === "string" && r.license_url ? r.license_url : null,
    can_clip: verdict.canClip,
    reason: typeof r.reason === "string" && r.reason ? r.reason : verdict.reason,
    url: typeof r.url === "string" && r.url ? r.url : fallbackUrl,
  };
}

export async function checkVideoLicense(url: string): Promise<{ check: LicenseCheck | null; error: string | null }> {
  const res = await apiGetResult(`/api/videos/license?url=${encodeURIComponent(url)}`);
  if (!res.ok) return { check: null, error: res.error ?? "Licence check failed." };
  const check = normalizeLicenseCheck(res.data, url);
  if (!check) return { check: null, error: "The licence check returned a video the app could not read." };
  return { check, error: null };
}

/** Live capture (v0.7.0): start capturing a live stream so it can be
 * clipped. Contract (apps/api/clipforge_api/trending.py):
 * POST /api/trending/live/capture {video_id, duration_s} (202) ->
 * {job_id, video_id, title, licence, is_own, duration_s, message,
 * notice}. The licence is re-verified server-side; only the user's own
 * live stream or a CC licensed one is captured. Returns the ApiResult
 * so the page can show the job id + live progress, or the honest
 * "not available in this build yet" state when the endpoint has not
 * landed (404). `duration_s` is bounded 60–300s server-side. */
export async function startLiveCapture(videoId: string, durationS: number): Promise<ApiResult> {
  return apiPostJson("/api/trending/live/capture", { video_id: videoId, duration_s: durationS });
}

export interface TrendingMatch {
  own_video_url: string;
  own_title: string;
  match_score: number;
}

/** POST /api/trending/match — the user's OWN channel video that best
 * matches a trending topic (title overlap). */
export async function matchTrendingTopic(channelUrl: string, trendingVideoId: string, trendingTitle: string): Promise<{ match: TrendingMatch | null; error: string | null }> {
  const res = await apiPostJson("/api/trending/match", { channel_url: channelUrl, trending_video_id: trendingVideoId, trending_title: trendingTitle });
  if (!res.ok) return { match: null, error: res.error ?? "Could not match the channel." };
  const r = (res.data ?? {}) as Record<string, unknown>;
  if (typeof r.own_video_url !== "string" || !r.own_video_url) return { match: null, error: "The channel returned no matching video." };
  return {
    match: {
      own_video_url: r.own_video_url,
      own_title: typeof r.own_title === "string" ? r.own_title : "",
      match_score: typeof r.match_score === "number" ? r.match_score : 0,
    },
    error: null,
  };
}

export interface TrendingRunInput {
  niche: string;
  trending_video_id: string;
  trending_title: string;
  own_source_url: string;
  own_source_type: "video" | "channel";
  rights_confirmed: boolean;
  top_n?: number;
  platforms?: string[];
  auto_post?: boolean;
  scheduled_for?: string | null;
}

/** POST /api/trending/run — starts the loop (202). The run is async:
 * poll getTrendingStatus() / the pipeline for the job. */
export async function startTrendingRun(input: TrendingRunInput): Promise<ApiResult> {
  return apiPostJson("/api/trending/run", input);
}

/** GET /api/trending/status — agent run status incl. the trending video
 * of the current / last run (topic only). Null when the endpoint is absent. */
export async function getTrendingStatus(): Promise<(AgentStatus & { trending_video_id: string; trending_title: string; state: string; progress_pct: number; queued: number; last_error: string | null }) | null> {
  const raw = await apiGet<unknown>("/api/trending/status");
  if (raw === null) return null;
  const status = normalizeAgentStatus(raw);
  if (!status) return null;
  const r = raw as Record<string, unknown>;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    ...status,
    trending_video_id: typeof r.trending_video_id === "string" ? r.trending_video_id : "",
    trending_title: typeof r.trending_title === "string" ? r.trending_title : "",
    state: typeof r.state === "string" ? r.state : "",
    progress_pct: num(r.progress_pct),
    queued: num(r.queued),
    last_error: typeof r.last_error === "string" ? r.last_error : null,
  };
}

export interface TrendingJobPost {
  id: string;
  platform: string;
  status: string;
  title: string;
  scheduled_for: string | null;
  provider_post_id: string | null;
  video_id: string | null;
}

export interface TrendingJob {
  id: string;
  title: string;
  source: string;
  status: string;
  trending_video_id: string;
  trending_title: string;
  trending_url: string;
  demo: boolean;
  posts: TrendingJobPost[];
}

export function normalizeTrendingJob(raw: unknown): TrendingJob | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.id !== "string" || !r.id) return null;
  const posts = Array.isArray(r.posts) ? r.posts : [];
  return {
    id: r.id,
    title: typeof r.title === "string" ? r.title : "",
    source: typeof r.source === "string" ? r.source : "",
    status: typeof r.status === "string" ? r.status : "",
    trending_video_id: typeof r.trending_video_id === "string" ? r.trending_video_id : "",
    trending_title: typeof r.trending_title === "string" ? r.trending_title : "",
    trending_url: typeof r.trending_url === "string" ? r.trending_url : "",
    demo: r.demo === true,
    posts: posts.map((p) => {
      const q = (p ?? {}) as Record<string, unknown>;
      return {
        id: typeof q.id === "string" ? q.id : "",
        platform: typeof q.platform === "string" ? q.platform : "",
        status: typeof q.status === "string" ? q.status : "",
        title: typeof q.title === "string" ? q.title : "",
        scheduled_for: typeof q.scheduled_for === "string" ? q.scheduled_for : null,
        provider_post_id: typeof q.provider_post_id === "string" ? q.provider_post_id : null,
        video_id: typeof q.video_id === "string" ? q.video_id : null,
      };
    }),
  };
}

/** GET /api/trending/jobs/{id} — the job record with each post's platform
 * + provider video id, for monitoring the posted clip. */
export async function getTrendingJob(jobId: string): Promise<TrendingJob | null> {
  const raw = await apiGet<unknown>(`/api/trending/jobs/${encodeURIComponent(jobId)}`);
  return raw === null ? null : normalizeTrendingJob(raw);
}

// ---------------------------------------------------------------------------
// ClipForge Pro (v0.8.0) — new backend endpoints. Every getter returns
// null / a plain error when the endpoint has not landed yet; pages render
// an honest "not available in this build yet" state instead of fake data.
// ---------------------------------------------------------------------------

/** One AI clip suggestion from POST /detection/analyze. */
export interface DetectionCandidate {
  start: number; end: number; score: number;
  hook_score: number; peak_score: number; payoff_score: number;
  emotion_score: number; visual_score: number; completeness_score: number;
  title: string; reason: string;
}

export function normalizeDetectionCandidate(raw: unknown): DetectionCandidate {
  const r = (raw ?? {}) as Record<string, unknown>;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    start: num(r.start), end: num(r.end), score: num(r.score),
    hook_score: num(r.hook_score), peak_score: num(r.peak_score),
    payoff_score: num(r.payoff_score), emotion_score: num(r.emotion_score),
    visual_score: num(r.visual_score), completeness_score: num(r.completeness_score),
    title: typeof r.title === "string" ? r.title : "",
    reason: typeof r.reason === "string" ? r.reason : "",
  };
}

/** POST /detection/analyze — AI clip suggestions for a source video. */
export async function analyzeDetection(
  segments: { start: number; end: number; text?: string }[],
  duration: number,
  niche?: string,
): Promise<{ candidates: DetectionCandidate[]; error: string | null }> {
  const res = await apiPostJson("/detection/analyze", { segments, duration, ...(niche ? { niche } : {}) });
  if (!res.ok) return { candidates: [], error: res.error ?? "Clip analysis failed." };
  const c = (res.data as { candidates?: unknown } | null)?.candidates;
  return { candidates: Array.isArray(c) ? c.map(normalizeDetectionCandidate) : [], error: null };
}

/** Performance profile, exactly as GET /perf/profile returns it. */
export type PerfProfileName = "auto" | "low" | "balanced" | "high";
export interface PerfProfile {
  profile: string; reason: string;
  cpu_cores: number | null; ram_gb: number | null; gpu: string | null;
  override: boolean;
}

export function normalizePerfProfile(raw: unknown): PerfProfile | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.profile !== "string") return null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  // API shape: {profile, mode, hardware: {cpu_cores, ram_gb, gpu, platform}, settings}
  const hw = (r.hardware ?? {}) as Record<string, unknown>;
  const mode = typeof r.mode === "string" ? r.mode : "auto";
  return {
    profile: r.profile,
    reason: typeof r.reason === "string" ? r.reason : "",
    cpu_cores: num(hw.cpu_cores ?? r.cpu_cores),
    ram_gb: num(hw.ram_gb ?? r.ram_gb),
    gpu: typeof hw.gpu === "string" ? hw.gpu : typeof r.gpu === "string" ? r.gpu : null,
    override: mode !== "auto",
  };
}

/** GET /perf/profile — null when the endpoint has not landed yet. */
export async function getPerfProfile(): Promise<PerfProfile | null> {
  const raw = await apiGet<unknown>("/perf/profile");
  return raw === null ? null : normalizePerfProfile(raw);
}

/** POST /perf/profile — set the override (auto = let the app decide). */
export async function setPerfProfile(profile: PerfProfileName): Promise<ApiResult> {
  return apiPostJson("/perf/profile", { profile });
}

/** One item in a batch run. */
export interface BatchItem {
  source: string; state: string; progress: number; clips: number;
}
export interface BatchStatus {
  batch_id: string; status: string; items: BatchItem[];
}

export function normalizeBatchStatus(raw: unknown): BatchStatus | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.batch_id !== "string" && typeof r.id !== "string") return null;
  const items = Array.isArray(r.items) ? r.items : [];
  return {
    batch_id: typeof r.batch_id === "string" ? r.batch_id : String(r.id ?? ""),
    status: typeof r.status === "string" ? r.status : "",
    items: items.map((it) => {
      const q = (it ?? {}) as Record<string, unknown>;
      const res = (q.result ?? {}) as Record<string, unknown>;
      const clips =
        typeof q.clips === "number"
          ? q.clips
          : typeof res.clips === "number"
            ? res.clips
            : 0;
      return {
        source: typeof q.source === "string" ? q.source : "",
        state: typeof q.state === "string" ? q.state : "queued",
        progress: typeof q.progress === "number" ? q.progress : 0,
        clips,
      } satisfies BatchItem;
    }),
  };
}

/** POST /batch — start a batch over up to 20 sources. Returns the batch id or an error. */
export async function startBatch(sources: { url?: string; path?: string }[]): Promise<{ batch_id: string | null; error: string | null }> {
  const res = await apiPostJson("/batch", { sources });
  if (!res.ok) return { batch_id: null, error: res.error ?? "Could not start the batch." };
  const id = (res.data as { batch_id?: unknown } | null)?.batch_id;
  return { batch_id: typeof id === "string" ? id : null, error: typeof id === "string" ? null : "The server did not return a batch id." };
}

/** GET /batch/{id} — pollable batch progress. */
export async function getBatch(id: string): Promise<BatchStatus | null> {
  const raw = await apiGet<unknown>(`/batch/${encodeURIComponent(id)}`);
  return raw === null ? null : normalizeBatchStatus(raw);
}

/** POST /batch/{id}/pause | /resume. */
export async function pauseBatch(id: string): Promise<ApiResult> {
  return apiPostJson(`/batch/${encodeURIComponent(id)}/pause`);
}
export async function resumeBatch(id: string): Promise<ApiResult> {
  return apiPostJson(`/batch/${encodeURIComponent(id)}/resume`);
}

/** X (Twitter) connection status, exactly as GET /x/status returns it. */
export interface XStatus { connected: boolean; account: string | null; note: string; }

export function normalizeXStatus(raw: unknown): XStatus | null {
  const r = (raw ?? {}) as Record<string, unknown>;
  if (typeof r.connected !== "boolean") return null;
  return {
    connected: r.connected,
    account: typeof r.account === "string" ? r.account : null,
    note: typeof r.note === "string" ? r.note : "",
  };
}

/** GET /x/status — null when the endpoint has not landed yet. */
export async function getXStatus(): Promise<XStatus | null> {
  const raw = await apiGet<unknown>("/x/status");
  return raw === null ? null : normalizeXStatus(raw);
}

/** POST /x/connect — returns the authorize URL to open in the system browser. */
export async function connectX(): Promise<{ url: string | null; error: string | null }> {
  const res = await apiPostJson("/x/connect");
  if (!res.ok) return { url: null, error: res.error ?? "Could not start X connect." };
  const url = (res.data as { url?: unknown } | null)?.url;
  return { url: typeof url === "string" ? url : null, error: typeof url === "string" ? null : "The server did not return a sign-in URL." };
}

/** POST /x/publish — publish a clip to X with text. */
export async function publishToX(clipId: string, text: string): Promise<ApiResult> {
  return apiPostJson("/x/publish", { clip_id: clipId, text });
}

/** One revenue row from GET /monetization/revenue. */
export interface RevenueRow {
  platform: string; views: number; rpm: number | null;
  earnings: number | null; currency: string; basis: string;
}

export function normalizeRevenue(raw: unknown): RevenueRow[] {
  const r = (raw ?? {}) as Record<string, unknown>;
  const rows = Array.isArray(r.platforms) ? r.platforms : [];
  return rows.map((row) => {
    const q = (row ?? {}) as Record<string, unknown>;
    const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
    return {
      platform: typeof q.platform === "string" ? q.platform : "",
      views: num(q.views),
      rpm: typeof q.rpm === "number" ? q.rpm : null,
      earnings: typeof q.earnings === "number" ? q.earnings : null,
      currency: typeof q.currency === "string" ? q.currency : "",
      basis: typeof q.basis === "string" ? q.basis : "",
    } satisfies RevenueRow;
  });
}

/** GET /monetization/revenue — null when the endpoint has not landed yet. */
export async function getRevenue(): Promise<RevenueRow[] | null> {
  const raw = await apiGet<unknown>("/monetization/revenue");
  return raw === null ? null : normalizeRevenue(raw);
}

/** POST /monetization/affiliate — attach an affiliate link to a clip. */
export async function addAffiliateLink(clipId: string, url: string): Promise<ApiResult> {
  return apiPostJson("/monetization/affiliate", { clip_id: clipId, url });
}

/** One A/B variant with its results. */
export interface AbVariant { title: string; thumbnail?: string | null; views: number; ctr: number | null; }

export function normalizeAbVariants(raw: unknown): AbVariant[] {
  const r = (raw ?? {}) as Record<string, unknown>;
  const rows = Array.isArray(r.variants) ? r.variants : [];
  return rows.map((row) => {
    const q = (row ?? {}) as Record<string, unknown>;
    return {
      title: typeof q.title === "string" ? q.title : "",
      thumbnail: typeof q.thumbnail === "string" ? q.thumbnail : null,
      views: typeof q.views === "number" ? q.views : 0,
      ctr: typeof q.ctr === "number" ? q.ctr : null,
    } satisfies AbVariant;
  });
}

/** POST /monetization/ab — create A/B title/thumbnail variants for a clip. */
export async function createAbTest(clipId: string, variants: { title: string; thumbnail?: string }[]): Promise<ApiResult> {
  return apiPostJson("/monetization/ab", { clip_id: clipId, variants });
}

/** GET /monetization/ab/{clip_id} — results for a clip's A/B test. */
export async function getAbResults(clipId: string): Promise<AbVariant[] | null> {
  const raw = await apiGet<unknown>(`/monetization/ab/${encodeURIComponent(clipId)}`);
  return raw === null ? null : normalizeAbVariants(raw);
}

/** One copyright-scan flag from POST /publish/scan. */
export interface PublishScanFlag { type: string; message: string; severity: string; }

export function normalizePublishScan(raw: unknown): PublishScanFlag[] {
  const r = (raw ?? {}) as Record<string, unknown>;
  const flags = Array.isArray(r.flags) ? r.flags : [];
  return flags.map((f) => {
    const q = (f ?? {}) as Record<string, unknown>;
    return {
      type: typeof q.type === "string" ? q.type : "",
      message: typeof q.message === "string" ? q.message : "",
      severity: typeof q.severity === "string" ? q.severity : "info",
    } satisfies PublishScanFlag;
  });
}

/** POST /publish/scan — copyright / policy scan before publishing a clip. */
export async function scanPublish(clipId: string): Promise<{ flags: PublishScanFlag[]; error: string | null }> {
  const res = await apiPostJson("/publish/scan", { clip_id: clipId });
  if (!res.ok) return { flags: [], error: res.error ?? "Copyright scan failed." };
  return { flags: normalizePublishScan(res.data), error: null };
}
