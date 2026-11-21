// One-click FFmpeg install UI (v0.4.1) — shared by onboarding, Settings,
// and the missing-FFmpeg banner in the main layout.
//
// Talks only to the API: POST /ffmpeg/install starts the install in the
// background, GET /ffmpeg/status is polled once a second while it runs.
// Progress, plain-language errors and retry all live here, so the three
// places that offer the install can never drift apart.
//
// After a successful install the caller is told via `onInstalled`, so it
// can re-fetch settings (the banner clears, the pill flips to "found").
// Nothing here ever downloads FFmpeg itself — the server does that.
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ffmpegInstallRunning, getFfmpegInstallStatus, getSettings, installFfmpeg,
  type FfmpegInstallState, type FfmpegInstallStatus,
} from "@/lib/api";
import { PrimaryButton, ProgressBar, SecondaryButton, ExternalLink } from "./ui";

export function useFfmpegInstaller(onInstalled?: () => void) {
  const [status, setStatus] = useState<FfmpegInstallStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onInstalledRef = useRef(onInstalled);
  onInstalledRef.current = onInstalled;
  // Fires onInstalled once when the install flips to done. Kept in an
  // effect (not inside the setStatus updater, which React requires to
  // be pure and may double-invoke) so the banner/settings re-check
  // runs exactly once per completed install.
  const prevStateRef = useRef<FfmpegInstallState | null>(null);
  useEffect(() => {
    if (status?.state === "done" && prevStateRef.current !== "done") onInstalledRef.current?.();
    prevStateRef.current = status?.state ?? null;
  }, [status]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const refresh = useCallback(async () => {
    const next = await getFfmpegInstallStatus();
    if (next) {
      setStatus(next);
      if (!ffmpegInstallRunning(next)) stopPolling();
    }
    return next;
  }, [stopPolling]);

  const start = useCallback(async (force = false) => {
    setStarting(true);
    setStartError(null);
    const res = await installFfmpeg(force);
    setStarting(false);
    if (!res.ok) {
      setStartError(res.error ?? "Could not start the FFmpeg install. Try again.");
      return false;
    }
    const next = await refresh();
    if (next && ffmpegInstallRunning(next)) {
      stopPolling();
      pollRef.current = setInterval(() => { refresh().catch(() => {}); }, 1000);
    }
    return true;
  }, [refresh, stopPolling]);

  useEffect(() => {
    refresh().catch(() => {});
    return () => stopPolling();
  }, [refresh, stopPolling]);

  return { status, starting, startError, start, refresh };
}

function fmtMB(bytes: number): string {
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

/** Full installer block: status line + button + progress + error + retry.
 *  Used in onboarding step 1 and in Settings → This app. */
export function FfmpegInstallSection({ onInstalled }: { onInstalled?: () => void }) {
  const { status, starting, startError, start } = useFfmpegInstaller(onInstalled);
  const running = ffmpegInstallRunning(status);
  // "Ready" means FFmpeg is usable — `found` is the authority, not the
  // install state: on a machine where FFmpeg is already on PATH the
  // installer never ran, so state is "idle" while found is true.
  // (Before v0.5.0 this required state === "done", so a system FFmpeg
  // wrongly rendered as "FFmpeg is missing" next to a "found" pill.)
  const done = status?.found === true;
  const error = status?.state === "error" || status?.state === "unsupported" ? status.error : null;

  if (done) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-emerald-300" role="status">
          FFmpeg is installed and ready — clipping will work.
          {status?.version ? <span className="text-neutral-500"> ({status.version})</span> : null}
          {status?.path ? <span className="block truncate font-mono text-xs text-neutral-500">{status.path}</span> : null}
        </p>
        {status?.path_warning ? (
          <p className="text-sm text-amber-300" role="status">{status.path_warning}</p>
        ) : null}
        {status?.checksum_verified === false ? (
          <p className="text-xs text-neutral-500">
            The download checksum could not be checked (no checksum file was published for this
            build) — the file still came over HTTPS from the official site.
          </p>
        ) : null}
        <SecondaryButton onClick={() => start(true)} disabled={starting || running}>
          {running ? "Reinstalling…" : "Reinstall FFmpeg"}
        </SecondaryButton>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-sm text-neutral-300">
        FFmpeg is missing — ClipForge needs it to cut video clips. One click installs the official build
        into the app&apos;s data folder and adds it to your Windows PATH. No restart needed.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <PrimaryButton onClick={() => start()} disabled={starting || running}>
          {starting ? "Starting…" : running ? "Installing…" : error ? "Try installing FFmpeg again" : "Install FFmpeg"}
        </PrimaryButton>
        {status?.download_url && !running && (
          <ExternalLink className="text-sm text-sky-400 underline" href={status.download_url}>
            Download it manually instead
          </ExternalLink>
        )}
      </div>
      {running && status && (
        <div className="space-y-1" role="status">
          <ProgressBar value={status.progress_pct} />
          <p className="text-sm text-neutral-300">
            {status.message || "Installing FFmpeg…"} {Math.round(status.progress_pct)}%
            {status.total_bytes ? ` — ${fmtMB(status.downloaded_bytes)} of ${fmtMB(status.total_bytes)}` : ""}
          </p>
          <p className="text-xs text-neutral-500">Keep the app open. About 100 MB on Windows, 40 MB on Linux.</p>
        </div>
      )}
      {(error || startError) && (
        <p className="text-sm text-red-300" role="alert">
          {error || startError}
          {status?.state === "unsupported" ? "" : " Nothing was half-installed — try again."}
        </p>
      )}
    </div>
  );
}

/** Banner shown across the app while FFmpeg is missing. The button
 *  expands the installer inline; once the install finishes the banner
 *  re-checks settings and clears itself. */
export function FfmpegMissingBanner() {
  const [missing, setMissing] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const check = useCallback(async () => {
    const settings = await getSettings();
    setMissing(settings !== null && settings.ffmpeg.found === false);
  }, []);

  useEffect(() => { check().catch(() => {}); }, [check]);

  if (!missing) return null;
  return (
    <div className="bg-amber-500/15 px-4 py-2.5 text-center text-sm text-amber-200" role="status">
      {!expanded ? (
        <>
          <span className="font-medium">FFmpeg is missing — clipping needs it.</span>{" "}
          <span className="text-amber-200/80">One click installs it (about 2 minutes, no restart needed).</span>{" "}
          <button onClick={() => setExpanded(true)} className="font-semibold underline hover:text-white">
            Install FFmpeg
          </button>
        </>
      ) : (
        <div className="mx-auto max-w-2xl text-left">
          <FfmpegInstallSection onInstalled={() => { setExpanded(false); check().catch(() => {}); }} />
        </div>
      )}
    </div>
  );
}
