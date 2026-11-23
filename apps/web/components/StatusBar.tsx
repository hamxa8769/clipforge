// Single-window status bar (v0.4.0) — Server / FFmpeg / Dry-run pills in
// the main layout, plus the in-app Logs drawer trigger. Everything stays
// in the main window: there is no separate "shell" window to open.
//
// Liveness comes from the shared useServerStatus() hook (v0.9.3) — this
// component no longer polls /health itself.
//
// THE STUCK "FFmpeg: checking…" (v0.9.2 screenshot): the pill read
// FFmpeg from GET /settings, and rendered "checking…" whenever the
// settings fetch had not returned — with no distinction between "not
// asked yet" and "asked and failed". GET /settings runs keyring reads
// plus ffmpeg/yt-dlp probes and routinely outlasts the client's
// timeout while /health answers fine, so the pill sat on "checking…"
// forever on a live server. Now the pill reads GET /ffmpeg/status
// (the installer state, with found/version from a verified probe),
// with a long timeout for the probe, and three settled states:
// checking (before the first attempt settles), found/missing, and
// "couldn't check" once an attempt has failed. While the server is
// down it says "unknown — waiting for server" instead of pretending
// to check. No pill in this bar can hang forever.
"use client";
import { useCallback, useEffect, useState } from "react";
import { API_URL, getFfmpegInstallStatus, getPerfProfile, type FfmpegInstallStatus, type PerfProfile } from "@/lib/api";
import { serverStatusLabel } from "@/lib/server-status";
import { RestartServerButton, useServerStatus } from "./ServerStatusProvider";
import LogsDrawer from "./LogsDrawer";

function portLabel(): string {
  try {
    if (API_URL) return new URL(API_URL).port || "";
  } catch { /* relative API URL in the desktop build */ }
  if (typeof window !== "undefined") return window.location.port;
  return "";
}

function Pill({ ok, warn, label, value }: { ok?: boolean; warn?: boolean; label: string; value: string }) {
  const dot = ok ? "bg-emerald-400" : warn ? "bg-amber-400" : "bg-red-400";
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-neutral-800 px-2.5 py-1 text-xs text-neutral-300">
      <span className={`h-2 w-2 rounded-full ${dot}`} aria-hidden="true" />
      <span className="text-neutral-500">{label}:</span> {value}
    </span>
  );
}

export default function StatusBar() {
  const server = useServerStatus();
  // FFmpeg probe state: undefined = no attempt has settled yet
  // ("checking…" is honest only here); null = an attempt failed;
  // a value = the server answered.
  const [ffmpeg, setFfmpeg] = useState<FfmpegInstallStatus | null | undefined>(undefined);
  const [perf, setPerf] = useState<PerfProfile | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);

  const loadFfmpeg = useCallback(async () => {
    const next = await getFfmpegInstallStatus();
    // A failed probe settles to null (never back to "checking…") and
    // the pill says "couldn't check" until a later probe succeeds.
    setFfmpeg(next);
  }, []);

  // Probe FFmpeg only while the server is live, and re-probe when it
  // comes back — polling a down server would just burn the backoff.
  useEffect(() => {
    if (!server.live) return;
    let cancelled = false;
    loadFfmpeg().catch(() => { if (!cancelled) setFfmpeg(null); });
    const t = setInterval(() => { loadFfmpeg().catch(() => {}); }, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, [server.live, loadFfmpeg]);

  // Performance profile: independent of liveness, never blocks the
  // bar — a failed fetch just hides the pill.
  useEffect(() => {
    if (!server.live) return;
    let cancelled = false;
    getPerfProfile().then((p) => { if (!cancelled) setPerf(p); }).catch(() => {});
    const t = setInterval(() => { getPerfProfile().then((p) => { if (!cancelled) setPerf(p); }).catch(() => {}); }, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, [server.live]);

  const port = portLabel();
  const health = server.health;
  const dryRun = health?.dry_run ?? true;

  // FFmpeg pill text: settles one way or another, never "checking…"
  // past the first attempt.
  const ffmpegPill = (() => {
    if (!server.live) return { ok: false, warn: true, value: "unknown — waiting for server" };
    if (ffmpeg === undefined) return { ok: false, warn: true, value: "checking…" };
    if (ffmpeg === null) return { ok: false, warn: true, value: "couldn't check — retrying" };
    if (ffmpeg.broken) return { ok: false, warn: false, value: "broken — reinstall it" };
    return ffmpeg.found
      ? { ok: true, warn: false, value: "found" }
      : { ok: false, warn: false, value: "missing — clipping needs it" };
  })();

  return (
    <>
      <div className="shrink-0 border-t border-neutral-800 bg-neutral-900">
        <div className="flex flex-wrap items-center gap-2 px-4 py-2">
          {server.checking ? (
            <span className="text-xs text-neutral-500">Checking server status…</span>
          ) : (
            <>
              <Pill
                ok={server.live}
                warn={server.reconnecting}
                label="Server"
                value={server.live ? `running${port ? ` · port ${port}` : ""}` : serverStatusLabel(server.status, server.attempts)}
              />
              <Pill ok={ffmpegPill.ok} warn={ffmpegPill.warn} label="FFmpeg" value={ffmpegPill.value} />
              <Pill
                ok={server.live && !dryRun}
                warn={!server.live || dryRun}
                label="Dry-run"
                value={!server.live ? "unknown" : dryRun ? "on — nothing publishes" : "off"}
              />
              {perf && (
                <Pill ok={perf.profile !== "low"} warn={perf.profile === "low"} label="Performance" value={`${perf.profile}${perf.override ? " (override)" : ""}`} />
              )}
              {health?.version && <span className="text-xs text-neutral-500">v{health.version}</span>}
            </>
          )}
          {server.reconnecting && <RestartServerButton />}
          <button
            onClick={() => setLogsOpen(true)}
            className="ml-auto rounded-md bg-neutral-800 px-3 py-1 text-xs font-medium text-neutral-200 hover:bg-neutral-700"
          >
            Logs
          </button>
        </div>
      </div>
      <LogsDrawer open={logsOpen} onClose={() => setLogsOpen(false)} />
    </>
  );
}
