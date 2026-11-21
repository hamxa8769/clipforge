// One-click yt-dlp install UI (v0.8.1) — mirrors FfmpegInstaller.tsx.
//
// Talks only to the API: POST /yt-dlp/install starts the install in the
// background, GET /yt-dlp/status is polled once a second while it runs.
// Progress, plain-language errors and retry all live here, so the places
// that offer the install (the blocked Ingest stage on Pipeline, the
// Agent page, Settings → This app) can never drift apart.
//
// After a successful install the caller is told via `onInstalled`, so it
// can re-fetch the job / agent status (the blocked stage clears, the
// retry button runs the job again). Nothing here ever downloads yt-dlp
// itself — the server does that.
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ytdlpInstallRunning, getYtDlpInstallStatus, installYtDlp,
  type YtDlpInstallState, type YtDlpInstallStatus,
} from "@/lib/api";
import { PrimaryButton, ProgressBar, SecondaryButton, ExternalLink } from "./ui";

export function useYtDlpInstaller(onInstalled?: () => void) {
  const [status, setStatus] = useState<YtDlpInstallStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onInstalledRef = useRef(onInstalled);
  onInstalledRef.current = onInstalled;
  // Fires onInstalled once when the install flips to done. Kept in an
  // effect (not inside the setStatus updater, which React requires to
  // be pure and may double-invoke) so the re-check runs exactly once
  // per completed install.
  const prevStateRef = useRef<YtDlpInstallState | null>(null);
  useEffect(() => {
    if (status?.state === "done" && prevStateRef.current !== "done") onInstalledRef.current?.();
    prevStateRef.current = status?.state ?? null;
  }, [status]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const refresh = useCallback(async () => {
    const next = await getYtDlpInstallStatus();
    if (next) {
      setStatus(next);
      if (!ytdlpInstallRunning(next)) stopPolling();
    }
    return next;
  }, [stopPolling]);

  const start = useCallback(async (force = false) => {
    setStarting(true);
    setStartError(null);
    const res = await installYtDlp(force);
    setStarting(false);
    if (!res.ok) {
      setStartError(res.error ?? "Could not start the yt-dlp install. Try again.");
      return false;
    }
    const next = await refresh();
    if (next && ytdlpInstallRunning(next)) {
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
 *  Used on the blocked Ingest stage (Pipeline), the Agent page, and in
 *  Settings → This app. */
export function YtDlpInstallSection({ onInstalled }: { onInstalled?: () => void }) {
  const { status, starting, startError, start } = useYtDlpInstaller(onInstalled);
  const running = ytdlpInstallRunning(status);
  // "Ready" means yt-dlp is usable — `found` is the authority, not the
  // install state: on a machine where yt-dlp is already on PATH the
  // installer never ran, so state is "idle" while found is true.
  const done = status?.found === true;
  const error = status?.state === "error" || status?.state === "unsupported" ? status.error : null;

  if (done) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-emerald-300" role="status">
          yt-dlp is installed and ready — YouTube ingest will work now.
          {status?.version ? <span className="text-neutral-500"> (version {status.version})</span> : null}
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
          {running ? "Reinstalling…" : "Reinstall yt-dlp"}
        </SecondaryButton>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-sm text-neutral-300">
        yt-dlp is missing — ClipForge needs it to download YouTube sources. One click installs the official
        release binary into the app&apos;s data folder and adds it to your Windows PATH. No restart needed.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <PrimaryButton onClick={() => start()} disabled={starting || running}>
          {starting ? "Starting…" : running ? "Installing…" : error ? "Try installing yt-dlp again" : "Install yt-dlp"}
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
            {status.message || "Installing yt-dlp…"} {Math.round(status.progress_pct)}%
            {status.total_bytes ? ` — ${fmtMB(status.downloaded_bytes)} of ${fmtMB(status.total_bytes)}` : ""}
          </p>
          <p className="text-xs text-neutral-500">Keep the app open. About 40 MB on Linux, 22 MB on Windows.</p>
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
