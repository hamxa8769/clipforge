// Tauri bridge for the desktop app (v0.4.0).
//
// The web UI runs inside the Tauri main window in the desktop app and in
// a plain browser during dev / when served standalone. Every call here
// first checks for the Tauri runtime and returns null when it is absent
// or the command does not exist yet — the UI must never crash or hang
// because a desktop command is missing; it shows an honest "not
// available in this build" state instead.
//
// Updater commands (check_for_updates, download_and_install_update,
// get_update_status) are owned by the desktop worker: they did not exist
// in apps/desktop/src-tauri/src/main.rs at v0.3.0. This UI is written
// against those exact names; until the Rust side lands them, every
// updater call resolves to null and the UI says updates are not
// available in this build yet.
//
// Server commands (v0.9.3): `get_server_status` and `retry_server`
// exist in main.rs and are granted by capabilities/default.json
// (allow-get-server-status / allow-retry-server) — the web UI can
// restart a dead server with the Restart button instead of leaving
// the user to relaunch the whole app. In a plain browser both resolve
// to null/false and no restart button is shown.
import { isExternalUrl } from "./external-links";

declare global {
  interface Window {
    __TAURI__?: { core?: { invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> } };
  }
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && typeof window.__TAURI__?.core?.invoke === "function";
}

async function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  if (!isTauri()) return null;
  try {
    const res = await window.__TAURI__!.core!.invoke!(cmd, args);
    return res as T;
  } catch {
    // Unknown command (not registered yet) or a Rust-side error: the
    // caller treats null as "not available", never as a crash.
    return null;
  }
}

/** Server log tail from the desktop sidecar (Rust get_server_logs). */
export async function getServerLogs(lines = 120): Promise<string | null> {
  const res = await tauriInvoke<string>("get_server_logs", { lines });
  return typeof res === "string" ? res : null;
}

/**
 * Desktop shell's view of the server (Rust get_server_status), exactly
 * as main.rs serialises ServerStatus: a `phase` tag — "starting" (with
 * elapsed_secs + detail), "ready" (with url + port) or "failed" (with
 * error) — plus how it was launched, the data dir, the log path, and
 * the splash-time FFmpeg check. Null outside the desktop app.
 *
 * NOTE: the phase is set once at startup and never updated
 * afterwards (diagnosis §1.1) — a mid-run death still reports the
 * stale "ready" phase. The web UI treats GET /health (the shared
 * server status) as the truth about liveness, and uses this only to
 * describe a restart the shell just started.
 */
export interface RustServerStatus {
  phase: "starting" | "ready" | "failed";
  elapsed_secs?: number;
  detail?: string;
  url?: string;
  port?: number;
  error?: string;
  source: string;
  data_dir: string;
  log_path: string;
  ffmpeg: { available: boolean; version: string | null; path: string | null };
}

/** Ask the desktop shell what phase the server is in. Null = unavailable in this build. */
export async function getServerStatus(): Promise<RustServerStatus | null> {
  const res = await tauriInvoke<RustServerStatus>("get_server_status");
  return res !== null && typeof res === "object" && typeof (res as { phase?: unknown }).phase === "string"
    ? res
    : null;
}

/**
 * Ask the desktop shell to (re)start the server (Rust retry_server).
 * Safe to call when a server is merely slow: the shell re-waits on a
 * live child instead of killing it. Returns true when the shell
 * accepted the restart; false when the command is unavailable (plain
 * browser / older build) or the shell refused it (e.g. a start is
 * already in progress). The caller must not claim success on false —
 * the shared /health poll is what confirms the server is back.
 *
 * NOTE: retry_server returns Result<(), String>, and Tauri resolves
 * Ok(()) as JSON null — so this does NOT use tauriInvoke (whose null
 * means "error"): resolve means accepted, reject means refused.
 */
export async function retryServer(): Promise<boolean> {
  if (!isTauri()) return false;
  try {
    await window.__TAURI__!.core!.invoke!("retry_server");
    return true;
  } catch {
    return false;
  }
}

export interface UpdateInfo {
  available: boolean;
  version?: string;
  current_version?: string;
  notes?: string;
  size_bytes?: number;
  download_url?: string;
  /** Set when the check itself failed (e.g. GitHub unreachable). */
  error?: string;
}

export interface UpdateStatus {
  state: string; // e.g. "idle" | "checking" | "available" | "downloading" | "ready" | "failed" ("error" accepted too)
  progress_pct?: number;
  version?: string;
  size_bytes?: number;
  error?: string;
}

/** Ask the desktop updater whether a newer release exists. Null = unavailable in this build. */
export async function checkForUpdates(): Promise<UpdateInfo | null> {
  return tauriInvoke<UpdateInfo>("check_for_updates");
}

/** Download + install the pending update (the app restarts afterwards). */
export async function downloadAndInstallUpdate(): Promise<boolean> {
  const res = await tauriInvoke<unknown>("download_and_install_update");
  return res !== null;
}

/** Current updater state (progress while downloading, error if any). */
export async function getUpdateStatus(): Promise<UpdateStatus | null> {
  return tauriInvoke<UpdateStatus>("get_update_status");
}

/** Human-readable byte size, e.g. 28.4 MB. */
export function fmtBytes(n: number | undefined | null): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Open an external URL in the user's system browser (Chrome).
 *
 * This is the ONLY way the UI opens an external link. Do not use
 * `window.open(url, "_blank")` or a plain `<a target="_blank">` for
 * external URLs: inside the Tauri desktop app the WebView has no
 * new-window handler, so `window.open` opens nothing, and a plain anchor
 * only reaches the browser through the opener plugin — which the ACL
 * denies unless a capability grants it (v0.4.1 had no capability file
 * at all, so NO link in the app opened: not Connect YouTube, not the
 * developer consoles, not FFmpeg / releases links).
 *
 * In the desktop app this invokes the Rust `open_external_url` command,
 * which opens the URL with the OS default browser via the opener plugin
 * (granted by `capabilities/default.json`). In a plain browser it falls
 * back to `window.open`. Returns `true` only when a browser window/tab
 * was actually opened — callers must not claim "opened" on `false`
 * (v0.4.1's Connect step did exactly that). Non-http(s) URLs are
 * refused here and again on the Rust side.
 */
export async function openExternal(url: string): Promise<boolean> {
  if (!isExternalUrl(url)) return false;
  if (isTauri()) {
    const opened = await tauriInvoke<boolean>("open_external_url", { url });
    if (opened === true) return true;
    // Command missing (older desktop build) or refused: fall through to
    // window.open, which works in a plain browser and is a no-op in the
    // WebView — the caller still reports the failure honestly.
  }
  if (typeof window === "undefined") return false;
  const w = window.open(url, "_blank", "noopener,noreferrer");
  return w !== null;
}
