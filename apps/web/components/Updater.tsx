// Updater UI (v0.4.0) — "Update available" banner + Settings → Updates
// section, wired to the desktop worker's Tauri commands
// (check_for_updates, download_and_install_update, get_update_status).
//
// Those commands did not exist in the Rust side at v0.3.0, so until they
// land every call resolves to null and the UI says so plainly instead of
// pretending to check: the banner stays hidden, and the Settings section
// explains updates are not available in this build yet and links to the
// GitHub releases page. Nothing here ever runs in a plain browser tab —
// update checks need the desktop app.
//
// Promise: an update never touches your clips, settings or keys —
// the installer only replaces the app itself.
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { getHealth } from "@/lib/api";
import { checkForUpdates, downloadAndInstallUpdate, fmtBytes, getUpdateStatus, isTauri, type UpdateInfo, type UpdateStatus } from "@/lib/tauri";
import { PrimaryButton, ProgressBar, SecondaryButton, Skeleton, ExternalLink } from "./ui";

const RELEASES_URL = "https://github.com/hamxa8769/clipforge/releases";

type UpdaterState =
  | { kind: "unavailable" } // not the desktop app, or commands not in this build yet
  | { kind: "checking" }
  | { kind: "uptodate"; current: string }
  | { kind: "available"; info: UpdateInfo; current: string }
  | { kind: "downloading"; info: UpdateInfo; progress: number }
  | { kind: "ready"; info: UpdateInfo }
  | { kind: "error"; message: string };

function useUpdater() {
  const [state, setState] = useState<UpdaterState>({ kind: "checking" });
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const check = useCallback(async () => {
    stopPolling();
    if (!isTauri()) { setState({ kind: "unavailable" }); return; }
    setState({ kind: "checking" });
    const [info, health] = await Promise.all([checkForUpdates(), getHealth()]);
    const current = health?.version ?? "";
    if (info === null) { setState({ kind: "unavailable" }); return; }
    if (info.error) { setState({ kind: "error", message: info.error }); return; }
    if (info.available) setState({ kind: "available", info, current });
    else setState({ kind: "uptodate", current: info.current_version ?? current });
  }, [stopPolling]);

  const install = useCallback(async () => {
    setState((prev) => (prev.kind === "available" ? { kind: "downloading", info: prev.info, progress: 0 } : prev));
    const ok = await downloadAndInstallUpdate();
    if (!ok) {
      setState({ kind: "error", message: "The update could not start. Try again, or download it from the releases page." });
      return;
    }
    pollRef.current = setInterval(async () => {
      const status: UpdateStatus | null = await getUpdateStatus();
      if (!status) return;
      if (status.state === "ready") {
        stopPolling();
        setState((prev) => (prev.kind === "downloading" ? { kind: "ready", info: prev.info } : prev));
      } else if (status.state === "error" || status.state === "failed") {
        // The Rust updater reports failure as "failed"; accept "error"
        // too so an older/other build still lands on the same message.
        stopPolling();
        setState({ kind: "error", message: status.error ?? "The update failed. Your current version is untouched — try again or download it manually." });
      } else if (status.state === "downloading") {
        setState((prev) => (prev.kind === "downloading" ? { ...prev, progress: status.progress_pct ?? prev.progress } : prev));
      }
    }, 1000);
  }, [stopPolling]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  return { state, check, install };
}

/** Top-of-app banner — only renders when an update is actually available (or downloading / ready). */
export function UpdateBanner() {
  const { state, check, install } = useUpdater();
  useEffect(() => { check().catch(() => {}); }, [check]);
  if (state.kind !== "available" && state.kind !== "downloading" && state.kind !== "ready") return null;
  const info = state.info;
  return (
    <div className="bg-sky-500/15 px-4 py-2.5 text-center text-sm text-sky-100" role="status">
      {state.kind === "ready" ? (
        <>Update v{info.version} is downloaded and verified — on Windows the installer starts now; on Linux, run the downloaded installer, then restart ClipForge. Your clips, settings and keys are kept.</>
      ) : state.kind === "downloading" ? (
        <>Downloading update v{info.version}… {Math.round(state.progress)}% — keep the app open. Your clips, settings and keys are kept.</>
      ) : (
        <>
          Update available: v{info.version}
          {info.size_bytes ? ` (${fmtBytes(info.size_bytes)})` : ""} —{" "}
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
