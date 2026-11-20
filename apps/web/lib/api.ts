// API client + clearly-labelled demo fallback.
// Contract assumed (FastAPI, base http://localhost:8000):
//   GET  /health -> { status, dry_run }
//   GET  /jobs -> Job[]
//   GET  /jobs/{id} -> Job ; GET /jobs/{id}/candidates -> Candidate[]
//   GET  /posts -> Post[] (approval queue) ; POST /posts/{id}/approve ; POST /posts/{id}/publish
//   GET  /monetization -> Monetization[]
//   GET  /analytics -> AnalyticsSummary
// When the API is unreachable, callers use mock data and must show a "Demo data" badge.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type JobStatus = "queued" | "transcribing" | "scoring" | "ready" | "failed";
export interface Job { id: string; title: string; source: string; status: JobStatus; created_at: string; candidates: number; }
export interface SeoVariant { title: string; description: string; hashtags: string[]; }
export interface Candidate {
  id: string; job_id: string; score: number; hook_rating: number; hook_line: string;
  reason: string; transcript_excerpt: string; start_s: number; end_s: number;
  status: "pending" | "approved" | "rejected";
  seo: Record<string, SeoVariant>;
}
export interface Post { id: string; clip_id: string; platform: string; caption: string; scheduled_at: string; status: string; }
export interface Monetization { platform: string; program: string; current: Record<string, number>; threshold: Record<string, number>; progress_pct: number; note?: string; }
export interface Health { status: string; dry_run: boolean }

export async function apiGet<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${API_URL}${path}`, { cache: "no-store", signal: AbortSignal.timeout(3000) });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch { return null; }
}
export async function apiPost(path: string, body: unknown = {}): Promise<boolean> {
  try {
    const res = await fetch(`${API_URL}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(5000) });
    return res.ok;
  } catch { return false; }
}
export async function getHealth(): Promise<Health | null> { return apiGet<Health>("/health"); }
