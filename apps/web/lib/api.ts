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
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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

export interface Job { id: string; title: string; source: string; status: JobStatus; created_at: string; candidates: number; }
export interface SeoVariant { title: string; description: string; hashtags: string[]; }
export interface Candidate {
  id: string; job_id: string; n: number; score: number; hook_rating: number; hook_line: string;
  reason: string; transcript_excerpt: string; start_s: number; end_s: number;
  status: "pending" | "approved" | "rejected";
  /** Server-side rendered clip path (e.g. data/clips/<job>/clip_01.mp4), if rendered. */
  file_path: string | null;
  seo: Record<string, SeoVariant>;
}
export interface Post { id: string; clip_id: string; platform: string; caption: string; scheduled_at: string; status: PostStatus; }
export interface Monetization { platform: string; program: string; current: Record<string, number>; threshold: Record<string, number>; progress_pct: number; note?: string; }
export interface Health { status: string; dry_run: boolean }

/** Normalise a single raw job payload (unknown wire shape) into a typed Job. */
export function normalizeJob(raw: unknown): Job {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    id: String(r.id ?? ""),
    title: String(r.title ?? ""),
    source: String(r.source ?? ""),
    status: normalizeJobStatus(r.status),
    created_at: String(r.created_at ?? ""),
    candidates: typeof r.candidates === "number" ? r.candidates : 0,
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
