// New clip run (v0.11.0) — the dashboard's one-place start: paste a
// video URL, pick a niche, and go. Two real paths, no dummy buttons:
//
// 1. "Start clip job" — starts a clip job straight from the pasted URL
//    (POST /jobs). v0.11.0 removed the licence-check step: no
//    GET /api/videos/license call, no rights checkbox, no licence
//    report. The job is sent with own_content_declaration=true so the
//    server's ingest accepts the YouTube URL (ingest still requires
//    the declaration server-side; the UI no longer asks for it — the
//    user is responsible for the rights to what they clip, stated in
//    the release notes, not in the UI).
// 2. "Run the agent on this source" — starts the agent on this URL +
//    niche via startAgentOnSource (PUT /api/agent/config with
//    enabled:true, then POST /api/agent/start), exactly the Agent
//    page's toggle flow, surfaced here so the daily run does not need
//    a tab hop.
//
// Everything is disabled with the reason shown when the API is
// unreachable — a run that cannot start is never presented as working.
"use client";
import { useState } from "react";
import {
  createYoutubeClipJob,
  startAgentOnSource, NICHE_PRESETS,
} from "@/lib/api";
import { Card, CardHeader, Field, PrimaryButton, SecondaryButton, Select, Spinner, TextInput } from "./ui";

function isChannelUrl(url: string): boolean {
  return /youtube\.com\/(channel\/|c\/|user\/|@)/i.test(url);
}

export default function NewClipRun({ live, onJobStarted }: { live: boolean; onJobStarted?: () => void }) {
  const [url, setUrl] = useState("");
  const [niche, setNiche] = useState("");
  const [customNiche, setCustomNiche] = useState("");
  const [starting, setStarting] = useState(false);
  const [startingAgent, setStartingAgent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [startedJobId, setStartedJobId] = useState<string | null>(null);
  const [agentStarted, setAgentStarted] = useState(false);

  const nicheValue = niche === "__custom" ? customNiche.trim() : niche;
  const offlineReason = "The server is not answering — clip runs start when the API is back.";

  async function onStartJob() {
    const trimmed = url.trim();
    if (!trimmed) { setError("Paste a YouTube video URL first."); return; }
    if (!/youtu\.?be/.test(trimmed)) { setError("That does not look like a YouTube URL — paste a youtube.com or youtu.be link."); return; }
    setStarting(true);
    setError(null);
    // No licence is recorded for the source — none was checked.
    const res = await createYoutubeClipJob({
      source_url: trimmed,
      title: "",
      source_license: "unknown",
      own_content_declaration: true,
    });
    setStarting(false);
    if (!res.ok) { setError(res.error ?? "Could not start the clip job."); return; }
    const data = (res.data ?? {}) as Record<string, unknown>;
    const jobId = typeof data.id === "string" ? data.id : null;
    setStartedJobId(jobId);
    onJobStarted?.();
  }

  async function onStartAgent() {
    const sourceUrl = url.trim();
    if (!sourceUrl) { setError("Paste a source video or channel URL first."); return; }
    if (!nicheValue) { setError("Pick a niche first — the agent uses it to pick topics and titles."); return; }
    setStartingAgent(true);
    setError(null);
    setAgentStarted(false);
    // One helper owns the save-then-start sequence: it sends
    // enabled:true with the config (without it POST /api/agent/start
    // 422s "agent is off" — the v0.9.0 dashboard bug where pasting any
    // link started nothing) and rolls the toggle back off if the
    // start itself fails.
    const started = await startAgentOnSource({
      source_type: isChannelUrl(sourceUrl) ? "channel" : "video",
      source_url: sourceUrl,
      niche: nicheValue,
      rights_confirmed: true,
    });
    setStartingAgent(false);
    if (!started.ok) { setError(started.error ?? "Could not start the agent."); return; }
    setAgentStarted(true);
    onJobStarted?.();
  }

  return (
    <Card className="card-hover">
      <CardHeader
        title="New clip run"
        hint="Paste a video, start clipping — all from here."
      />
      {!live && (
        <p className="mb-4 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-200" role="note">{offlineReason}</p>
      )}
      <div className="grid gap-4 lg:grid-cols-[1fr_220px]">
        <Field label="Source video or channel URL" hint="Paste any YouTube video link.">
          <TextInput
            id="new-clip-url"
            type="url"
            value={url}
            onChange={(e) => { setUrl(e.target.value); setStartedJobId(null); setAgentStarted(false); }}
            placeholder="https://www.youtube.com/watch?v=…"
            disabled={!live}
            aria-label="Source video or channel URL"
          />
        </Field>
        <Field label="Niche" hint="Used by the agent run to pick topics and titles.">
          <Select value={niche} onChange={(e) => setNiche(e.target.value)} disabled={!live} aria-label="Niche">
            <option value="">Pick a niche…</option>
            {NICHE_PRESETS.map((n) => (
              <option key={n} value={n} className="capitalize">{n}</option>
            ))}
            <option value="__custom">Custom…</option>
          </Select>
          {niche === "__custom" && (
            <TextInput
              className="mt-2"
              value={customNiche}
              onChange={(e) => setCustomNiche(e.target.value)}
              placeholder="Your niche"
              disabled={!live}
              aria-label="Custom niche"
            />
          )}
        </Field>
      </div>

      {startedJobId && (
        <p className="mt-4 rounded-lg bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200" role="status">
          Clip job {startedJobId} started — cutting clips now. It will appear below and in the approval queue when ready.
        </p>
      )}
      {agentStarted && (
        <p className="mt-4 rounded-lg bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200" role="status">
          Agent started on this source{nicheValue ? ` in the “${nicheValue}” niche` : ""} — clips land in the approval queue when ready.
        </p>
      )}
      {error && (
        <p className="mt-4 rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-200" role="alert">{error}</p>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <PrimaryButton onClick={() => void onStartJob()} disabled={!live || starting || !url.trim()}>
          {starting ? <Spinner label="Starting…" /> : "Start clip job"}
        </PrimaryButton>
        <SecondaryButton
          onClick={() => void onStartAgent()}
          disabled={!live || startingAgent || !url.trim() || !nicheValue}
        >
          {startingAgent ? "Starting agent…" : "Run the agent on this source"}
        </SecondaryButton>
      </div>
    </Card>
  );
}
