// Requirements panel (v0.9.2) — the complete pre-flight check, in one
// place: yt-dlp, FFmpeg, ffprobe and Whisper, each verified by actually
// running it (GET /requirements/status). A binary that exists but does
// not run shows as "Broken" with its path, never as installed — the
// v0.9.1 report was a job failing mid-download on a yt-dlp the app
// thought was fine.
//
// "Verify requirements" re-runs the whole check live (POST
// /requirements/recheck); on Windows the server first merges the
// registry PATH, so a tool installed by hand after startup (PowerShell
// / pip / winget) is picked up with no restart. Each missing/broken
// requirement offers its one-click fix inline (the same installer
// sections Settings uses), so the fix never drifts from the status.
//
// Used on the Dashboard (above the clip run, so a blocked requirement
// is seen before a job starts) and in Settings → This app.
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  getRequirementsStatus, recheckRequirements,
  type RequirementItem, type RequirementsStatus,
} from "@/lib/api";
import { useFfmpegInstaller } from "./FfmpegInstaller";
import { useYtDlpInstaller } from "./YtDlpInstaller";
import { PrimaryButton, SecondaryButton } from "./ui";

const STATUS_LABEL: Record<RequirementItem["status"], string> = {
  ok: "Installed",
  missing: "Missing",
  broken: "Broken",
  degraded: "Needs download",
};

const STATUS_DOT: Record<RequirementItem["status"], string> = {
  ok: "bg-emerald-400",
  missing: "bg-amber-400",
  broken: "bg-red-400",
  degraded: "bg-amber-400",
};

function shortVersion(version: string): string {
  // "ffmpeg version 8.1.2 Copyright (c) ..." -> "8.1.2"
  const m = version.match(/(\d+(?:\.\d+)+)/);
  return m ? m[1] : version;
}

export function RequirementsPanel() {
  const [status, setStatus] = useState<RequirementsStatus | null>(null);
  // Whether a check attempt has settled yet. "Checking…" is honest
  // only before the first attempt; after a failed attempt the panel
  // says it couldn't check instead of hanging on "Checking…" forever
  // (v0.9.2: GET /requirements/status runs the tool probes and can
  // outlast the fetch timeout while the server is live).
  const [checked, setChecked] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const next = await getRequirementsStatus();
    setChecked(true);
    if (next) {
      setStatus(next);
      setError(null);
    } else {
      setError("Could not reach the app to check requirements. Try again.");
    }
    return next;
  }, []);

  // After a one-click install finishes, the requirement flips to
  // Installed — re-run the check so the panel agrees with the server.
  const ffmpeg = useFfmpegInstaller(() => { refresh().catch(() => {}); });
  const ytdlp = useYtDlpInstaller(() => { refresh().catch(() => {}); });

  useEffect(() => {
    refresh().catch(() => {});
  }, [refresh]);

  const recheck = useCallback(async () => {
    setChecking(true);
    setError(null);
    const next = await recheckRequirements();
    setChecking(false);
    if (!next) {
      setError("Could not reach the app to check again. Try again.");
      return;
    }
    setStatus(next);
  }, []);

  const ffmpegInstalling = ffmpeg.status !== null &&
    ["downloading", "verifying", "extracting", "installing"].includes(ffmpeg.status.state);
  const ytdlpInstalling = ytdlp.status !== null &&
    ["downloading", "verifying", "installing"].includes(ytdlp.status.state);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-neutral-300">
          {status === null
            ? checked
              ? "Couldn't check requirements — the server didn't answer in time. Press “Verify requirements” to try again."
              : "Checking what the pipeline needs…"
            : status.all_ready
              ? "All requirements installed — jobs can run."
              : `${status.blocking.length} requirement${status.blocking.length === 1 ? "" : "s"} need${status.blocking.length === 1 ? "s" : ""} attention — a job cannot start until ${status.blocking.length === 1 ? "it is" : "they are"} fixed.`}
        </p>
        <SecondaryButton onClick={() => { recheck().catch(() => {}); }} disabled={checking}>
          {checking ? "Checking…" : "Verify requirements"}
        </SecondaryButton>
      </div>

      {error && (
        <p className="text-sm text-red-300" role="alert">{error}</p>
      )}

      {status && (
        <ul className="space-y-2">
          {status.requirements.map((req) => (
            <li key={req.id} className="rounded-lg bg-neutral-950 px-3 py-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[req.status]}`} aria-hidden="true" />
                <span className="text-sm font-medium text-neutral-100">{req.label}</span>
                <span className="text-sm text-neutral-400">
                  {STATUS_LABEL[req.status]}
                  {req.version ? ` · ${req.id === "ffmpeg" || req.id === "ffprobe" ? shortVersion(req.version) : req.version}` : ""}
                </span>
                {req.source ? (
                  <span className="text-xs text-neutral-500">({req.source})</span>
                ) : null}
                <span className="ml-auto flex flex-wrap items-center gap-2">
                  {req.fix_action === "install-yt-dlp" && (
                    <PrimaryButton
                      onClick={() => { ytdlp.start(req.status === "broken").catch(() => {}); }}
                      disabled={ytdlp.starting || ytdlpInstalling || checking}
                    >
                      {ytdlpInstalling
                        ? `Installing… ${Math.round(ytdlp.status?.progress_pct ?? 0)}%`
                        : ytdlp.starting ? "Starting…" : req.fix_label || "Install yt-dlp"}
                    </PrimaryButton>
                  )}
                  {req.fix_action === "install-ffmpeg" && (
                    <PrimaryButton
                      onClick={() => { ffmpeg.start(req.status === "broken").catch(() => {}); }}
                      disabled={ffmpeg.starting || ffmpegInstalling || checking}
                    >
                      {ffmpegInstalling
                        ? `Installing… ${Math.round(ffmpeg.status?.progress_pct ?? 0)}%`
                        : ffmpeg.starting ? "Starting…" : req.fix_label || "Install FFmpeg"}
                    </PrimaryButton>
                  )}
                  {req.fix_action === "settings" && (
                    <PrimaryButton href="/settings">
                      {req.fix_label || "Open Settings"}
                    </PrimaryButton>
                  )}
                </span>
              </div>
              <p className="mt-1 text-xs text-neutral-400">{req.detail}</p>
              {req.path ? (
                <p className="mt-0.5 truncate font-mono text-xs text-neutral-500">{req.path}</p>
              ) : null}
              {(req.id === "yt-dlp" && ytdlp.startError) || (req.id === "ffmpeg" && ffmpeg.startError) ? (
                <p className="mt-1 text-sm text-red-300" role="alert">
                  {req.id === "yt-dlp" ? ytdlp.startError : ffmpeg.startError}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {status?.checked_at ? (
        <p className="text-xs text-neutral-500">
          Last checked {new Date(status.checked_at).toLocaleTimeString()}.
          Jobs are re-checked before they start — a missing or broken tool blocks the run with this same message instead of failing mid-download.
        </p>
      ) : null}
    </div>
  );
}
