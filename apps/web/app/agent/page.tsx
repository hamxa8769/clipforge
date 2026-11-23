// Agent page (v0.5.0) — one place to run the ClipForge agent.
//
//   - Big on/off toggle per account/channel (from GET /api/agent/status)
//   - Source picker: one video URL, or a source YouTube channel URL
//   - Niche picker: presets + free text
//   - Auto-post toggle, default OFF, with the approval-first warning
//   - Rights checkbox — the agent cannot be turned on without it
//   - Live status: current job + progress + which engine is active
//     (Gemini / OpenAI / rule-based fallback, stated plainly)
//
// Contract (worker 2 owns the backend engine):
//   GET /api/agent/status, GET/PUT /api/agent/config,
//   POST /api/agent/start | /api/agent/stop, GET /api/llm/providers.
// Until those endpoints land, the page says so plainly and the
// controls that need the engine stay disabled — it never pretends an
// agent is running. The AI providers card lives below (see
// components/LlmProviders.tsx); the niche/source choice is also saved
// to the setup-wizard state so the wizard and this page agree.
"use client";
import { useCallback, useEffect, useState } from "react";
import {
  activeEngineName, getAgentConfig, getAgentStatus, getLlmProviders,
  saveAgentConfig, saveSetupState, startAgent, stopAgent,
  NICHE_PRESETS, type AgentConfig, type AgentStatus, type LlmProvider,
} from "@/lib/api";
import LlmProviders from "@/components/LlmProviders";
import { useServerStatus } from "@/components/ServerStatusProvider";
import { YtDlpInstallSection } from "@/components/YtDlpInstaller";
import {
  Card, CardHeader, DemoBadge, EmptyState, ErrorBanner, PrimaryButton,
  ProgressBar, SecondaryButton, Skeleton, Toggle, platformLabel,
} from "@/components/ui";

const ENGINE_BLURB: Record<string, string> = {
  Gemini: "Gemini picks the best moments and writes the titles.",
  OpenAI: "OpenAI picks the best moments and writes the titles.",
  "AI engine": "Your saved AI key picks the best moments and writes the titles.",
  "rule-based engine": "No AI key is connected, so the built-in rule-based engine picks moments and writes titles from templates. Connect Gemini below for smarter picks.",
};

/** True when a message is the yt-dlp-missing block (start failure or run error). */
function ytDlpMissing(text: string | null | undefined): boolean {
  return !!text && text.toLowerCase().includes("yt-dlp");
}

export default function AgentPage() {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [providers, setProviders] = useState<LlmProvider[] | null>(null);
  const [engineAvailable, setEngineAvailable] = useState(false);
  const [loaded, setLoaded] = useState(false);
  // Liveness is the shared server status (v0.9.3) — no longer "did
  // my own status/config fetch happen to succeed".
  const server = useServerStatus();
  const live = server.live;
  const [saving, setSaving] = useState(false);
  const [acting, setActing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [customNiche, setCustomNiche] = useState("");

  // Form state (mirrors the server config once loaded).
  const [enabled, setEnabled] = useState(false);
  const [sourceType, setSourceType] = useState<"video" | "channel">("video");
  const [sourceUrl, setSourceUrl] = useState("");
  const [niche, setNiche] = useState("");
  const [autoPost, setAutoPost] = useState(false);
  const [rights, setRights] = useState(false);

  const load = useCallback(async () => {
    const [fetchedStatus, fetchedConfig, fetchedProviders] = await Promise.all([
      getAgentStatus(), getAgentConfig(), getLlmProviders(),
    ]);
    setEngineAvailable(fetchedStatus !== null);
    setStatus(fetchedStatus);
    setConfig(fetchedConfig);
    setProviders(fetchedProviders);
    if (fetchedConfig) {
      setEnabled(fetchedConfig.enabled);
      setSourceType(fetchedConfig.source_type);
      setSourceUrl(fetchedConfig.source_url);
      // The backend defaults an unset niche to "general" — in the UI
      // that means "nothing picked yet", not a custom niche.
      const savedNiche = fetchedConfig.niche === "general" ? "" : fetchedConfig.niche;
      setNiche(savedNiche);
      setAutoPost(fetchedConfig.auto_post);
      setRights(fetchedConfig.rights_confirmed);
      if (!NICHE_PRESETS.includes(savedNiche as (typeof NICHE_PRESETS)[number]) && savedNiche) {
        setCustomNiche(savedNiche);
      }
    }
    setLoaded(true);
  }, []);

  useEffect(() => { load().catch(() => setLoaded(true)); }, [load]);

  // When the server comes back after being down, reload on our own.
  useEffect(() => { if (live) load().catch(() => setLoaded(true)); }, [live, load]);

  // Live status refresh while the agent is running.
  useEffect(() => {
    if (!status?.running) return;
    const t = window.setInterval(() => { getAgentStatus().then((s) => { if (s) setStatus(s); }).catch(() => {}); }, 5000);
    return () => window.clearInterval(t);
  }, [status?.running]);

  const engine = activeEngineName(providers);
  const nicheValue = niche === "__custom" ? customNiche.trim() : niche;
  const canEnable = rights && sourceUrl.trim().length > 0 && nicheValue.length > 0;
  const enableBlocker = !rights
    ? "Confirm the rights checkbox first — the agent cannot be turned on without it."
    : !sourceUrl.trim()
      ? "Add a source video or channel URL first."
      : !nicheValue
        ? "Pick a niche first."
        : null;

  async function save(patch: Partial<AgentConfig>) {
    setSaving(true);
    setNotice(null);
    const res = await saveAgentConfig(patch);
    setSaving(false);
    if (!res.ok) {
      setNotice(res.error ?? "Could not save the agent settings.");
      return false;
    }
    // Keep the setup wizard in sync (niche + source live there too).
    if (patch.niche !== undefined || patch.source_type !== undefined || patch.source_url !== undefined) {
      saveSetupState({
        niche: patch.niche ?? nicheValue,
        source_type: patch.source_type ?? sourceType,
        source_url: patch.source_url ?? sourceUrl,
        steps: { niche_source: true },
      }).catch(() => {});
    }
    await load();
    return true;
  }

  async function onToggleAgent(next: boolean) {
    if (next && !canEnable) { setNotice(enableBlocker); return; }
    setActing(true);
    setNotice(null);
    const saved = await save({
      enabled: next, source_type: sourceType, source_url: sourceUrl.trim(),
      niche: nicheValue, auto_post: autoPost, rights_confirmed: rights,
    });
    if (!saved) { setActing(false); return; }
    const res = next ? await startAgent() : await stopAgent();
    setActing(false);
    if (!res.ok) {
      // The agent could not be turned (on|off): never leave the switch
      // showing a state the server did not reach. When starting failed
      // (e.g. yt-dlp missing) roll the toggle back OFF so it shows the
      // truth instead of a stuck ON that silently does nothing.
      if (next) {
        await save({ enabled: false });
      }
      setNotice(res.error ?? (next ? "Could not start the agent." : "Could not stop the agent."));
      await load();
      return;
    }
    setNotice(next ? "Agent is on — it will work through the source you picked." : "Agent is off.");
    await load();
  }

  if (!loaded) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" /><Skeleton className="h-48" /><Skeleton className="h-64" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Agent</h1>
        <p className="mt-1 text-sm text-neutral-400">
          The agent watches your source, makes clips, and — only if you allow it — posts them.
          Approval-first is the default: nothing posts without your approval unless you turn auto-post on.
        </p>
      </div>

      {!engineAvailable && (
        <ErrorBanner message="The agent engine is not available in this build yet — the controls below are shown so you can see how it works, but starting the agent needs the agent service (coming in v0.5.0). Your niche and source choices are still saved." />
      )}
      {!live && engineAvailable && <DemoBadge />}
      {notice && (
        <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>
      )}
      {ytDlpMissing(notice) && (
        <Card>
          <CardHeader title="Install yt-dlp" hint="One click — no restart needed" />
          <YtDlpInstallSection onInstalled={() => { load().catch(() => {}); }} />
          <p className="mt-3 text-sm text-neutral-400">
            After the install finishes, turn the agent on again above — it will pick up your saved source and niche.
          </p>
        </Card>
      )}

      <Card>
        <CardHeader title="Agent status" hint="Live from the agent service" />
        {!engineAvailable ? (
          <EmptyState
            title="Agent service not installed yet"
            body="The Agent page is ready, but the agent engine itself has not landed in this build. Once it does, this card shows whether the agent is running, its current job, and progress per account."
          />
        ) : status ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${status.enabled ? "bg-emerald-500/15 text-emerald-300" : "bg-neutral-700/50 text-neutral-300"}`}>
                {status.enabled ? "Enabled" : "Disabled"}
              </span>
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${status.running ? "bg-amber-500/15 text-amber-300" : status.state === "error" ? "bg-red-500/15 text-red-300" : status.state === "done" ? "bg-emerald-500/15 text-emerald-300" : "bg-neutral-700/50 text-neutral-300"}`}>
                {status.running ? "Running" : status.state === "error" ? "Error" : status.state === "done" ? "Done" : status.state === "stopped" ? "Stopped" : "Idle"}
              </span>
              <span className="text-sm text-neutral-300">
                Engine: <span className="font-medium text-neutral-100">{engine}</span>
              </span>
            </div>
            <p className="text-sm text-neutral-400">{ENGINE_BLURB[engine]}</p>
            {status.running && (
              <div>
                <ProgressBar value={status.progress_pct} />
                <p className="mt-1 text-xs text-neutral-400">{Math.round(status.progress_pct)}% — working through your source…</p>
              </div>
            )}
            {status.state === "error" && status.last_error && (
              <div role="alert" className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-200">
                The agent could not run: {status.last_error}
              </div>
            )}
            {status.state === "error" && ytDlpMissing(status.last_error) && (
              <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-3">
                <YtDlpInstallSection onInstalled={() => { load().catch(() => {}); }} />
                <p className="mt-3 text-sm text-neutral-400">
                  After the install finishes, turn the agent on again — it will pick up your saved source and niche.
                </p>
              </div>
            )}
            {status.state === "done" && (
              <p className="text-sm text-emerald-200">
                Last run finished — {status.queued} clip{status.queued === 1 ? "" : "s"} queued for approval
                {status.approved > 0 ? `, ${status.approved} approved` : ""}
                {status.published > 0 ? `, ${status.published} published` : ""}.
              </p>
            )}
            {status.state === "stopped" && (
              <p className="text-sm text-neutral-300">The agent was stopped. Turn it on again to run it.</p>
            )}
            {status.current_job && (
              <p className="text-sm text-neutral-300">Current job: <span className="font-medium">{status.current_job}</span></p>
            )}
            {status.accounts.length === 0 ? (
              <p className="text-sm text-neutral-400">No accounts connected yet — connect a channel first, then the agent can post to it.</p>
            ) : (
              <ul className="space-y-3">
                {status.accounts.map((a) => (
                  <li key={`${a.platform}-${a.account}`} className="rounded-lg border border-neutral-800 p-4">
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="font-semibold">{platformLabel(a.platform)}</span>
                      <span className="text-sm text-neutral-300">{a.account}</span>
                      <span className={`ml-auto rounded-full px-2 py-0.5 text-xs font-medium ${a.status === "running" ? "bg-amber-500/15 text-amber-300" : a.status === "error" ? "bg-red-500/15 text-red-300" : "bg-neutral-700/50 text-neutral-300"}`}>{a.status || "idle"}</span>
                    </div>
                    <ProgressBar value={a.progress_pct} className="mt-3" />
                    <p className="mt-2 text-xs text-neutral-400">
                      {a.progress_pct}% · {a.queued} queued · {a.approved} approved · {a.published} published
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="text-sm text-neutral-400">Could not read the agent status.</p>
        )}
      </Card>

      <Card>
        <CardHeader title="Turn the agent on" hint="Approval-first by default" />
        <div className="space-y-5">
          <Toggle
            checked={enabled}
            onChange={(v) => onToggleAgent(v)}
            disabled={!engineAvailable || acting || saving}
            label="Agent on/off"
            hint={engineAvailable
              ? "When on, the agent works through your source and queues clips for approval."
              : "Disabled until the agent service is installed in this build."}
          />
          {enabled && enableBlocker && (
            <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">{enableBlocker}</p>
          )}

          <fieldset>
            <legend className="text-sm font-medium text-neutral-100">Source</legend>
            <p className="mt-0.5 text-xs text-neutral-400">One video to clip again and again, or a whole channel to watch for new videos.</p>
            <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Source type">
              {(["video", "channel"] as const).map((t) => (
                <button
                  key={t}
                  type="button"
                  role="radio"
                  aria-checked={sourceType === t}
                  onClick={() => setSourceType(t)}
                  className={`rounded-md border px-3 py-1.5 text-sm ${sourceType === t ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}
                >
                  {t === "video" ? "One video URL" : "Source YouTube channel URL"}
                </button>
              ))}
            </div>
            <input
              className="mt-2 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm"
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
              placeholder={sourceType === "video" ? "https://www.youtube.com/watch?v=…" : "https://www.youtube.com/@yourchannel"}
              aria-label={sourceType === "video" ? "Source video URL" : "Source channel URL"}
            />
          </fieldset>

          <fieldset>
            <legend className="text-sm font-medium text-neutral-100">Niche</legend>
            <p className="mt-0.5 text-xs text-neutral-400">What the clips are about — the agent picks moments and titles to match.</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {NICHE_PRESETS.map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setNiche(n)}
                  aria-pressed={niche === n}
                  className={`rounded-full border px-3 py-1 text-sm capitalize ${niche === n ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}
                >
                  {n}
                </button>
              ))}
              <button
                type="button"
                onClick={() => setNiche("__custom")}
                aria-pressed={niche === "__custom"}
                className={`rounded-full border px-3 py-1 text-sm ${niche === "__custom" ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}
              >
                Other…
              </button>
            </div>
            {niche === "__custom" && (
              <input
                className="mt-2 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
                value={customNiche}
                onChange={(e) => setCustomNiche(e.target.value)}
                placeholder="Your niche — e.g. cricket highlights"
                aria-label="Custom niche"
              />
            )}
          </fieldset>

          <Toggle
            checked={autoPost}
            onChange={(v) => setAutoPost(v)}
            label="Auto-post (publish without review)"
            hint="OFF by default. When on, approved-looking clips publish straight to your channels with no review step. Approval-first is the safe default — leave this off unless you are sure."
          />
          {autoPost && (
            <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200" role="alert">
              Warning: with auto-post on, the agent publishes clips without asking you. Only use it with a source and niche you trust.
            </p>
          )}

          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-amber-400"
              checked={rights}
              onChange={(e) => setRights(e.target.checked)}
            />
            <span>
              <span className="font-medium text-neutral-100">Rights acknowledgement (required)</span>
              <span className="mt-0.5 block text-xs text-neutral-400">
                This source is my own video/channel or I have a licence/rights to use it. The agent cannot be turned on without this.
              </span>
            </span>
          </label>

          <div className="flex flex-wrap gap-2">
            <PrimaryButton
              onClick={() => save({ source_type: sourceType, source_url: sourceUrl.trim(), niche: nicheValue, auto_post: autoPost, rights_confirmed: rights })}
              disabled={!engineAvailable || saving || acting}
            >
              {saving ? "Saving…" : "Save agent settings"}
            </PrimaryButton>
            {config && (
              <SecondaryButton href="/pipeline">See the pipeline</SecondaryButton>
            )}
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader title="AI engine" hint="Pick a provider, or use the built-in rule engine with no key" />
        <LlmProviders />
      </Card>
    </div>
  );
}
