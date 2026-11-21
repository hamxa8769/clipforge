// In-app Logs drawer (v0.4.0) — server logs inside the main window, never
// a second window. Source order:
//   1. Desktop app: the Tauri get_server_logs command (sidecar log tail).
//   2. Otherwise: the latest job's run log (GET /jobs then /jobs/{id}/logs).
//   3. Neither available: an honest empty state, not a blank panel.
"use client";
import { useCallback, useEffect, useState } from "react";
import { getJobLogs, getJobs, type JobLogEntry } from "@/lib/api";
import { getServerLogs } from "@/lib/tauri";
import { SecondaryButton, Skeleton } from "./ui";

export default function LogsDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [source, setSource] = useState<"server" | "job" | "none" | null>(null);
  const [serverLogs, setServerLogs] = useState<string | null>(null);
  const [jobLogs, setJobLogs] = useState<JobLogEntry[] | null>(null);
  const [jobTitle, setJobTitle] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setSource(null);
    setServerLogs(null);
    setJobLogs(null);
    const tauri = await getServerLogs(120);
    if (tauri !== null) {
      setServerLogs(tauri);
      setSource("server");
      setLoading(false);
      return;
    }
    const jobs = await getJobs();
    const latest = jobs && jobs.length > 0 ? [...jobs].sort((a, b) => b.created_at.localeCompare(a.created_at))[0] : null;
    if (latest) {
      const logs = await getJobLogs(latest.id);
      setJobLogs(logs ?? []);
      setJobTitle(latest.title || latest.id);
      setSource("job");
    } else {
      setSource("none");
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (open) load().catch(() => { setSource("none"); setLoading(false); });
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/60" onClick={onClose} role="dialog" aria-label="Logs">
      <div
        className="flex h-full w-full max-w-xl flex-col border-l border-neutral-800 bg-neutral-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold">Logs</h2>
          <span className="text-xs text-neutral-500">
            {source === "server" ? "Server log (desktop app)" : source === "job" ? `Latest job: ${jobTitle}` : ""}
          </span>
          <span className="ml-auto flex gap-2">
            <SecondaryButton onClick={() => load()}>Refresh</SecondaryButton>
            <SecondaryButton onClick={onClose}>Close</SecondaryButton>
          </span>
        </div>

        <div className="mt-4 min-h-0 flex-1 overflow-auto rounded-lg bg-neutral-950 p-4">
          {loading ? (
            <div className="space-y-2"><Skeleton className="h-4" /><Skeleton className="h-4" /><Skeleton className="h-4" /></div>
          ) : source === "server" ? (
            <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed text-neutral-300">{serverLogs || "No log lines yet."}</pre>
          ) : source === "job" ? (
            (jobLogs ?? []).length === 0 ? (
              <p className="text-sm text-neutral-400">The latest job has not run yet, so there is nothing in its log.</p>
            ) : (
              <ul className="space-y-2">
                {jobLogs!.map((l, i) => (
                  <li key={i} className="font-mono text-xs leading-relaxed">
                    <span className={l.level === "error" ? "text-red-300" : "text-neutral-500"}>[{l.stage}]</span>{" "}
                    <span className="text-neutral-300">{l.message}</span>
                    {l.ts && <span className="ml-2 text-neutral-600">{new Date(l.ts).toLocaleTimeString()}</span>}
                  </li>
                ))}
              </ul>
            )
          ) : (
            <p className="text-sm text-neutral-400">
              No logs available right now. In the desktop app this panel shows the server log; otherwise it shows the latest job&apos;s run log. Run a job first, or open the desktop app.
            </p>
          )}
        </div>
        <p className="mt-3 text-xs text-neutral-500">Logs stay in this window — ClipForge never opens a second window for them.</p>
      </div>
    </div>
  );
}
