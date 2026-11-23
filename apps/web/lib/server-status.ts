// Single source of truth for "is the ClipForge server up?" (v0.9.3).
//
// THE BUG (v0.9.2, user's screenshot 2026-11-23): every component
// probed the server on its own, at different times, through different
// endpoints — TopBar polled GET /health every 30 s, the status bar
// polled /health + /settings + /perf/profile, Settings fetched
// /settings exactly once on mount, the dashboard polled only while it
// thought the API was live. So one transient failure left the app
// contradicting itself: the header said "Live — server connected"
// while Settings said "Server unreachable — settings cannot be
// changed" and the status bar's FFmpeg pill sat on "checking…"
// forever. No page ever recovered without a manual reload.
//
// The split is structural, not a race: GET /health is a static,
// no-I/O liveness probe that answers in milliseconds, while GET
// /settings runs the OS keyring reads plus the FFmpeg / yt-dlp
// `--version` probes (up to 10 s per candidate on the server), so on
// Windows /settings routinely takes longer than the web client's
// 3 s fetch timeout — /health succeeds and /settings times out, in
// the same second, on the same server.
//
// The fix: exactly ONE poller of exactly ONE endpoint — GET /health,
// the fast liveness probe — owned by ServerStatusProvider (mounted
// once in the root layout). Every component and page reads liveness
// from the shared useServerStatus() hook and never probes /health
// itself. Slower endpoints (/settings, /requirements/status,
// /ffmpeg/status, /yt-dlp/status) get a longer timeout (defined in
// lib/api.ts as SLOW_API_TIMEOUT_MS), and every pill that depends on
// them has a settled "couldn't check" state instead of hanging on
// "checking…" forever.
//
// This file is pure logic (no React) so the state machine and the
// backoff schedule are unit-testable under Node — see
// lib/server-status.test.ts. The React provider lives in
// components/ServerStatusProvider.tsx.
import type { Health } from "./api";

/** The three states the whole UI agrees on. "checking" exists only
 *  before the very first probe settles; after that the UI is either
 *  "live" or "reconnecting" — never "unknown" in two places at once. */
export type ServerStatus = "checking" | "live" | "reconnecting";

/** How often a live server is re-checked. */
export const LIVE_POLL_MS = 15000;

/** Delay before reconnect attempt N (N = failed probes so far, 0-based):
 *  1 s, 2 s, 4 s, 8 s, then capped at 15 s. Fast enough to feel alive
 *  when the server restarts, slow enough not to hammer a struggling
 *  one. */
export function reconnectDelayMs(attempt: number): number {
  if (attempt <= 0) return 1000;
  return Math.min(15000, 1000 * 2 ** attempt);
}

/** Plain-language label for the shared status, for the header pill
 *  and anywhere else that names the connection. */
export function serverStatusLabel(status: ServerStatus, attempts: number): string {
  switch (status) {
    case "checking":
      return "Checking server…";
    case "live":
      return "Live — server connected";
    case "reconnecting":
      return `Reconnecting… (attempt ${Math.max(1, attempts)})`;
  }
}

/** Everything the provider shares: the agreed status, the last good
 *  /health payload (null while down), how many reconnect attempts
 *  have failed so far, and when the server was last seen live. */
export interface ServerStatusSnapshot {
  status: ServerStatus;
  /** Last good /health payload; null when the server is down. */
  health: Health | null;
  /** Failed reconnect probes since the server was last live. */
  attempts: number;
  /** Epoch ms of the last successful probe, or null if never live. */
  lastLiveAt: number | null;
}

export const INITIAL_SERVER_STATUS: ServerStatusSnapshot = {
  status: "checking",
  health: null,
  attempts: 0,
  lastLiveAt: null,
};

/**
 * Plain-language age of the last successful probe ("just now",
 * "45 s ago", "3 min ago") for the reconnecting notice. Null when
 * the server was never seen live — the UI then says nothing about a
 * last sighting instead of inventing one.
 */
export function formatLastSeenAge(lastLiveAt: number | null, now: number): string | null {
  if (lastLiveAt === null) return null;
  const secs = Math.max(0, Math.floor((now - lastLiveAt) / 1000));
  if (secs < 10) return "just now";
  if (secs < 60) return `${secs} s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  return `${Math.floor(mins / 60)} h ago`;
}

/**
 * Pure state machine: fold one /health probe result into the previous
 * snapshot. A success always resets to live with attempts 0; a
 * failure moves to reconnecting (attempts +1) and keeps lastLiveAt so
 * the UI can say "last connected at …" instead of pretending it never
 * worked. "checking" only survives until the first probe settles.
 */
export function reduceServerStatus(
  prev: ServerStatusSnapshot,
  probe: { ok: boolean; health: Health | null; now: number },
): ServerStatusSnapshot {
  if (probe.ok && probe.health !== null) {
    return { status: "live", health: probe.health, attempts: 0, lastLiveAt: probe.now };
  }
  return {
    status: "reconnecting",
    health: null,
    attempts: prev.status === "reconnecting" ? prev.attempts + 1 : 1,
    lastLiveAt: prev.lastLiveAt,
  };
}
