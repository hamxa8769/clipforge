// New clip run (v0.9.0) — the dashboard's one-place start: paste a video
// URL, pick a niche, and go. Three real paths, no dummy buttons:
//
// 1. "Check licence & clip this video" — verifies the licence first
//    (GET /api/videos/license) and only then starts a clip job
//    (POST /jobs). A standard-licence video that is not yours is refused
//    with the server's plain-language reason; CC / public-domain / own
//    videos start, and CC-BY attribution is added automatically.
// 2. "Start clip job — my own video" (v0.9.1) — when the rights box is
//    ticked, the job starts straight from the own-content declaration
//    (POST /jobs with own_content_declaration=true), with NO licence
//    check in the way. The check needs a YouTube connection or API key,
//    so without this path an own video could never start on a fresh
//    install: the check 422d and the Start button never appeared. The
//    server still enforces the gate — this only exposes its existing
//    own-content path; a non-declared, non-licensed source still 422s.
// 3. "Run the agent on this source" — starts the agent on this URL +
//    niche via startAgentOnSource (PUT /api/agent/config with
//    enabled:true, then POST /api/agent/start), exactly the Agent
//    page's toggle flow, surfaced here so the daily run does not need
//    a tab hop. Needs the rights checkbox (or a passed licence check)
//    first.
//
// Everything is disabled with the reason shown when the API is
// unreachable — a run that cannot start is never presented as working.
"use client";
import { useState } from "react";
import Link from "next/link";
import {
  checkVideoLicense, createYoutubeClipJob, jobSourceLicense, licenseBadgeLabel,
  startAgentOnSource, NICHE_PRESETS,
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
  // Own video, no licence check needed: the server accepts the
  // own-content declaration on POST /jobs as-is (v0.9.1). The URL must
  // still look like a YouTube link — anything else goes through the
  // licence check, which validates the URL shape.
  const canStartOwnJob = ownContent && /youtu\.?be/.test(url.trim());

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
    if (err || !found) {
      // No YouTube connection / API key is the common dead-end on a
      // fresh install: say the own-video way out in the same message
      // instead of leaving the user with a check that can never pass.
      const hint = err && /no youtube connection|api key/i.test(err)
        ? " If this is your own video, tick 'This is my own video' above and press 'Start clip job — my own video' instead — that needs no licence check or YouTube connection."
        : "";
      setError((err ?? "Licence check failed.") + hint);
      return;
    }
    setCheck(found);
  }

  async function onStartJob() {
    // Two ways in, both server-enforced: a passed licence check, or
    // the own-content declaration (no check needed for your own video).
    if (!check?.can_clip && !canStartOwnJob) return;
    setStarting(true);
    setError(null);
    const res = check?.can_clip
      ? await createYoutubeClipJob({
          source_url: check.url || url.trim(),
          title: check.title,
          source_license: jobSourceLicense(check),
          source_title: check.title,
          source_channel: check.channel,
          own_content_declaration: ownContent || check.license === "own",
        })
      : await createYoutubeClipJob({
          source_url: url.trim(),
          title: "",
          source_license: "own",
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
      rights_confirmed: rightsOk,
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
        hint="Paste a video, check its licence, start clipping — all from here."
        action={<Link href="/trending" className="text-sm font-medium text-amber-300 hover:text-amber-200">Find a licensed video →</Link>}
      />
      {!live && (
        <p className="mb-4 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-200" role="note">{offlineReason}</p>
      )}
      <div className="grid gap-4 lg:grid-cols-[1fr_220px]">
        <Field label="Source video or channel URL" hint="Your own video, a Creative Commons video, or a public-domain video. Standard-licence videos that aren't yours are refused.">
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
          <span className="block text-xs text-neutral-500">Tick this for your own uploads. For Creative Commons videos the licence check below confirms the rights instead.</span>
        </span>
      </label>

      {/* Licence verdict — never clip on a guess. */}
      {check && (
        <div className={`mt-4 rounded-lg border px-3 py-2.5 text-sm ${check.can_clip ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" : "border-red-500/40 bg-red-500/10 text-red-200"}`} role="status">
          <p className="font-medium">
            {check.can_clip ? "✓ Can be clipped — " : "✕ Cannot be clipped — "}
            {licenseBadgeLabel(check.license)} · {check.title || "Untitled video"}
            {check.channel ? ` · by ${check.channel}` : ""}
          </p>
          <p className="mt-0.5 text-xs leading-snug opacity-90">{check.reason}</p>
          {check.can_clip && check.license === "cc" && (
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
        {check?.can_clip ? (
          <PrimaryButton onClick={() => void onStartJob()} disabled={!live || starting}>
            {starting ? <Spinner label="Starting…" /> : `Start clip job — ${licenseBadgeLabel(check.license)}`}
          </PrimaryButton>
        ) : canStartOwnJob ? (
          <PrimaryButton onClick={() => void onStartJob()} disabled={!live || starting}>
            {starting ? <Spinner label="Starting…" /> : "Start clip job — my own video"}
          </PrimaryButton>
        ) : (
          <PrimaryButton onClick={() => void onCheckLicence()} disabled={!live || checking}>
            {checking ? <Spinner label="Checking licence…" /> : "Check licence & clip this video"}
          </PrimaryButton>
        )}
        <SecondaryButton
          onClick={() => void onStartAgent()}
          disabled={!live || startingAgent || !rightsOk || !url.trim() || !nicheValue}
        >
          {startingAgent ? "Starting agent…" : "Run the agent on this source"}
        </SecondaryButton>
        {!rightsOk && live && (
          <span className="text-xs text-neutral-500">Agent run needs the rights checkbox (or a passed licence check) first.</span>
        )}
      </div>
      <p className="mt-3 text-xs leading-snug text-neutral-500">
        Licensed clips only — ClipForge clips your own videos, Creative Commons and public-domain videos. Nothing from a standard-licence video that isn&apos;t yours is ever cut or posted.
      </p>
    </Card>
  );
}
