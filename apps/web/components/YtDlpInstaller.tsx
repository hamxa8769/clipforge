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
  ytdlpInstallRunning, getYtDlpInstallStatus, installYtDlp, recheckYtDlp,
  type YtDlpInstallState, type YtDlpInstallStatus,
} from "@/lib/api";
import { PrimaryButton, ProgressBar, SecondaryButton, ExternalLink } from "./ui";

export function useYtDlpInstaller(onInstalled?: () => void) {
  const [status, setStatus] = useState<YtDlpInstallStatus | null>(null);
  // Whether a status check has settled yet — see FfmpegInstaller: a
  // failed check must say "couldn't check", never "missing".
  const [checked, setChecked] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const [recheckError, setRecheckError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onInstalledRef = useRef(onInstalled);
  onInstalledRef.current = onInstalled;
  // Fires onInstalled once when the install flips to done, or when a
  // re-check finds yt-dlp (state stays "idle" then — the blocked stage
  // still has to clear). Kept in an effect (not inside the setStatus
  // updater, which React requires to be pure and may double-invoke)
  // so the re-check runs exactly once per transition.
  const prevStateRef = useRef<YtDlpInstallState | null>(null);
  const prevFoundRef = useRef<boolean | null>(null);
  useEffect(() => {
    if (status?.state === "done" && prevStateRef.current !== "done") onInstalledRef.current?.();
    else if (status?.found === true && prevFoundRef.current !== true) onInstalledRef.current?.();
    prevStateRef.current = status?.state ?? null;
    prevFoundRef.current = status?.found ?? null;
  }, [status]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const refresh = useCallback(async () => {
    const next = await getYtDlpInstallStatus();
    setChecked(true);
    if (next) {
      setStatus(next);
      if (!ytdlpInstallRunning(next)) stopPolling();
    }
    return next;
  }, [stopPolling]);

  // Re-check (v0.9.1): the user installed yt-dlp themselves (PowerShell
  // / pip / winget) and the app still says "missing". Re-probes every
  // known location with no app restart — on Windows the server first
  // re-reads the registry PATH, so an install made after startup is
  // picked up. Returns the fresh status (null when unreachable).
  const recheck = useCallback(async () => {
    setRechecking(true);
    setRecheckError(null);
    const next = await recheckYtDlp();
    setRechecking(false);
    if (!next) {
      setRecheckError("Could not reach the app to check again. Try again.");
      return null;
    }
    setStatus(next);
    return next;
  }, []);

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

  return { status, checked, starting, startError, start, refresh, recheck, rechecking, recheckError };
}

function fmtMB(bytes: number): string {
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

/** Full installer block: status line + button + progress + error + retry.
 *  Used on the blocked Ingest stage (Pipeline), the Agent page, and in
 *  Settings → This app. */
export function YtDlpInstallSection({ onInstalled }: { onInstalled?: () => void }) {
  const { status, checked, starting, startError, start, refresh, recheck, rechecking, recheckError } = useYtDlpInstaller(onInstalled);
  const running = ytdlpInstallRunning(status);
  // "Ready" means yt-dlp is usable — `found` is the authority, not the
  // install state: on a machine where yt-dlp is already on PATH the
  // installer never ran, so state is "idle" while found is true.
  const done = status?.found === true;
  const broken = status?.broken === true;
  const error = status?.state === "error" || status?.state === "unsupported" ? status.error : null;

  // Settled states first — see FfmpegInstallSection.
  if (!checked) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-neutral-400">Checking yt-dlp…</p>
      </div>
    );
  }
  if (status === null) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-amber-300" role="alert">
          Couldn&apos;t check yt-dlp right now — the server didn&apos;t answer in time. Nothing is installed or removed.
        </p>
        <SecondaryButton onClick={() => { refresh().catch(() => {}); }}>Try again</SecondaryButton>
      </div>
    );
  }

  if (done) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-emerald-300" role="status">
          yt-dlp is installed and ready — YouTube ingest will work now.
          {status?.version ? <span className="text-neutral-500"> (version {status.version})</span> : null}
          {status?.source ? <span className="block text-xs text-neutral-500">Found in: {status.source}</span> : null}
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
        <div className="flex flex-wrap items-center gap-2">
          <SecondaryButton onClick={() => start(true)} disabled={starting || running || rechecking}>
            {running ? "Reinstalling…" : "Reinstall yt-dlp"}
          </SecondaryButton>
          <SecondaryButton onClick={() => { recheck().catch(() => {}); }} disabled={starting || running || rechecking}>
            {rechecking ? "Checking…" : "Re-check"}
          </SecondaryButton>
        </div>
        {recheckError && (
          <p className="text-sm text-red-300" role="alert">{recheckError}</p>
        )}
      </div>
    );
  }

  if (broken) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-amber-300" role="alert">
          {status?.broken_reason || "yt-dlp was found but does not run."}
          {status?.path ? <span className="block truncate font-mono text-xs text-neutral-500">{status.path}</span> : null}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <PrimaryButton onClick={() => start(true)} disabled={starting || running || rechecking}>
            {starting ? "Starting…" : running ? "Installing…" : "Reinstall yt-dlp"}
          </PrimaryButton>
          <SecondaryButton onClick={() => { recheck().catch(() => {}); }} disabled={starting || running || rechecking}>
            {rechecking ? "Checking…" : "Re-check"}
          </SecondaryButton>
        </div>
        {recheckError && (
          <p className="text-sm text-red-300" role="alert">{recheckError}</p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-sm text-neutral-300">
        yt-dlp is missing — ClipForge needs it to download YouTube sources. One click installs the official
        release binary into the app&apos;s data folder and adds it to your Windows PATH. No restart needed.
      </p>
      <p className="text-xs text-neutral-500">
        Already installed it yourself (PowerShell, pip or winget)? Press Re-check — the app looks again
        without restarting.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <PrimaryButton onClick={() => start()} disabled={starting || running || rechecking}>
          {starting ? "Starting…" : running ? "Installing…" : error ? "Try installing yt-dlp again" : "Install yt-dlp"}
        </PrimaryButton>
        <SecondaryButton onClick={() => { recheck().catch(() => {}); }} disabled={starting || running || rechecking}>
          {rechecking ? "Checking…" : "Re-check"}
        </SecondaryButton>
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
      {(error || startError || recheckError) && (
        <p className="text-sm text-red-300" role="alert">
          {error || startError || recheckError}
          {status?.state === "unsupported" ? "" : " Nothing was half-installed — try again."}
        </p>
      )}
    </div>
  );
}
