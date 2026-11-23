// New clip run (v0.9.0) — the dashboard's one-place start: paste a video
// URL, pick a niche, and go. Two real paths, no dummy buttons:
//
// 1. "Check licence & clip this video" — reports the licence first
//    (GET /api/videos/license) and then starts a clip job (POST /jobs).
//    Advisory since v0.9.1: the licence check never refuses a video —
//    any pasted link proceeds, the rights notice is shown instead, and
//    CC-BY attribution is added automatically for CC videos.
// 2. "Run the agent on this source" — saves this URL + niche as the
//    agent's source (PUT /api/agent/config) and starts the agent
//    (POST /api/agent/start), exactly the Agent page's flow, surfaced
//    here so the daily run does not need a tab hop. Needs the rights
//    checkbox (or a licence check) first.
//
// Everything is disabled with the reason shown when the API is
// unreachable — a run that cannot start is never presented as working.
"use client";
import { useState } from "react";
import Link from "next/link";
import {
  checkVideoLicense, createYoutubeClipJob, jobSourceLicense, licenseBadgeLabel,
  RIGHTS_ADVISORY,
  saveAgentConfig, startAgent, NICHE_PRESETS,
  type LicenseCheck,
} from "@/lib/api";
import { Card, CardHeader, Field, PrimaryButton, SecondaryButton, Select, Spinner, TextInput } from "./ui";

function isChannelUrl(url: string): boolean {
  return /youtube\.com\/(channel\/|c\/|user\/|@)/i.test(url);
}

export default function NewClipRun({ live, onJobStarted }: { live: boolean; onJobStarted?: () => void }) {
  const [url, setUrl] = useState("");
  const [niche, setNiche] = useState("");
  const [customNiche, setCustomNiche] = useState("");
  const [ownContent, setOwnContent] = useState(false);
  const [checking, setChecking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startingAgent, setStartingAgent] = useState(false);
  const [check, setCheck] = useState<LicenseCheck | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [startedJobId, setStartedJobId] = useState<string | null>(null);
  const [agentStarted, setAgentStarted] = useState(false);

  const nicheValue = niche === "__custom" ? customNiche.trim() : niche;
  const rightsOk = ownContent || (check?.can_clip ?? false);
  const offlineReason = "The server is unreachable — clip runs start when the API is back.";

  async function onCheckLicence() {
    const trimmed = url.trim();
    if (!trimmed) { setError("Paste a YouTube video URL first."); return; }
    if (!/youtu\.?be/.test(trimmed)) { setError("That does not look like a YouTube URL — paste a youtube.com or youtu.be link."); return; }
    setChecking(true);
    setError(null);
    setCheck(null);
    setStartedJobId(null);
    setAgentStarted(false);
    const { check: found, error: err } = await checkVideoLicense(trimmed);
    setChecking(false);
    if (err || !found) { setError(err ?? "Licence check failed."); return; }
    setCheck(found);
  }

  async function onStartJob() {
    // Advisory since v0.9.1: the licence check never refuses a video —
    // any checked link proceeds; the rights notice is shown instead.
    if (!check) return;
    setStarting(true);
    setError(null);
    const res = await createYoutubeClipJob({
      source_url: check.url || url.trim(),
      title: check.title,
      source_license: jobSourceLicense(check),
      source_title: check.title,
      source_channel: check.channel,
      own_content_declaration: ownContent || check.license === "own",
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
    const saved = await saveAgentConfig({
      source_type: isChannelUrl(sourceUrl) ? "channel" : "video",
      source_url: sourceUrl,
      niche: nicheValue,
      rights_confirmed: rightsOk,
    });
    if (!saved.ok) {
      setStartingAgent(false);
      setError(saved.error ?? "Could not save the agent source.");
      return;
    }
    const started = await startAgent();
    setStartingAgent(false);
    if (!started.ok) { setError(started.error ?? "Could not start the agent."); return; }
    setAgentStarted(true);
    onJobStarted?.();
  }

  return (
    <Card className="card-hover">
      <CardHeader
        title="New clip run"
        hint="Paste a video, check its licence, start clipping — all from here."
        action={<Link href="/trending" className="text-sm font-medium text-amber-300 hover:text-amber-200">Find a licensed video →</Link>}
      />
      <p className="mb-4 rounded-lg bg-neutral-800/60 px-3 py-2 text-sm text-neutral-300" role="note">{RIGHTS_ADVISORY}</p>
      {!live && (
        <p className="mb-4 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-200" role="note">{offlineReason}</p>
      )}
      <div className="grid gap-4 lg:grid-cols-[1fr_220px]">
        <Field label="Source video or channel URL" hint="Paste any YouTube video link — the licence check below reports its licence, and the rights notice above applies.">
          <TextInput
            id="new-clip-url"
            type="url"
            value={url}
            onChange={(e) => { setUrl(e.target.value); setCheck(null); setStartedJobId(null); setAgentStarted(false); }}
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

      <label className="mt-4 flex cursor-pointer items-start gap-2.5 text-sm text-neutral-200">
        <input
          type="checkbox"
          checked={ownContent}
          onChange={(e) => setOwnContent(e.target.checked)}
          disabled={!live}
          className="mt-0.5 h-4 w-4 shrink-0 accent-amber-400"
        />
        <span>
          This is my own video (or I have the rights to use it)
          <span className="block text-xs text-neutral-500">Tick this for your own uploads. For other videos the licence check below reports the video&apos;s licence instead.</span>
        </span>
      </label>

      {/* Licence report — advisory since v0.9.1: the licence is reported,
          never a refusal. Any checked video can be clipped. */}
      {check && (
        <div className="mt-4 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2.5 text-sm text-emerald-200" role="status">
          <p className="font-medium">
            {"✓ Can be clipped — "}
            {licenseBadgeLabel(check.license)} · {check.title || "Untitled video"}
            {check.channel ? ` · by ${check.channel}` : ""}
          </p>
          <p className="mt-0.5 text-xs leading-snug opacity-90">{check.reason}</p>
          {check.license === "cc" && (
            <p className="mt-0.5 text-xs leading-snug opacity-90">CC-BY attribution will be added to the clip description automatically.</p>
          )}
        </div>
      )}
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
        {!check ? (
          <PrimaryButton onClick={() => void onCheckLicence()} disabled={!live || checking}>
            {checking ? <Spinner label="Checking licence…" /> : "Check licence & clip this video"}
          </PrimaryButton>
        ) : (
          <PrimaryButton onClick={() => void onStartJob()} disabled={!live || starting}>
            {starting ? <Spinner label="Starting…" /> : `Start clip job — ${licenseBadgeLabel(check.license)}`}
          </PrimaryButton>
        )}
        <SecondaryButton
          onClick={() => void onStartAgent()}
          disabled={!live || startingAgent || !rightsOk || !url.trim() || !nicheValue}
        >
          {startingAgent ? "Starting agent…" : "Run the agent on this source"}
        </SecondaryButton>
        {!rightsOk && live && (
          <span className="text-xs text-neutral-500">Agent run needs the rights checkbox (or a licence check) first.</span>
        )}
      </div>
      <p className="mt-3 text-xs leading-snug text-neutral-500">
        {RIGHTS_ADVISORY} ClipForge does not guarantee rights clearance.
      </p>
    </Card>
  );
}
