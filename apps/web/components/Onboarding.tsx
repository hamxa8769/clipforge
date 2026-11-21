// First-run onboarding wizard (v0.4.0) — shown once, when no channel is
// connected and no LLM key is set. Four short steps:
//   (a) FFmpeg check with guided install help,
//   (b) LLM key (optional — skippable),
//   (c) connect the first channel (links into the Connect wizard),
//   (d) make the first clip.
//
// "Done" is persisted in localStorage so it never nags; Settings has a
// "Replay onboarding" link that dispatches a window event this component
// listens for. Navigating away from the wizard (e.g. into the Connect
// wizard) dismisses it for the session — it reappears on next launch
// only if setup is still untouched.
"use client";
import { useCallback, useEffect, useState } from "react";
import { getChannels, getOnboarding, getSettings, saveSettings, updateOnboarding, type AppSettingsView, type ChannelStatus } from "@/lib/api";
import { Card, PrimaryButton, SecondaryButton, Skeleton } from "./ui";

const DONE_KEY = "clipforge-onboarding-done";
export const REPLAY_EVENT = "clipforge:replay-onboarding";

const STEPS = ["FFmpeg", "AI key (optional)", "First channel", "First clip"] as const;

export default function Onboarding() {
  const [visible, setVisible] = useState(false);
  const [step, setStep] = useState(0);
  const [settings, setSettings] = useState<AppSettingsView | null>(null);
  const [llmKey, setLlmKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  const [keySaved, setKeySaved] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);

  const checkShouldShow = useCallback(async (force: boolean) => {
    // Always fetch first: the wizard needs the settings (FFmpeg check)
    // even when it decides not to show — otherwise a later "Replay
    // onboarding" would open on an empty skeleton that never fills.
    // Progress lives in server prefs (GET/PUT /onboarding) with a
    // localStorage flag as well, so the wizard never nags even when the
    // server is unreachable on a later launch. An older server without
    // /onboarding falls back to the localStorage flag alone.
    const [channels, fetched, serverProgress] = await Promise.all([
      getChannels(), getSettings(), getOnboarding(),
    ]);
    if (channels === null || fetched === null) return; // API unreachable: don't nag
    setSettings(fetched);
    const localDone = typeof window !== "undefined" && window.localStorage.getItem(DONE_KEY) === "1";
    if (!force && (localDone || serverProgress?.completed === true)) {
      try { window.localStorage.setItem(DONE_KEY, "1"); } catch { /* private mode */ }
      return;
    }
    const anyConnected = (channels as ChannelStatus[]).some((c) => c.status === "connected");
    const llmSet = fetched.llm.api_key.configured;
    if (!force && (anyConnected || llmSet)) {
      try { window.localStorage.setItem(DONE_KEY, "1"); } catch { /* private mode */ }
      updateOnboarding({ completed: true }).catch(() => {});
      return;
    }
    setStep(0);
    setVisible(true);
  }, []);

  useEffect(() => {
    checkShouldShow(false).catch(() => {});
    const onReplay = () => { checkShouldShow(true).catch(() => { setStep(0); setVisible(true); }); };
    window.addEventListener(REPLAY_EVENT, onReplay);
    return () => window.removeEventListener(REPLAY_EVENT, onReplay);
  }, [checkShouldShow]);

  function finish() {
    try { window.localStorage.setItem(DONE_KEY, "1"); } catch { /* private mode */ }
    updateOnboarding({ completed: true }).catch(() => {});
    setVisible(false);
  }

  /** Record one step of progress in the server prefs (best-effort). */
  function recordProgress(patch: Parameters<typeof updateOnboarding>[0]) {
    updateOnboarding(patch).catch(() => {});
  }

  async function saveKey() {
    if (!llmKey.trim()) return;
    setSavingKey(true);
    setKeyError(null);
    const res = await saveSettings({ llm_api_key: llmKey.trim() });
    setSavingKey(false);
    if (!res.ok) { setKeyError(res.error ?? "Could not save the key."); return; }
    setKeySaved(true);
    setLlmKey("");
    recordProgress({ llm: "set" });
    setStep(2);
  }

  if (!visible) return null;

  const ffmpeg = settings?.ffmpeg;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-label="Welcome to ClipForge">
      <Card className="w-full max-w-lg">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-bold">Welcome to ClipForge</h2>
          <span className="ml-auto text-xs text-neutral-500">Step {step + 1} of {STEPS.length}</span>
        </div>
        <p className="mt-1 text-sm text-neutral-400">Four quick steps and you&apos;re ready to make clips. Nothing here is permanent — you can change all of it later in Settings.</p>

        <ol className="mt-4 flex gap-2">
          {STEPS.map((label, i) => (
            <li key={label} className={`flex-1 rounded-full px-2 py-1 text-center text-[11px] font-medium ${i === step ? "bg-amber-400 text-neutral-950" : i < step ? "bg-emerald-500/20 text-emerald-300" : "bg-neutral-800 text-neutral-400"}`}>
              {i < step ? "✓ " : ""}{label}
            </li>
          ))}
        </ol>

        <div className="mt-5 min-h-40">
          {step === 0 && (
            <div className="space-y-3">
              <h3 className="font-semibold">1. Check FFmpeg</h3>
              {!settings ? (
                <Skeleton className="h-6 w-64" />
              ) : ffmpeg?.found ? (
                <p className="text-sm text-neutral-300">FFmpeg is installed and ready — clipping will work. <span className="text-neutral-500">({ffmpeg.path})</span></p>
              ) : (
                <>
                  <p className="text-sm text-neutral-300">FFmpeg is <strong>missing</strong> — ClipForge needs it to cut video clips. It takes two minutes to install:</p>
                  <ol className="list-decimal space-y-1 pl-5 text-sm text-neutral-300">
                    <li>Open the FFmpeg download page and get the Windows build (gpl, full): <a className="text-sky-400 underline" href="https://ffmpeg.org/download.html" target="_blank" rel="noreferrer">ffmpeg.org/download.html</a></li>
                    <li>Unzip it somewhere permanent, e.g. <code className="rounded bg-neutral-800 px-1 font-mono text-xs">C:\ffmpeg</code>.</li>
                    <li>Add <code className="rounded bg-neutral-800 px-1 font-mono text-xs">C:\ffmpeg\bin</code> to your PATH (search “Edit the system environment variables” → Environment Variables → Path → New), then restart ClipForge.</li>
                  </ol>
                  <p className="text-xs text-neutral-500">On Mac: <code className="rounded bg-neutral-800 px-1 font-mono text-xs">brew install ffmpeg</code>. On Linux: <code className="rounded bg-neutral-800 px-1 font-mono text-xs">sudo apt install ffmpeg</code>.</p>
                </>
              )}
            </div>
          )}

          {step === 1 && (
            <div className="space-y-3">
              <h3 className="font-semibold">2. AI key — better titles (optional)</h3>
              <p className="text-sm text-neutral-300">An OpenAI-compatible API key makes ClipForge write smarter titles and descriptions. Without one, titles still work — they use built-in templates instead. You can skip this and add a key later in Settings.</p>
              {keySaved ? (
                <p className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-200">Key saved. Moving on…</p>
              ) : (
                <>
                  <label className="block text-sm">
                    <span className="text-neutral-400">API key</span>
                    <input type="password" autoComplete="off" className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={llmKey} onChange={(e) => setLlmKey(e.target.value)} placeholder="sk-…" />
                  </label>
                  {keyError && <p className="text-sm text-red-300">{keyError}</p>}
                  <div className="flex gap-2">
                    <PrimaryButton onClick={() => saveKey()} disabled={savingKey || !llmKey.trim()}>{savingKey ? "Saving…" : "Save key & continue"}</PrimaryButton>
                    <SecondaryButton onClick={() => { recordProgress({ llm: "skipped" }); setStep(2); }}>Skip — use templates</SecondaryButton>
                  </div>
                </>
              )}
            </div>
          )}

          {step === 2 && (
            <div className="space-y-3">
              <h3 className="font-semibold">3. Connect your first channel</h3>
              <p className="text-sm text-neutral-300">Pick a platform and the Connect wizard walks you through it: create the developer app, paste the keys, press Connect. It takes about five minutes per platform, and you only do it once.</p>
              <p className="text-sm text-neutral-400">Until a channel is connected, ClipForge still clips and scores — it just saves posts as drafts instead of publishing.</p>
              <div><PrimaryButton href="/channels/connect" onClick={() => setVisible(false)}>Open the Connect wizard</PrimaryButton></div>
              <p className="text-xs text-neutral-500">Or skip for now — you can connect later from the Channels page.</p>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-3">
              <h3 className="font-semibold">4. Make your first clip</h3>
              <p className="text-sm text-neutral-300">Give ClipForge a video (your own content) or a YouTube link to your own video. It transcribes it, finds the best moments, scores up to 10 clips, and shows them for your approval. Nothing publishes without your approval.</p>
              <div><PrimaryButton href="/automation" onClick={finish}>Create your first clip job</PrimaryButton></div>
            </div>
          )}
        </div>

        <div className="mt-6 flex items-center gap-2 border-t border-neutral-800 pt-4">
          {step > 0 && step < 3 && <SecondaryButton onClick={() => setStep(step - 1)}>Back</SecondaryButton>}
          {step === 0 && <PrimaryButton onClick={() => { recordProgress({ ffmpeg_acknowledged: true }); setStep(1); }}>Next</PrimaryButton>}
          {step === 2 && <SecondaryButton onClick={() => setStep(3)}>Skip for now</SecondaryButton>}
          {step === 3 && <SecondaryButton onClick={finish}>Done</SecondaryButton>}
          <button onClick={finish} className="ml-auto text-sm text-neutral-500 hover:text-neutral-300">Don&apos;t show again</button>
        </div>
      </Card>
    </div>
  );
}
