// First-run setup wizard (v0.5.0) — one proper setup, five steps:
//   1. FFmpeg install
//   2. Gemini key (optional — skippable)
//   3. Connect a channel (links into the existing Connect wizard)
//   4. Pick niche + source
//   5. Turn on the agent
//
// Progress lives on the server (GET/POST /api/setup/state, stored in
// <data_dir>/setup.json) with the /onboarding prefs + a localStorage
// flag as fallback, so the wizard is skippable step-by-step and
// resumable across restarts, and never nags once completed. Each step
// shows done/pending state in the stepper.
//
// The modal wrapper (default export) auto-shows on first run and on
// the "Replay onboarding" event from Settings. The same wizard is
// also available as a full page at /setup (SetupWizard export).
"use client";
import { useCallback, useEffect, useState } from "react";
import {
  NICHE_PRESETS, getChannels, getLlmProviders, getOnboarding, getSettings, getSetupState,
  saveAgentConfig, saveSetupState, startAgent, updateOnboarding,
  type AppSettingsView, type ChannelStatus, type SetupState, type SetupStatePatch, type SetupStepKey,
} from "@/lib/api";
import { FfmpegInstallSection } from "./FfmpegInstaller";
import LlmProviders from "./LlmProviders";
import { Card, PrimaryButton, SecondaryButton, Skeleton } from "./ui";

const DONE_KEY = "clipforge-onboarding-done";
export const REPLAY_EVENT = "clipforge:replay-onboarding";

const STEP_KEYS: SetupStepKey[] = ["ffmpeg", "gemini", "channel", "niche_source", "agent"];
const STEP_TITLES: Record<SetupStepKey, string> = {
  ffmpeg: "Install FFmpeg",
  gemini: "AI engine (optional)",
  channel: "Connect a channel",
  niche_source: "Niche & source",
  agent: "Turn on the agent",
};

interface WizardProps {
  /** Full-page rendering (the /setup page) instead of a modal. */
  inline?: boolean;
  onDone?: () => void;
}

export function SetupWizard({ inline = false, onDone }: WizardProps) {
  const [state, setState] = useState<SetupState | null>(null);
  const [settings, setSettings] = useState<AppSettingsView | null>(null);
  const [channels, setChannels] = useState<ChannelStatus[] | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // Step 4 form.
  const [niche, setNiche] = useState("");
  const [customNiche, setCustomNiche] = useState("");
  const [sourceType, setSourceType] = useState<"video" | "channel">("video");
  const [sourceUrl, setSourceUrl] = useState("");
  // Step 5 form.
  const [rights, setRights] = useState(false);
  const [agentStarting, setAgentStarting] = useState(false);

  const refresh = useCallback(async () => {
    const [fetchedState, fetchedSettings, fetchedChannels] = await Promise.all([
      getSetupState(), getSettings(), getChannels(),
    ]);
    const fallback: SetupState = {
      current_step: 0,
      steps: { ffmpeg: false, gemini: false, channel: false, niche_source: false, agent: false },
      niche: "", source_type: "video", source_url: "", completed: false,
    };
    const next = fetchedState ?? fallback;
    setState(next);
    setStep(Math.min(next.current_step, STEP_KEYS.length - 1));
    // The backend defaults an unset niche to "general" — in the UI
    // that means "nothing picked yet".
    const savedNiche = next.niche === "general" ? "" : next.niche;
    setNiche(savedNiche);
    setSourceType(next.source_type);
    setSourceUrl(next.source_url);
    if (savedNiche && !NICHE_PRESETS.includes(savedNiche as (typeof NICHE_PRESETS)[number])) {
      setNiche("__custom");
      setCustomNiche(savedNiche);
    }
    setSettings(fetchedSettings);
    setChannels(fetchedChannels);
    setLoaded(true);
  }, []);

  useEffect(() => { refresh().catch(() => setLoaded(true)); }, [refresh]);

  async function record(patch: SetupStatePatch) {
    setSaving(true);
    const res = await saveSetupState(patch);
    setSaving(false);
    if (res.ok) {
      setState((prev) => (prev ? { ...prev, ...patch, steps: { ...prev.steps, ...(patch.steps ?? {}) } } : prev));
      return true;
    }
    setNotice(res.error ?? "Could not save this step — the server did not answer.");
    return false;
  }

  async function advance(markDone?: SetupStepKey) {
    const patch: SetupStatePatch = { current_step: Math.min(step + 1, STEP_KEYS.length - 1) };
    if (markDone) patch.steps = { [markDone]: true };
    await record(patch);
    setStep((s) => Math.min(s + 1, STEP_KEYS.length - 1));
  }

  function finish() {
    record({ completed: true }).catch(() => {});
    updateOnboarding({ completed: true }).catch(() => {});
    try { window.localStorage.setItem(DONE_KEY, "1"); } catch { /* private mode */ }
    onDone?.();
  }

  const ffmpegReady = settings?.ffmpeg.found === true;
  const anyChannelConnected = (channels ?? []).some((c) => c.status === "connected");
  const nicheValue = niche === "__custom" ? customNiche.trim() : niche;
  const stepKey = STEP_KEYS[step];

  async function continueFromGemini() {
    // Only mark the step done when a key really is saved; otherwise the
    // step just advances and stays pending (it was optional anyway).
    const [providers, settingsNow] = await Promise.all([getLlmProviders(), getSettings()]);
    const configured =
      providers?.some((p) => p.configured) === true || settingsNow?.llm.api_key.configured === true;
    await advance(configured ? "gemini" : undefined);
  }

  async function checkChannelAgain() {
    const fresh = await getChannels();
    setChannels(fresh);
    if ((fresh ?? []).some((c) => c.status === "connected")) {
      await advance("channel");
    } else {
      setNotice("No channel is connected yet — use the Connect wizard, sign in on the platform's page, then press this button again.");
    }
  }

  async function saveNicheAndSource() {
    const ok = await record({
      niche: nicheValue, source_type: sourceType, source_url: sourceUrl.trim(),
      steps: { niche_source: true },
    });
    if (ok) setStep((s) => Math.min(s + 1, STEP_KEYS.length - 1));
  }

  async function turnOnAgent() {
    if (!rights) { setNotice("Confirm the rights checkbox first — the agent cannot be turned on without it."); return; }
    if (!sourceUrl.trim() || !nicheValue) {
      setNotice("Go back to step 4 first — the agent needs a source URL and a niche.");
      return;
    }
    setAgentStarting(true);
    setNotice(null);
    const saved = await saveAgentConfig({
      enabled: true, source_type: sourceType, source_url: sourceUrl.trim(),
      niche: nicheValue, auto_post: false, rights_confirmed: true,
    });
    if (!saved.ok) {
      setAgentStarting(false);
      setNotice("The agent service is not available in this build yet — finish setup now and turn the agent on from the Agent page once it lands.");
      return;
    }
    const started = await startAgent();
    setAgentStarting(false);
    if (!started.ok) {
      setNotice(started.error ?? "Could not start the agent.");
      return;
    }
    await record({ steps: { agent: true } });
    finish();
  }

  if (!loaded) {
    return (
      <Card className={inline ? "" : "w-full max-w-lg"}>
        <Skeleton className="h-8 w-64" /><Skeleton className="mt-4 h-40" />
      </Card>
    );
  }

  const body = (
    <Card className={inline ? "" : "w-full max-w-lg"}>
      <div className="flex items-center gap-3">
        <h2 className="text-xl font-bold">{inline ? "Set up ClipForge" : "Welcome to ClipForge"}</h2>
        <span className="ml-auto text-xs text-neutral-500">Step {step + 1} of {STEP_KEYS.length}</span>
      </div>
      <p className="mt-1 text-sm text-neutral-400">
        Five quick steps and you&apos;re ready. Every step can be skipped — you can come back here any time from Settings.
      </p>

      <ol className="mt-4 flex gap-2">
        {STEP_KEYS.map((key, i) => {
          const done = state?.steps[key] === true;
          return (
            <li key={key} className={`flex-1 rounded-full px-2 py-1 text-center text-[11px] font-medium ${i === step ? "bg-amber-400 text-neutral-950" : done ? "bg-emerald-500/20 text-emerald-300" : "bg-neutral-800 text-neutral-400"}`}>
              {done ? "✓ " : ""}{STEP_TITLES[key]}
            </li>
          );
        })}
      </ol>

      {notice && (
        <div className="mt-4 rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-sm text-sky-100">{notice}</div>
      )}

      <div className="mt-5 min-h-40">
        {stepKey === "ffmpeg" && (
          <div className="space-y-3">
            <h3 className="font-semibold">1. Install FFmpeg</h3>
            <p className="text-sm text-neutral-400">FFmpeg cuts and renders your clips. ClipForge installs it for you — one click, no manual download.</p>
            {!settings ? (
              <Skeleton className="h-6 w-64" />
            ) : (
              <FfmpegInstallSection onInstalled={() => { getSettings().then((s) => { if (s) setSettings(s); }).catch(() => {}); }} />
            )}
            <div className="flex flex-wrap gap-2 pt-2">
              <PrimaryButton onClick={() => advance("ffmpeg")} disabled={saving || !ffmpegReady}>
                {ffmpegReady ? "FFmpeg is ready — continue" : "Install FFmpeg first"}
              </PrimaryButton>
              <SecondaryButton onClick={() => advance()} disabled={saving}>Skip for now</SecondaryButton>
            </div>
          </div>
        )}

        {stepKey === "gemini" && (
          <div className="space-y-3">
            <h3 className="font-semibold">2. AI engine (optional)</h3>
            <LlmProviders />
            <div className="flex flex-wrap gap-2 pt-2">
              <PrimaryButton onClick={continueFromGemini} disabled={saving}>Continue</PrimaryButton>
              <SecondaryButton onClick={() => advance()} disabled={saving}>Skip — use the built-in engine</SecondaryButton>
            </div>
          </div>
        )}

        {stepKey === "channel" && (
          <div className="space-y-3">
            <h3 className="font-semibold">3. Connect a channel</h3>
            <p className="text-sm text-neutral-400">
              Connect at least one channel so clips have somewhere to go. The Connect wizard walks through the platform&apos;s developer app and sign-in.
            </p>
            {channels === null ? (
              <Skeleton className="h-6 w-64" />
            ) : (
              <ul className="space-y-1 text-sm">
                {channels.map((c) => (
                  <li key={c.platform} className="flex items-center gap-2">
                    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${c.status === "connected" ? "bg-emerald-500/15 text-emerald-300" : "bg-neutral-700/50 text-neutral-300"}`}>
                      {c.status === "connected" ? "Connected" : "Not connected"}
                    </span>
                    <span className="text-neutral-200">{c.display_name}</span>
                    {c.account_name && <span className="text-neutral-500">— {c.account_name}</span>}
                  </li>
                ))}
              </ul>
            )}
            <div className="flex flex-wrap gap-2 pt-2">
              <PrimaryButton href="/channels/connect?platform=youtube" onClick={onDone}>Open the Connect wizard</PrimaryButton>
              <SecondaryButton onClick={checkChannelAgain} disabled={saving}>
                {anyChannelConnected ? "Channel connected — continue" : "I connected a channel — check again"}
              </SecondaryButton>
              <SecondaryButton onClick={() => advance()} disabled={saving}>Skip for now</SecondaryButton>
            </div>
          </div>
        )}

        {stepKey === "niche_source" && (
          <div className="space-y-4">
            <h3 className="font-semibold">4. Pick a niche and a source</h3>
            <div>
              <p className="text-sm text-neutral-400">What are the clips about?</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {NICHE_PRESETS.map((n) => (
                  <button key={n} type="button" onClick={() => setNiche(n)} aria-pressed={niche === n}
                    className={`rounded-full border px-3 py-1 text-sm capitalize ${niche === n ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}>
                    {n}
                  </button>
                ))}
                <button type="button" onClick={() => setNiche("__custom")} aria-pressed={niche === "__custom"}
                  className={`rounded-full border px-3 py-1 text-sm ${niche === "__custom" ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}>
                  Other…
                </button>
              </div>
              {niche === "__custom" && (
                <input className="mt-2 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={customNiche}
                  onChange={(e) => setCustomNiche(e.target.value)} placeholder="Your niche — e.g. cricket highlights" aria-label="Custom niche" />
              )}
            </div>
            <div>
              <p className="text-sm text-neutral-400">Where should the agent get videos from?</p>
              <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Source type">
                {(["video", "channel"] as const).map((t) => (
                  <button key={t} type="button" role="radio" aria-checked={sourceType === t} onClick={() => setSourceType(t)}
                    className={`rounded-md border px-3 py-1.5 text-sm ${sourceType === t ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}>
                    {t === "video" ? "One video URL" : "Source YouTube channel URL"}
                  </button>
                ))}
              </div>
              <input className="mt-2 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={sourceUrl}
                onChange={(e) => setSourceUrl(e.target.value)}
                placeholder={sourceType === "video" ? "https://www.youtube.com/watch?v=…" : "https://www.youtube.com/@yourchannel"}
                aria-label={sourceType === "video" ? "Source video URL" : "Source channel URL"} />
            </div>
            <div className="flex flex-wrap gap-2 pt-1">
              <PrimaryButton onClick={saveNicheAndSource} disabled={saving || !nicheValue || !sourceUrl.trim()}>
                Save and continue
              </PrimaryButton>
              <SecondaryButton onClick={() => advance()} disabled={saving}>Skip for now</SecondaryButton>
            </div>
          </div>
        )}

        {stepKey === "agent" && (
          <div className="space-y-3">
            <h3 className="font-semibold">5. Turn on the agent</h3>
            <p className="text-sm text-neutral-400">
              The agent watches {sourceType === "channel" ? "your source channel" : "your source video"}
              {nicheValue ? ` for ${nicheValue} moments` : ""}, makes clips, and queues them for your approval.
              Nothing posts without your approval — auto-post stays off.
            </p>
            <label className="flex items-start gap-3 text-sm">
              <input type="checkbox" className="mt-0.5 h-4 w-4 accent-amber-400" checked={rights} onChange={(e) => setRights(e.target.checked)} />
              <span>
                <span className="font-medium text-neutral-100">Rights acknowledgement (required)</span>
                <span className="mt-0.5 block text-xs text-neutral-400">
                  This source is my own video/channel or I have a licence/rights to use it.
                </span>
              </span>
            </label>
            <div className="flex flex-wrap gap-2 pt-2">
              <PrimaryButton onClick={turnOnAgent} disabled={agentStarting || !rights}>
                {agentStarting ? "Starting…" : "Turn on the agent"}
              </PrimaryButton>
              <SecondaryButton onClick={finish} disabled={saving}>Finish setup — I&apos;ll turn it on later</SecondaryButton>
            </div>
          </div>
        )}
      </div>

      <div className="mt-5 flex items-center justify-between border-t border-neutral-800 pt-4">
        <button
          className="text-sm text-neutral-400 underline disabled:cursor-not-allowed disabled:opacity-50"
          onClick={() => setStep((s) => Math.max(0, s - 1))}
          disabled={step === 0}
        >
          ← Back
        </button>
        <button className="text-sm text-neutral-400 underline" onClick={finish}>Skip the whole setup</button>
      </div>
    </Card>
  );

  if (inline) return body;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-black/70 p-4" role="dialog" aria-label="Set up ClipForge">
      {body}
    </div>
  );
}

export default function Onboarding() {
  const [visible, setVisible] = useState(false);

  const checkShouldShow = useCallback(async (force: boolean) => {
    // The /setup page renders this same wizard inline — the modal must
    // never stack on top of it.
    if (typeof window !== "undefined" && window.location.pathname.startsWith("/setup")) return;
    const [channels, fetched, serverProgress, setupState] = await Promise.all([
      getChannels(), getSettings(), getOnboarding(), getSetupState(),
    ]);
    if (channels === null || fetched === null) return; // API unreachable: don't nag
    setVisible(false);
    const localDone = typeof window !== "undefined" && window.localStorage.getItem(DONE_KEY) === "1";
    if (!force && (localDone || serverProgress?.completed === true || setupState?.completed === true)) {
      try { window.localStorage.setItem(DONE_KEY, "1"); } catch { /* private mode */ }
      return;
    }
    const anyConnected = (channels as ChannelStatus[]).some((c) => c.status === "connected");
    const llmSet = fetched.llm.api_key.configured;
    if (!force && (anyConnected || llmSet)) {
      try { window.localStorage.setItem(DONE_KEY, "1"); } catch { /* private mode */ }
      updateOnboarding({ completed: true }).catch(() => {});
      saveSetupState({ completed: true }).catch(() => {});
      return;
    }
    setVisible(true);
  }, []);

  useEffect(() => {
    checkShouldShow(false).catch(() => {});
    const onReplay = () => { checkShouldShow(true).catch(() => setVisible(true)); };
    window.addEventListener(REPLAY_EVENT, onReplay);
    return () => window.removeEventListener(REPLAY_EVENT, onReplay);
  }, [checkShouldShow]);

  if (!visible) return null;
  return <SetupWizard onDone={() => setVisible(false)} />;
}
