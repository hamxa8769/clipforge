// Updater UI (v0.4.0, fixed v0.10.2) — "Update available" banner +
// Settings → Updates section, wired to the desktop worker's Tauri
// commands (check_for_updates, download_and_install_update,
// get_update_status).
//
// The v0.10.2 failures this fixes (the user saw "nothing happens"
// even though a newer release existed):
// * The check raced the quiet launch check: the Rust side answered
//   a busy check with an error, the UI showed that error and never
//   looked at the updater state again — the available update stayed
//   hidden for the session. Now the UI first adopts the launch
//   check's result from get_update_status, and retries a busy check.
// * The banner hid every non-available state, so a failed check
//   looked exactly like "no update". Now a failed check shows in the
//   banner too, with a retry button and the releases link.
// * Download progress never showed: the install code only started
//   polling after the download promise resolved, and the Rust
//   command ran on the main thread, so the poll could never run
//   during the download anyway. Now polling starts before the
//   download, and the Rust side runs it on a worker thread.
// * A failed download showed a generic message. Now the UI reads
//   the real error from get_update_status.
//
// Promise: an update never touches your clips, settings or keys —
// the installer only replaces the app itself.
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { checkForUpdates, downloadAndInstallUpdate, fmtBytes, getUpdateStatus, isTauri, isUpdateCheckBusy, updateInfoFromStatus, type UpdateInfo, type UpdateStatus } from "@/lib/tauri";
import { useServerStatus } from "./ServerStatusProvider";
import { PrimaryButton, ProgressBar, SecondaryButton, Skeleton, ExternalLink } from "./ui";

const RELEASES_URL = "https://github.com/hamxa8769/clipforge/releases";

/** Busy-check retries: the launch check usually finishes in ~1 s. */
const CHECK_BUSY_RETRIES = 3;
const CHECK_BUSY_DELAY_MS = 2000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

type UpdaterState =
  | { kind: "unavailable" } // not the desktop app, or commands not in this build yet
  | { kind: "checking" }
  | { kind: "uptodate"; current: string }
  | { kind: "available"; info: UpdateInfo; current: string }
  | { kind: "downloading"; info: UpdateInfo; progress: number }
  | { kind: "ready"; info: UpdateInfo }
  | { kind: "error"; message: string };

function useUpdater() {
  // The running version comes from the shared server status (v0.9.3)
  // — the updater no longer fetches /health itself.
  const server = useServerStatus();
  const [state, setState] = useState<UpdaterState>({ kind: "checking" });
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  /** Map a Rust updater-state snapshot onto UI state. Returns null
   *  when the snapshot holds no settled outcome yet (idle/checking),
   *  so the caller falls through to a fresh check. */
  const stateFromStatus = useCallback((status: UpdateStatus, current: string): UpdaterState | null => {
    if (status.state === "failed" || status.state === "error") {
      return { kind: "error", message: status.error ?? "The update check failed. Try again, or download it from the releases page." };
    }
    const info = updateInfoFromStatus(status, current);
    if (!info) return null;
    if (status.state === "available") return { kind: "available", info, current };
    if (status.state === "downloading") return { kind: "downloading", info, progress: status.progress_pct ?? 0 };
    if (status.state === "ready") return { kind: "ready", info };
    return null;
  }, []);

  const check = useCallback(async () => {
    stopPolling();
    if (!isTauri()) { setState({ kind: "unavailable" }); return; }
    setState({ kind: "checking" });
    const current = server.health?.version ?? "";
    // 1. Adopt what the quiet launch check already found. The launch
    // check runs at startup; if it already settled, its result is in
    // the Rust state — a fresh check would only race it (and the old
    // code turned that race into a sticky error that hid the update).
    const status = await getUpdateStatus();
    if (status) {
      const adopted = stateFromStatus(status, current);
      if (adopted) { setState(adopted); return; }
    }
    // 2. Fresh check. A busy answer ("already in progress") just
    // means the launch check is still running: wait and retry instead
    // of showing it as a failure.
    for (let attempt = 0; ; attempt++) {
      const info = await checkForUpdates();
      if (info === null) { setState({ kind: "unavailable" }); return; }
      if (info.error && isUpdateCheckBusy(info.error) && attempt < CHECK_BUSY_RETRIES) {
        await sleep(CHECK_BUSY_DELAY_MS);
        continue;
      }
      if (info.error) { setState({ kind: "error", message: info.error }); return; }
      if (info.available) setState({ kind: "available", info, current });
      else setState({ kind: "uptodate", current: info.current_version ?? current });
      return;
    }
    // Re-checks when the shared health (hence the version) arrives,
    // so the displayed version is never stale from mount time.
  }, [stopPolling, stateFromStatus, server.health?.version]);

  const install = useCallback(async () => {
    // Start polling FIRST (v0.10.2): progress arrives through
    // get_update_status while the download is in flight. The old code
    // only started polling after the download promise resolved — i.e.
    // never during the download — so the user saw "0%" frozen.
    let settled = false;
    const poll = async () => {
      const status: UpdateStatus | null = await getUpdateStatus();
      if (!status || settled) return;
      if (status.state === "ready") {
        settled = true;
        stopPolling();
        setState((prev) => (prev.kind === "downloading" ? { kind: "ready", info: prev.info } : prev));
      } else if (status.state === "error" || status.state === "failed") {
        // The Rust updater reports failure as "failed"; accept "error"
        // too so an older/other build still lands on the same message.
        settled = true;
        stopPolling();
        setState({ kind: "error", message: status.error ?? "The update failed. Your current version is untouched — try again or download it manually." });
      } else if (status.state === "downloading") {
        setState((prev) => (prev.kind === "downloading" ? { ...prev, progress: status.progress_pct ?? prev.progress } : prev));
      }
    };
    setState((prev) => (prev.kind === "available" ? { kind: "downloading", info: prev.info, progress: 0 } : prev));
    pollRef.current = setInterval(() => { poll().catch(() => {}); }, 1000);
    await poll().catch(() => {});
    const ok = await downloadAndInstallUpdate();
    if (settled) return; // the poll already showed ready / failed
    stopPolling();
    if (!ok) {
      // The command failed: read the real reason from the Rust state
      // instead of a generic "could not start".
      const status = await getUpdateStatus();
      setState({ kind: "error", message: status?.error ?? "The update could not start. Try again, or download it from the releases page." });
      return;
    }
    // Resolved without the poll seeing "ready" (Linux manual-install
    // path): confirm from the state before claiming anything.
    const status = await getUpdateStatus();
    if (status?.state === "ready") {
      setState((prev) => (prev.kind === "downloading" ? { kind: "ready", info: prev.info } : prev));
    } else if (status && (status.state === "failed" || status.state === "error")) {
      setState({ kind: "error", message: status.error ?? "The update failed. Your current version is untouched — try again or download it manually." });
    }
  }, [stopPolling]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  return { state, check, install };
}

/** Top-of-app banner — renders when an update is available,
 *  downloading / ready, or when the check failed (v0.10.2: a failed
 *  check used to render nothing, so the user saw no update and no
 *  error — just silence). */
export function UpdateBanner() {
  const { state, check, install } = useUpdater();
  useEffect(() => { check().catch(() => {}); }, [check]);
  if (state.kind !== "available" && state.kind !== "downloading" && state.kind !== "ready" && state.kind !== "error") return null;
  return (
    <div className="bg-sky-500/15 px-4 py-2.5 text-center text-sm text-sky-100" role="status">
      {state.kind === "error" ? (
        <>Couldn&apos;t check for updates: {state.message}{" "}
          <button onClick={() => check()} className="font-semibold underline hover:text-white">Try again</button>
          {" · "}<ExternalLink className="underline hover:text-white" href={RELEASES_URL}>Download from GitHub</ExternalLink>
        </>
      ) : state.kind === "ready" ? (
        <>Update v{state.info.version} is downloaded and verified — on Windows the installer starts now; on Linux, run the downloaded installer, then restart ClipForge. Your clips, settings and keys are kept.</>
      ) : state.kind === "downloading" ? (
        <>Downloading update v{state.info.version}… {Math.round(state.progress)}% — keep the app open. Your clips, settings and keys are kept.</>
      ) : (
        <>
          Update available: v{state.info.version}
          {state.info.size_bytes ? ` (${fmtBytes(state.info.size_bytes)})` : ""} —{" "}
          <button onClick={() => install()} className="font-semibold underline hover:text-white">Download &amp; install</button>
          {" · "}your clips, settings and keys are kept.
        </>
      )}
    </div>
  );
}

/** Settings → Updates section: every state, in plain words. */
export function UpdateSection() {
  const { state, check, install } = useUpdater();
  useEffect(() => { check().catch(() => {}); }, [check]);

  return (
    <div className="space-y-3">
      {state.kind === "checking" && <Skeleton className="h-6 w-64" />}
      {state.kind === "unavailable" && (
        <>
          <p className="text-sm text-neutral-300">
            Update checks need the desktop app, and this build does not have the updater yet. New versions are published on GitHub Releases.
          </p>
          <ExternalLink className="text-sm text-sky-400 underline" href={RELEASES_URL}>Open ClipForge releases on GitHub</ExternalLink>
          <p className="text-xs text-neutral-500">Updating never touches your clips, settings or keys — the installer only replaces the app itself.</p>
        </>
      )}
      {state.kind === "uptodate" && (
        <>
          <p className="text-sm text-neutral-300">You&apos;re up to date{state.current ? ` (v${state.current})` : ""}.</p>
          <SecondaryButton onClick={() => check()}>Check again</SecondaryButton>
        </>
      )}
      {state.kind === "available" && (
        <>
          <p className="text-sm text-neutral-300">
            Update available: <strong className="text-neutral-100">v{state.info.version}</strong>
            {state.info.size_bytes ? ` · ${fmtBytes(state.info.size_bytes)}` : ""}
            {state.current ? ` · you have v${state.current}` : ""}.
          </p>
          {state.info.notes && <p className="text-sm text-neutral-400">{state.info.notes}</p>}
          <div className="flex flex-wrap gap-2">
            <PrimaryButton onClick={() => install()}>Download &amp; install</PrimaryButton>
            <SecondaryButton onClick={() => check()}>Check again</SecondaryButton>
          </div>
          <p className="text-xs text-neutral-500">The app restarts to finish installing. Your clips, settings and keys are kept.</p>
        </>
      )}
      {state.kind === "downloading" && (
        <>
          <p className="text-sm text-neutral-300">Downloading update v{state.info.version}… {Math.round(state.progress)}%</p>
          <ProgressBar value={state.progress} />
          <p className="text-xs text-neutral-500">Keep the app open. Your clips, settings and keys are kept.</p>
        </>
      )}
      {state.kind === "ready" && (
        <p className="text-sm text-neutral-300">Update v{state.info.version} is downloaded and verified. On Windows the installer starts now; on Linux, run the downloaded installer, then restart ClipForge — your clips, settings and keys are kept.</p>
      )}
      {state.kind === "error" && (
        <>
          <p className="text-sm text-red-200">{state.message}</p>
          <div className="flex flex-wrap gap-2">
            <SecondaryButton onClick={() => check()}>Try again</SecondaryButton>
            <ExternalLink className="inline-flex items-center justify-center rounded-md bg-neutral-800 px-3 py-1.5 text-sm font-medium text-neutral-200 hover:bg-neutral-700" href={RELEASES_URL}>Download from GitHub</ExternalLink>
          </div>
        </>
      )}
    </div>
  );
}
