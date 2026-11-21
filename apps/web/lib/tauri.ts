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
