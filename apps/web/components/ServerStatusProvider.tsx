// Server-status provider (v0.9.3) — the ONE poller of GET /health.
//
// Mounted once in the root layout. It owns the only /health polling
// in the app: an immediate probe on mount, every LIVE_POLL_MS while
// live, and exponential-backoff retries (reconnectDelayMs) while down.
// Every component and page reads liveness from useServerStatus() and
// never fetches /health itself, so the header, the status bar, the
// dashboard, Settings and every other page can no longer disagree
// about whether the server is up.
//
// When the server comes back, pages recover on their own: they watch
// `live` and re-load their data when it flips to true — no manual
// reload, no "Start the server, then reload this page".
"use client";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { getHealth, type Health } from "@/lib/api";
import { getServerStatus, isTauri, retryServer } from "@/lib/tauri";
import {
  formatLastSeenAge, INITIAL_SERVER_STATUS, LIVE_POLL_MS, reconnectDelayMs, reduceServerStatus,
  serverStatusLabel, type ServerStatus, type ServerStatusSnapshot,
} from "@/lib/server-status";

export interface ServerStatusValue extends ServerStatusSnapshot {
  /** True only when the last /health probe succeeded. */
  live: boolean;
  /** True only before the first probe settles. */
  checking: boolean;
  /** True once a probe has failed and retries are running. */
  reconnecting: boolean;
  /** Probe right now instead of waiting for the next scheduled retry. */
  retryNow: () => void;
}

const ServerStatusContext = createContext<ServerStatusValue | null>(null);

/** Read the shared server status. Outside a provider (should not
 *  happen — the layout mounts one) this falls back to "checking"
 *  with a no-op retry, so a page never crashes for lack of context. */
export function useServerStatus(): ServerStatusValue {
  const ctx = useContext(ServerStatusContext);
  if (ctx) return ctx;
  return {
    ...INITIAL_SERVER_STATUS,
    live: false,
    checking: true,
    reconnecting: false,
    retryNow: () => {},
  };
}

export function ServerStatusProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<ServerStatusSnapshot>(INITIAL_SERVER_STATUS);
  // Bumps to force an immediate probe (the Retry now button).
  const [retryNonce, setRetryNonce] = useState(0);
  const retryNonceRef = useRef(0);
  // Mirror of the snapshot for the timer loop, so the next delay is
  // always computed from the fresh state (live → fixed interval,
  // down → exponential backoff) without stale closures. This ref
  // lives for the whole provider lifetime — a Retry now re-runs the
  // effect, and the attempt count / last-seen time must survive it.
  const snapshotRef = useRef<ServerStatusSnapshot>(INITIAL_SERVER_STATUS);

  const retryNow = useCallback(() => {
    retryNonceRef.current += 1;
    setRetryNonce(retryNonceRef.current);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const apply = (next: ServerStatusSnapshot) => {
      snapshotRef.current = next;
      setSnapshot(next);
    };

    const probe = async () => {
      let health: Health | null = null;
      try {
        health = await getHealth();
      } catch {
        health = null;
      }
      if (cancelled) return;
      apply(reduceServerStatus(snapshotRef.current, { ok: health !== null, health, now: Date.now() }));
    };

    const loop = async () => {
      await probe();
      if (cancelled) return;
      const s = snapshotRef.current;
      timer = setTimeout(() => { void loop(); }, s.status === "live" ? LIVE_POLL_MS : reconnectDelayMs(s.attempts));
    };

    void loop();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // Re-run the loop when the user presses Retry now: the effect
    // re-probes immediately instead of waiting out the backoff.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [retryNonce]);

  const status: ServerStatus = snapshot.status;
  const value: ServerStatusValue = {
    ...snapshot,
    live: status === "live",
    checking: status === "checking",
    reconnecting: status === "reconnecting",
    retryNow,
  };

  return <ServerStatusContext.Provider value={value}>{children}</ServerStatusContext.Provider>;
}

/**
 * Restart button for a dead server (P0-2). Only renders inside the
 * desktop app — in a plain browser there is no shell to restart the
 * server, so nothing is shown. Calls the Rust `retry_server` command
 * (safe on a merely slow server: the shell re-waits on a live child
 * instead of killing it), then polls `get_server_status` so the
 * button can show the shell's own words ("Starting the ClipForge
 * server…") while the shared /health poll confirms the server is
 * actually back. A refused restart says so plainly, with the
 * fallback of closing and reopening the app.
 */
export function RestartServerButton({ className = "" }: { className?: string }) {
  const [state, setState] = useState<"idle" | "starting" | "failed">("idle");
  const [detail, setDetail] = useState<string | null>(null);
  const pollsRef = useRef(0);

  useEffect(() => {
    if (state !== "starting") return;
    pollsRef.current = 0;
    const t = setInterval(() => {
      pollsRef.current += 1;
      if (pollsRef.current > 10) {
        clearInterval(t);
        return;
      }
      getServerStatus().then((s) => {
        if (!s) return;
        if (s.phase === "starting") setDetail(s.detail ?? "Starting the server…");
        else if (s.phase === "failed") {
          setState("failed");
          setDetail(s.error ?? "The server failed to start.");
          clearInterval(t);
        }
      }).catch(() => {});
    }, 1000);
    return () => clearInterval(t);
  }, [state]);

  // No shell, no restart button — a plain browser tab cannot restart
  // a server it does not own.
  if (!isTauri()) return null;

  async function restart() {
    setState("starting");
    setDetail("Asking the app to restart the server…");
    const ok = await retryServer();
    if (!ok) {
      setState("failed");
      setDetail("The restart didn't start — try closing and reopening ClipForge.");
      return;
    }
    setDetail("Restart requested — waiting for the server…");
  }

  if (state === "failed") {
    return (
      <span className={`inline-flex flex-wrap items-center gap-2 ${className}`}>
        <span className="text-sm text-red-300" role="alert">{detail}</span>
        <button
          onClick={() => { setState("idle"); setDetail(null); void restart(); }}
          className="shrink-0 rounded-md bg-neutral-800 px-3 py-1.5 text-sm font-medium text-neutral-200 hover:bg-neutral-700"
        >
          Try again
        </button>
      </span>
    );
  }

  return (
    <span className={`inline-flex flex-wrap items-center gap-2 ${className}`}>
      <button
        onClick={() => { void restart(); }}
        disabled={state === "starting"}
        className="shrink-0 rounded-md bg-amber-400 px-3 py-1.5 text-sm font-semibold text-neutral-950 hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {state === "starting" ? "Restarting…" : "Restart server"}
      </button>
      {state === "starting" && detail && <span className="text-sm text-neutral-300">{detail}</span>}
    </span>
  );
}

/**
 * Reconnecting notice, shared by every page that has to say "the
 * server is down". States the attempt count and that the app keeps
 * trying on its own — never "reload this page". `variant="inline"`
 * is the slim strip for pages that still show their last live data;
 * the default card is for pages with nothing live to show yet.
 */
export function ReconnectingNotice({ variant = "card" }: { variant?: "card" | "inline" }) {
  const { attempts, lastLiveAt, retryNow } = useServerStatus();
  // "Server not answering", not "Server unreachable": the UI cannot
  // tell a dead server from a slow/busy one (a threadpool-starved
  // server stops answering /health while the process still lives), so
  // it must not declare the server dead — only that it is not
  // answering, when it was last seen, and what happens next.
  const lastSeen = formatLastSeenAge(lastLiveAt, Date.now());
  const body = (
    <>
      <p className="flex-1 text-sm text-amber-200">
        <span className="font-medium">{serverStatusLabel("reconnecting", attempts)}.</span>{" "}
        The app keeps trying on its own — no need to reload.{" "}
        {lastSeen ? `Last connected ${lastSeen}. ` : ""}
        Check that ClipForge is running, then wait a moment.
      </p>
      <button
        onClick={retryNow}
        className="shrink-0 rounded-md bg-amber-400 px-3 py-1.5 text-sm font-semibold text-neutral-950 hover:bg-amber-300"
      >
        Retry now
      </button>
      <RestartServerButton />
    </>
  );
  if (variant === "inline") {
    return (
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3" role="status">
        {body}
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-5" role="status">
      <p className="text-base font-semibold text-amber-100">Server not answering</p>
      <div className="mt-2 flex flex-wrap items-center gap-3">{body}</div>
    </div>
  );
}
