// Single-window status bar (v0.4.0) — Server / FFmpeg / Dry-run pills in
// the main layout, plus the in-app Logs drawer trigger. Everything stays
// in the main window: there is no separate "shell" window to open.
//
// Data comes from the existing GET /health and GET /settings (masked) —
// no new endpoints. The port is the API's own port: the desktop app
// serves the UI from the API server, so window.location.port is the
// server port; in dev it comes from the configured API URL.
"use client";
import { useCallback, useEffect, useState } from "react";
import { API_URL, getHealth, getSettings, type AppSettingsView, type Health } from "@/lib/api";
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
  const [health, setHealth] = useState<Health | null | undefined>(undefined);
  const [settings, setSettings] = useState<AppSettingsView | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);

  const load = useCallback(async () => {
    const [h, s] = await Promise.all([getHealth(), getSettings()]);
    setHealth(h);
    setSettings(s);
  }, []);

  useEffect(() => {
    let cancelled = false;
    load().catch(() => { if (!cancelled) setHealth(null); });
    const t = setInterval(() => { load().catch(() => {}); }, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, [load]);

  const port = portLabel();
  const serverUp = health !== null && health !== undefined;
  const ffmpeg = settings?.ffmpeg;
  const dryRun = health?.dry_run ?? true;

  return (
    <>
      <div className="border-b border-neutral-800 bg-neutral-950">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-2 px-4 py-2">
          {health === undefined ? (
            <span className="text-xs text-neutral-500">Checking server status…</span>
          ) : (
            <>
              <Pill ok={serverUp} label="Server" value={serverUp ? `running${port ? ` · port ${port}` : ""}` : "unreachable — start the server"} />
              {settings ? (
                <Pill ok={ffmpeg?.found === true} label="FFmpeg" value={ffmpeg?.found ? "found" : "missing — clipping needs it"} />
              ) : (
                <Pill warn label="FFmpeg" value={serverUp ? "checking…" : "unknown"} />
              )}
              <Pill ok={!dryRun} warn={dryRun} label="Dry-run" value={dryRun ? "on — nothing publishes" : "off"} />
              {health?.version && <span className="text-xs text-neutral-500">v{health.version}</span>}
            </>
          )}
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
