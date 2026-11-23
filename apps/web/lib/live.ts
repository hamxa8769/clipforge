// Pure helpers for the Live monitoring page (v0.10.0).
// No fetches here: the API calls live in lib/api.ts, and these
// normalisers are unit-tested with `npm run test:unit`.
import type { LivePeak, LiveSession } from "./api.js";

/** A live session's display state: active (starting/live), ended, or failed. */
export function liveSessionState(session: LiveSession | null): "active" | "ended" | "failed" | "unknown" {
  if (!session) return "unknown";
  if (session.status === "failed") return "failed";
  if (session.status === "starting" || session.status === "live") return "active";
  return "ended";
}

/** True when the session is still monitoring (polling should continue). */
export function isLiveSessionActive(session: LiveSession | null): boolean {
  return liveSessionState(session) === "active";
}

/** "mm:ss" for an elapsed-seconds value. */
export function fmtElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds || 0));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

/** Peak window as "start–end" seconds, e.g. "53.0s – 93.0s". */
export function fmtPeakWindow(peak: LivePeak): string {
  return `${peak.start_s.toFixed(1)}s – ${peak.end_s.toFixed(1)}s`;
}

/** One-line chat-availability label for a platform capability row. */
export function liveChatLabel(chat: boolean | string): string {
  if (chat === true) return "Chat: yes, no key needed";
  if (chat === "oauth") return "Chat: needs your channel connected (OAuth)";
  return "Chat: not available (audio-only)";
}

/** Peaks that already have a pipeline job, newest trigger first. */
export function peaksWithJobs(session: LiveSession | null): LivePeak[] {
  if (!session) return [];
  return [...session.peaks].filter((p) => p.job_id).sort((a, b) => b.trigger_s - a.trigger_s);
}

/** Peaks whose cut failed or whose job could not be created. */
export function missedPeaks(session: LiveSession | null): LivePeak[] {
  if (!session) return [];
  return session.peaks.filter((p) => !p.job_id && p.error);
}
