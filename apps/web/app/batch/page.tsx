// Batch Mode (v0.8.0) — clip many videos in one run.
//
// Paste or drop up to 20 video URLs or file paths, start the batch
// (POST /batch), then watch each video's state and progress live
// (GET /batch/{id}, polled every 3s while the batch runs). Pause and
// resume the whole batch from here. The clips-ready count is the number
// of finished videos that produced clips.
//
// The active performance profile (GET /perf/profile) is shown at the
// top: on a Low-end profile the batch processes 1 video at a time.
//
// Rights guardrail: only your own videos or Creative Commons / licensed /
// public-domain videos. Batch mode never re-uploads another creator's video.
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  apiGet, apiPostJson, getBatch, getPerfProfile, pauseBatch, resumeBatch,
  type BatchStatus, type PerfProfile,
} from "@/lib/api";
import {
  batchStartError, buildBatchPayload, extractBatchItemErrors, isBatchTerminal,
} from "@/lib/batch";
import {
  Badge, Card, CardHeader, EmptyState, ErrorBanner, PageHeader,
  PrimaryButton, ProgressBar, SecondaryButton, Skeleton,
} from "@/components/ui";

const GUARDRAIL =
  "Batch mode clips only your own videos or Creative Commons / licensed / public-domain videos — never another creator's videos.";

const MAX_SOURCES = 20;
const DONE_STATES = ["done", "finished", "completed", "ready"];
const RUNNING_STATES = ["running", "processing", "transcribing", "scoring", "rendering"];

export default function BatchPage() {
  const [input, setInput] = useState("");
  const [sources, setSources] = useState<string[]>([]);
  // Rights guardrail (fixed): the server only accepts YouTube URLs when
  // own_content_declaration is true, so the user must confirm it here
  // before a batch can start.
  const [rights, setRights] = useState(false);
  const [batch, setBatch] = useState<BatchStatus | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);
  // Per-item server errors (keyed by source) — the normalised batch
  // status drops `error`, so they are read from the raw response.
  const [itemErrors, setItemErrors] = useState<Map<string, string>>(new Map());
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState<PerfProfile | null>(null);
  const [profileLoaded, setProfileLoaded] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getPerfProfile().then((p) => { setProfile(p); setProfileLoaded(true); }).catch(() => setProfileLoaded(true));
  }, []);

  function parseInput(text: string): string[] {
    return text.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
  }

  function addSources(text: string) {
    const parsed = parseInput(text);
    const merged = [...sources];
    for (const s of parsed) {
      if (merged.length >= MAX_SOURCES) { setError(`Only ${MAX_SOURCES} videos per batch — the rest were ignored.`); break; }
      if (!merged.includes(s)) merged.push(s);
    }
    setSources(merged);
    setInput("");
  }

  async function onStart() {
    if (sources.length === 0) { setError("Add at least one video URL or path first."); return; }
    if (!rights) { setError("Please tick the rights box first — ClipForge only clips your own videos or Creative Commons / public-domain videos."); return; }
    setStarting(true);
    setError(null);
    setNotice(null);
    // POST /batch expects plain string sources + own_content_declaration.
    // (The old code sent [{url} | {path}] objects, which the server
    // rejects with 422 — every batch start from the UI failed.)
    const res = await apiPostJson("/batch", buildBatchPayload(sources, rights));
    setStarting(false);
    const batch_id = (res.data as { batch_id?: unknown } | null)?.batch_id;
    if (!res.ok || typeof batch_id !== "string") {
      setError(batchStartError(res) || "Could not start the batch. The batch service may not be installed in this build yet.");
      return;
    }
    setBatch(null);
    setItemErrors(new Map());
    setBatchId(batch_id);
    setNotice(`Batch started — ${sources.length} video${sources.length === 1 ? "" : "s"} queued.`);
    const first = await getBatch(batch_id);
    if (first) setBatch(first);
  }

  const poll = useCallback(async () => {
    if (!batchId) return;
    const next = await getBatch(batchId);
    if (next) setBatch(next);
    const raw = await apiGet<unknown>(`/batch/${encodeURIComponent(batchId)}`);
    if (raw) setItemErrors(extractBatchItemErrors(raw));
  }, [batchId]);

  const running = batch !== null && RUNNING_STATES.includes(batch.status.toLowerCase());
  const paused = batch?.status.toLowerCase() === "paused";
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => { poll().catch(() => {}); }, 3000);
    return () => window.clearInterval(t);
  }, [running, poll]);

  async function onPauseResume() {
    if (!batchId) return;
    setBusy(true);
    const res = paused ? await resumeBatch(batchId) : await pauseBatch(batchId);
    setBusy(false);
    if (!res.ok) { setNotice(res.error ?? (paused ? "Could not resume the batch." : "Could not pause the batch.")); return; }
    setNotice(paused ? "Batch resumed." : "Batch paused — running videos finish their current step, then stop.");
    await poll();
  }

  const items = batch?.items ?? [];
  const clipsReady = items.reduce((n, it) => n + (DONE_STATES.includes(it.state.toLowerCase()) ? it.clips : 0), 0);
  const doneCount = items.filter((it) => DONE_STATES.includes(it.state.toLowerCase())).length;
  const failedCount = items.filter((it) => it.state.toLowerCase() === "failed").length;
  const lowEnd = profile?.profile === "low";

  return (
    <div className="space-y-6">
      <PageHeader
        title="Batch Mode"
        intro={`Clip up to ${MAX_SOURCES} videos in one run — paste URLs or file paths, start, and watch each video finish.`}
        actions={<Badge tone="info">Pro · v0.8.0</Badge>}
      />
      <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-100" role="note">{GUARDRAIL}</div>
      {error && <ErrorBanner message={error} />}
      {notice && <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>}

      <Card className="glass">
        <CardHeader title="Performance profile" hint="How hard the batch pushes this machine" />
        {!profileLoaded ? (
          <Skeleton className="h-10" />
        ) : profile ? (
          <div className="space-y-1 text-sm">
            <p>
              Active profile: <span className="font-semibold capitalize text-neutral-100">{profile.profile}</span>
              {profile.override && <span className="ml-2 text-neutral-400">(manual override)</span>}
            </p>
            {profile.reason && <p className="text-neutral-400">{profile.reason}</p>}
            {lowEnd && (
              <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-amber-100" role="note">
                Low-end mode: processing 1 video at a time, with the smallest transcription model and low-resolution previews, so the machine stays responsive.
              </p>
            )}
            <p className="text-xs text-neutral-500">
              {profile.cpu_cores !== null && <>CPU: {profile.cpu_cores} cores · </>}
              {profile.ram_gb !== null && <>RAM: {profile.ram_gb} GB · </>}
              {profile.gpu ? <>GPU: {profile.gpu}</> : "No GPU detected"}
              {" · "}Change the profile in <a className="text-amber-300 underline" href="/settings">Settings → Performance</a>.
            </p>
          </div>
        ) : (
          <p className="text-sm text-neutral-400">Performance profile not available in this build yet — the batch will run with the server&apos;s defaults.</p>
        )}
      </Card>

      <Card className="glass">
        <CardHeader title="Add videos" hint={`Up to ${MAX_SOURCES} per batch`} />
        <label className="block text-sm">
          <span className="text-neutral-400">Video URLs or file paths — one per line, or comma-separated</span>
          <textarea
            rows={4}
            className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={"https://www.youtube.com/watch?v=… (your own video)\nC:\\Videos\\my-video.mp4"}
            aria-label="Video URLs or paths"
          />
        </label>
        <div className="mt-3 flex flex-wrap gap-2">
          <SecondaryButton onClick={() => addSources(input)} disabled={!input.trim()}>Add to batch</SecondaryButton>
          <SecondaryButton onClick={() => fileRef.current?.click()}>Choose a file list</SecondaryButton>
          <input
            ref={fileRef}
            type="file"
            accept=".txt,.csv"
            className="hidden"
            aria-label="Choose a text file with video URLs"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              const text = await f.text();
              addSources(text);
              e.target.value = "";
            }}
          />
          {sources.length > 0 && <SecondaryButton onClick={() => { setSources([]); setBatch(null); setBatchId(null); }}>Clear all</SecondaryButton>}
        </div>
        {sources.length > 0 && (
          <div className="mt-4">
            <p className="text-sm font-medium text-neutral-100">{sources.length} video{sources.length === 1 ? "" : "s"} in this batch</p>
            <ul className="mt-2 space-y-1">
              {sources.map((s, i) => (
                <li key={i} className="flex items-center gap-2 rounded-lg bg-neutral-950 px-3 py-2 text-sm">
                  <span className="min-w-0 flex-1 truncate font-mono text-xs">{s}</span>
                  <button className="shrink-0 text-neutral-400 hover:text-neutral-100" onClick={() => setSources((prev) => prev.filter((_, j) => j !== i))} aria-label={`Remove ${s}`}>✕</button>
                </li>
              ))}
            </ul>
          </div>
        )}
        <label className="mt-4 flex items-start gap-2 text-sm">
          <input type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} className="mt-0.5 h-4 w-4 accent-amber-400" />
          <span>
            These videos are my own, or Creative Commons / public-domain videos I may use.
            CC-BY attribution is added to clips automatically. The server refuses
            YouTube URLs without this confirmation.
          </span>
        </label>
        <div className="mt-4">
          <PrimaryButton
            onClick={onStart}
            disabled={starting || sources.length === 0 || !rights || (batchId !== null && !(batch !== null && isBatchTerminal(batch.status)))}
          >
            {starting ? "Starting batch…" : batchId !== null && !(batch !== null && isBatchTerminal(batch.status)) ? "Batch running — see progress below" : batch !== null && isBatchTerminal(batch.status) ? `Start a new batch (${sources.length} video${sources.length === 1 ? "" : "s"})` : `Start batch (${sources.length} video${sources.length === 1 ? "" : "s"})`}
          </PrimaryButton>
          {sources.length > 0 && !rights && <span className="ml-3 text-xs text-amber-200">Tick the rights box above to start.</span>}
          {lowEnd && <span className="ml-3 text-xs text-neutral-400">Low-end mode: 1 at a time.</span>}
        </div>
      </Card>

      {batchId && (
        <Card className="glass">
          <CardHeader
            title={`Batch ${batchId}`}
            hint={batch ? `Status: ${batch.status}` : "Loading…"}
            action={
              (running || paused) ? (
                <SecondaryButton onClick={onPauseResume} disabled={busy}>{busy ? "Working…" : paused ? "Resume batch" : "Pause batch"}</SecondaryButton>
              ) : undefined
            }
          />
          {items.length === 0 ? (
            <p className="text-sm text-neutral-400">Waiting for the first progress update…</p>
          ) : (
            <>
              <p className="mb-3 text-sm text-neutral-300">
                {doneCount} of {items.length} done · {failedCount} failed · <span className="font-semibold text-neutral-100">{clipsReady} clip{clipsReady === 1 ? "" : "s"} ready</span>
              </p>
              <ul className="space-y-3">
                {items.map((it, i) => (
                  <li key={i} className="rounded-lg border border-neutral-800 p-3">
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="min-w-0 flex-1 truncate font-mono text-xs">{it.source}</span>
                      <span className="rounded-full bg-neutral-700/50 px-2 py-0.5 text-xs font-medium text-neutral-200">{it.state}</span>
                      <span className="text-xs text-neutral-400">{it.clips} clip{it.clips === 1 ? "" : "s"}</span>
                    </div>
                    <div className="mt-2"><ProgressBar value={it.progress} tone={it.state.toLowerCase() === "failed" ? "danger" : DONE_STATES.includes(it.state.toLowerCase()) ? "success" : "default"} /></div>
                    <p className="mt-1 text-xs text-neutral-500">{Math.round(it.progress)}%</p>
                    {it.state.toLowerCase() === "failed" && itemErrors.get(it.source) && (
                      <p className="mt-1 text-xs text-red-300">{itemErrors.get(it.source)}</p>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </Card>
      )}

      {!batchId && (
        <EmptyState
          title="No batch running"
          body="Add your own videos above and start a batch. Each video is transcribed, scored and clipped in turn, and the finished clips land in the approval queue."
        />
      )}
    </div>
  );
}
