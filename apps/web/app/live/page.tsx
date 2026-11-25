// Live monitoring page (v0.10.0, Worker A) — watch a live stream,
// spot the peak moments, send each peak to the normal clip pipeline.
//
//   - Start: paste a YouTube / Twitch / Kick live URL -> the server
//     resolves the live edge with yt-dlp and records it with ffmpeg
//     (6s buffer segments + a live audio pipe), exactly as the API
//     documents it. A URL that is not live fails 422 in plain
//     language — the page never shows a session that records nothing.
//   - Peaks: audio-energy and chat-velocity jumps (>=60% above the
//     trailing 3-minute baseline, chat counted as people not
//     messages) plus "clip it" / custom trigger phrases in the
//     rolling transcript. Each peak is cut (12s before, 28s after
//     the trigger) and becomes a normal pipeline job — poll the
//     session for its job id and open it in the pipeline / queue.
//   - Honest states: with no YouTube connection, YouTube chat is
//     shown as unavailable (an API key alone cannot read live chat);
//     Kick is audio-only (no public chat API without auth); without
//     faster-whisper there is no rolling transcript and no keyword
//     triggers — peaks still fire from audio + chat, and the page
//     says so. A replay is always labelled a replay, never a live
//     stream.
//
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  clipNowLive,
  getLiveMonitor,
  getLivePlatforms,
  getLiveStatus,
  listLiveMonitors,
  startLiveMonitor,
  stopLiveMonitor,
  type LivePeak,
  type LivePlatform,
  type LiveSession,
} from "@/lib/api";
import {
  fmtElapsed,
  fmtPeakWindow,
  isLiveSessionActive,
  liveChatLabel,
  liveSessionState,
} from "@/lib/live";
import {
  Card, CardHeader, EmptyState, ErrorBanner, PrimaryButton,
  SecondaryButton, Skeleton, Badge,
} from "@/components/ui";
import { useServerStatus } from "@/components/ServerStatusProvider";

function LevelStrip({ level }: { level: number[] }) {
  if (!level.length) return <p className="text-sm text-neutral-500">No audio yet — levels appear once the stream is recording.</p>;
  const min = -60;
  const max = 0;
  return (
    <div className="flex h-10 items-end gap-px" aria-label="Audio level, last 60 seconds">
      {level.map((v, i) => {
        const h = Math.max(4, Math.min(100, ((v - min) / (max - min)) * 100));
        const hot = v > -12;
        return <span key={i} className={`w-full rounded-sm ${hot ? "bg-amber-400" : "bg-neutral-600"}`} style={{ height: `${h}%` }} title={`${v} dB`} />;
      })}
    </div>
  );
}

function PeakRow({ peak }: { peak: LivePeak }) {
  return (
    <li className="flex flex-wrap items-center gap-2 border-b border-neutral-800 py-2 last:border-0">
      <Badge tone={peak.job_id ? "success" : peak.error ? "danger" : "neutral"}>
        {peak.kind === "keyword" ? "keyword" : peak.kind === "manual" ? "manual" : "peak"}
      </Badge>
      <span className="text-sm font-medium text-neutral-100">{fmtPeakWindow(peak)}</span>
      <span className="text-sm text-neutral-400">{peak.reasons.join(" · ")}</span>
      <span className="text-sm text-neutral-500">score {peak.score}</span>
      {peak.job_id ? (
        <Link href={`/jobs/${peak.job_id}`} className="ml-auto text-sm font-medium text-amber-400 hover:underline">
          Open job →
        </Link>
      ) : peak.error ? (
        <span className="ml-auto text-sm text-red-400">{peak.error}</span>
      ) : (
        <span className="ml-auto text-sm text-neutral-500">Cutting…</span>
      )}
    </li>
  );
}

export default function LivePage() {
  const { status: serverStatus } = useServerStatus();
  const serverDown = serverStatus !== "live";
  const [url, setUrl] = useState("");
  const [triggerWords, setTriggerWords] = useState("");
  const [platforms, setPlatforms] = useState<LivePlatform[] | null>(null);
  const [preflight, setPreflight] = useState<Record<string, unknown> | null>(null);
  const [sessions, setSessions] = useState<LiveSession[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [active, setActive] = useState<LiveSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [loading, setLoading] = useState(true);

  const refreshSessions = useCallback(async () => {
    const { sessions: list, error: listErr } = await listLiveMonitors();
    if (listErr) {
      setError(listErr);
      setSessions([]);
    } else {
      setSessions(list ?? []);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [{ platforms: plats }, { status }] = await Promise.all([getLivePlatforms(), getLiveStatus()]);
      if (cancelled) return;
      setPlatforms(plats);
      setPreflight(status);
      await refreshSessions();
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [refreshSessions]);

  // Poll the active session while it monitors; stop polling when it ends.
  useEffect(() => {
    if (!activeId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const poll = async () => {
      const { session, error: err } = await getLiveMonitor(activeId);
      if (cancelled) return;
      if (err || !session) {
        setError(err ?? "The monitor session could not be loaded.");
        return;
      }
      setActive(session);
      if (!isLiveSessionActive(session)) {
        if (timer) clearInterval(timer);
        void refreshSessions();
      }
    };
    void poll();
    timer = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [activeId, refreshSessions]);

  const start = useCallback(async () => {
    setError(null);
    setStarting(true);
    const words = triggerWords.split(",").map((w) => w.trim()).filter(Boolean);
    const res = await startLiveMonitor(url.trim(), { triggerWords: words });
    setStarting(false);
    if (!res.ok) {
      setError(res.error ?? "Could not start live monitoring.");
      return;
    }
    const data = (res.data ?? {}) as Record<string, unknown>;
    const id = typeof data.session_id === "string" ? data.session_id : "";
    if (!id) {
      setError("The server started monitoring but returned no session id.");
      return;
    }
    setActiveId(id);
    setUrl("");
    void refreshSessions();
  }, [url, triggerWords, refreshSessions]);

  const stop = useCallback(async () => {
    if (!activeId) return;
    const res = await stopLiveMonitor(activeId);
    if (!res.ok) setError(res.error ?? "Could not stop the monitor.");
    const { session } = await getLiveMonitor(activeId);
    if (session) setActive(session);
    void refreshSessions();
  }, [activeId, refreshSessions]);

  const clipNow = useCallback(async () => {
    if (!activeId) return;
    const res = await clipNowLive(activeId, 30);
    if (!res.ok) setError(res.error ?? "Could not clip the last 30 seconds.");
  }, [activeId]);

  const openSession = useCallback(async (id: string) => {
    setActiveId(id);
    const { session, error: err } = await getLiveMonitor(id);
    if (err || !session) setError(err ?? "Could not load the monitor session.");
    else setActive(session);
  }, []);

  const requirements = Array.isArray(preflight?.requirements) ? (preflight?.requirements as Record<string, unknown>[]) : [];
  const blocking = Array.isArray(preflight?.blocking) ? (preflight?.blocking as string[]) : [];
  const youtube = (preflight?.youtube ?? {}) as Record<string, unknown>;
  const activeState = liveSessionState(active);
  const base = (active?.base ?? {}) as Record<string, unknown>;
  const loudDb = typeof base.loud_db === "number" ? `${base.loud_db} dB` : "—";
  const loudJump = typeof base.loud_jump_pct === "number" ? ` · now ${base.loud_jump_pct}%` : "";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-neutral-50">Live monitoring</h1>
        <p className="mt-1 text-sm text-neutral-400">
          Watch a live stream for peak moments — loudness and chat jumps, plus “clip it” in the
          transcript — and send each peak to the normal clip pipeline. Peaks are cut from the
          live edge: a moment that already passed cannot be recovered.
        </p>
      </div>

      {error && <ErrorBanner message={error} onRetry={() => setError(null)} />}
      {serverDown && (
        <ErrorBanner message="The ClipForge server is unreachable — live monitoring needs it. Check the server status and try again." />
      )}

      {loading ? (
        <Skeleton className="h-40" />
      ) : (
        <>
          <Card>
            <CardHeader title="Before you start" hint="Pre-flight + what each platform can do" />
            {blocking.length > 0 ? (
              <p className="text-sm text-red-400">
                Missing: {blocking.join(", ")} — install from Settings before monitoring. Peaks cannot be cut without them.
              </p>
            ) : (
              <p className="text-sm text-neutral-300">Ready: FFmpeg and yt-dlp are installed.</p>
            )}
            <ul className="mt-2 space-y-1 text-sm text-neutral-400">
              {requirements.filter((r) => r.id === "whisper").map((r) => (
                <li key="whisper">Rolling transcription: {String(r.detail ?? "")}</li>
              ))}
              <li>
                YouTube chat: {typeof youtube.chat_note === "string" && youtube.chat_note
                  ? youtube.chat_note
                  : "No YouTube connection — YouTube monitoring still works for audio, but live chat needs the channel connected."}
              </li>
            </ul>
            <div className="mt-4 grid gap-3 md:grid-cols-3">
              {(platforms ?? []).map((p) => (
                <div key={p.platform} className="rounded-lg border border-neutral-800 p-3">
                  <p className="font-semibold capitalize text-neutral-100">{p.platform}</p>
                  <p className="mt-1 text-xs leading-relaxed text-neutral-400">{p.audio_note}</p>
                  <p className="mt-1 text-xs leading-relaxed text-neutral-400">{liveChatLabel(p.chat)}</p>
                </div>
              ))}
            </div>
          </Card>

          <Card>
            <CardHeader title="Monitor a live stream" hint="YouTube, Twitch or Kick" />
            <div className="flex flex-col gap-3 sm:flex-row">
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://www.youtube.com/watch?v=… or twitch.tv/… or kick.com/…"
                disabled={serverDown || starting}
                className="min-w-0 flex-1 rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-500"
                aria-label="Live stream URL"
              />
              <PrimaryButton onClick={start} disabled={serverDown || starting || !url.trim()}>
                {starting ? "Starting…" : "Start monitoring"}
              </PrimaryButton>
            </div>
            <div className="mt-3">
              <label className="text-xs text-neutral-500" htmlFor="live-triggers">
                Extra trigger phrases (optional, comma-separated) — e.g. let&apos;s gooo, insane
              </label>
              <input
                id="live-triggers"
                value={triggerWords}
                onChange={(e) => setTriggerWords(e.target.value)}
                placeholder="let's gooo, insane"
                disabled={serverDown}
                className="mt-1 w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-500"
              />
            </div>
            <p className="mt-2 text-xs text-neutral-500">
              The monitor needs about a minute of audio before it can judge anything. A peak is
              cut 12 seconds before to 28 seconds after the trigger, then queued as a normal job.
            </p>
          </Card>
        </>
      )}

      {active && (
        <Card>
          <CardHeader
            title={`${active.mode === "replay" ? "Replay" : "Live"} — ${active.title || active.url}`}
            hint={`${active.platform} · ${activeState} · ${fmtElapsed(active.elapsed_s)} elapsed`}
            action={
              <div className="flex gap-2">
                {isLiveSessionActive(active) && (
                  <>
                    <SecondaryButton onClick={clipNow}>Clip last 30s</SecondaryButton>
                    <SecondaryButton onClick={stop}>Stop</SecondaryButton>
                  </>
                )}
              </div>
            }
          />
          {active.mode === "replay" && (
            <p className="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
              This is a replay of a recorded file, not a live stream.
            </p>
          )}
          {active.status === "failed" && (
            <p className="mb-3 text-sm text-red-400">{active.error || "Monitoring failed."}</p>
          )}
          <LevelStrip level={active.level} />
          <div className="mt-3 grid gap-2 text-sm text-neutral-400 sm:grid-cols-3">
            <p>
              Baseline: {loudDb}
              {loudJump}
            </p>
            <p>
              Chat: {active.chat_total} messages
              {active.chat_on ? "" : " (chat not connected — audio-only peaks)"}
            </p>
            <p>
              Transcript: {active.words} words
              {active.transcript_ok === false ? " — unavailable, keyword triggers off" : ""}
            </p>
          </div>
          {active.transcript_tail && (
            <p className="mt-3 rounded-lg bg-neutral-800/60 px-3 py-2 text-sm italic text-neutral-300">
              “{active.transcript_tail}”
            </p>
          )}
          {active.recent_chat.length > 0 && (
            <ul className="mt-3 space-y-1 text-sm text-neutral-400">
              {active.recent_chat.slice(-5).map((c, i) => (
                <li key={i}>
                  <span className="font-medium text-neutral-200">{c.user}</span>: {c.msg}
                </li>
              ))}
            </ul>
          )}
          <h3 className="mt-4 font-semibold text-neutral-100">Peaks ({active.peaks.length})</h3>
          {active.peaks.length === 0 ? (
            <p className="mt-1 text-sm text-neutral-500">
              No peaks detected yet — the monitor needs a minute of audio first, and a peak only
              fires when the stream gets at least 60% louder or busier than its recent baseline.
            </p>
          ) : (
            <ul className="mt-1">
              {[...active.peaks].reverse().map((peak) => (
                <PeakRow key={peak.n} peak={peak} />
              ))}
            </ul>
          )}
          {active.missed_peaks > 0 && (
            <p className="mt-2 text-sm text-amber-400">
              {active.missed_peaks} peak(s) could not be cut (their window had left the buffer, or the job could not be created).
            </p>
          )}
        </Card>
      )}

      <Card>
        <CardHeader title="Monitor sessions" hint="Newest first — finished sessions stay visible" />
        {!sessions ? (
          <Skeleton className="h-20" />
        ) : sessions.length === 0 ? (
          <EmptyState
            title="Nothing monitored yet"
            body="Start a live stream above. When no stream is live, the start fails plainly instead of recording silence — nothing is invented here."
          />
        ) : (
          <ul className="divide-y divide-neutral-800">
            {sessions.map((s) => (
              <li key={s.session_id} className="flex flex-wrap items-center gap-2 py-2">
                <Badge tone={s.status === "failed" ? "danger" : s.status === "live" || s.status === "starting" ? "success" : "neutral"}>
                  {s.mode === "replay" ? "replay" : s.status}
                </Badge>
                <button onClick={() => void openSession(s.session_id)} className="min-w-0 flex-1 truncate text-left text-sm font-medium text-neutral-100 hover:underline">
                  {s.title || s.url}
                </button>
                <span className="text-sm text-neutral-500">
                  {fmtElapsed(s.elapsed_s)} · {s.peaks.length} peak(s)
                  {s.status === "failed" && s.error ? ` · ${s.error}` : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
