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
