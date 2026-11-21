// Settings (v0.4.0) — plain-language setup, no raw JSON as the main view.
//
// The complaint was that this page showed raw settings JSON (dry_run,
// client secrets "not configured", ffmpeg found:false, monetization
// thresholds) and left the user asking "how do I connect?". Now:
//   - LLM key: one field + Save + Test, result in plain words.
//   - Channels: one status line per platform + a link into the guided
//     Connect wizard (the keys are pasted there, not here).
//   - Updates: the desktop updater state (see components/Updater.tsx).
//   - Publishing safety, environment, monetization: short plain lines.
//   - Advanced (collapsed by default): the full settings JSON, read-only,
//     for support/debugging — plus "Replay onboarding".
//
// All values come from GET /settings (masked: only the last 4 characters
// of a secret ever reach this UI) and are saved with PUT /settings.
// Secrets are stored encrypted on the server — never in the repo, never
// in localStorage, never in a URL.
"use client";
import { useCallback, useEffect, useState } from "react";
import {
  explainConnectionError, getPerfProfile, getSettings, saveSettings, setPerfProfile,
  testConnection, updateOnboarding,
  type AppSettingsView, type PerfProfile, type PerfProfileName,
} from "@/lib/api";
import { UpdateSection } from "@/components/Updater";
import { FfmpegInstallSection } from "@/components/FfmpegInstaller";
import { YtDlpInstallSection } from "@/components/YtDlpInstaller";
import GeminiSetup from "@/components/GeminiSetup";
import { REPLAY_EVENT } from "@/components/Onboarding";
import Link from "next/link";
import { Card, CardHeader, DemoBadge, PrimaryButton, SecondaryButton, Skeleton, Toggle } from "@/components/ui";

const PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"] as const;
const PLATFORM_LABELS: Record<string, string> = {
  youtube: "YouTube",
  instagram: "Instagram",
  facebook: "Facebook",
  tiktok: "TikTok",
};

function StatusLine({ configured, masked }: { configured: boolean; masked: string }) {
  if (!configured) return <span className="text-sm text-neutral-500">Not set yet</span>;
  return (
    <span className="text-sm text-neutral-300">
      Saved <span className="font-mono">{masked || "••••••••"}</span>
    </span>
  );
}

export default function SettingsPage() {
  const [view, setView] = useState<AppSettingsView | null>(null);
  const [live, setLive] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; summary: string; nextStep: string }>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Performance profile (v0.8.0): GET /perf/profile + POST /perf/profile.
  const [perf, setPerf] = useState<PerfProfile | null>(null);
  const [perfLoaded, setPerfLoaded] = useState(false);
  const [perfSaving, setPerfSaving] = useState(false);
  const [perfNotice, setPerfNotice] = useState<string | null>(null);

  const loadPerf = useCallback(async () => {
    try {
      const p = await getPerfProfile();
      setPerf(p);
    } finally {
      setPerfLoaded(true);
    }
  }, []);

  useEffect(() => { loadPerf().catch(() => setPerfLoaded(true)); }, [loadPerf]);

  async function savePerf(profile: PerfProfileName) {
    setPerfSaving(true);
    setPerfNotice(null);
    const res = await setPerfProfile(profile);
    setPerfSaving(false);
    if (!res.ok) { setPerfNotice(res.error ?? "Could not save the performance profile."); return; }
    setPerfNotice(profile === "auto" ? "Back to Auto — the app picks the profile from this machine." : `Performance profile set to ${profile}.`);
    await loadPerf();
  }

  // Form state: empty means "keep the stored value"; a value means "set it".
  const [llmKey, setLlmKey] = useState("");
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [privacy, setPrivacy] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const fetched = await getSettings();
      setLive(fetched !== null);
      if (fetched) {
        setView(fetched);
        setLlmBaseUrl(fetched.llm.base_url);
        setLlmModel(fetched.llm.model);
        setDryRun(fetched.dry_run);
        setPrivacy(fetched.default_privacy);
      }
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { load().catch(() => setLive(false)); }, [load]);

  async function save(patch: Record<string, unknown>) {
    setSaving(true);
    setNotice(null);
    const res = await saveSettings(patch);
    setSaving(false);
    if (!res.ok) { setNotice(res.error ?? "Save failed."); return; }
    setNotice("Saved.");
    setLlmKey("");
    await load();
  }

  async function test(service: string, label: string) {
    setTesting(service);
    const res = await testConnection(service);
    setTesting(null);
    const detail = (res.data as { message?: string } | null)?.message;
    if (res.ok) {
      setTestResults((prev) => ({
        ...prev,
        [service]: { ok: true, summary: `${label} is working — ClipForge reached it with the saved key.`, nextStep: "" },
      }));
    } else {
      const explained = explainConnectionError(detail ?? res.error);
      setTestResults((prev) => ({ ...prev, [service]: { ok: false, summary: explained.summary, nextStep: explained.nextStep } }));
    }
  }

  function replayOnboarding() {
    try { window.localStorage.removeItem("clipforge-onboarding-done"); } catch { /* private mode */ }
    updateOnboarding({ completed: false }).catch(() => {});
    window.dispatchEvent(new Event(REPLAY_EVENT));
  }

  const loading = view === null && !loaded;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        {!live && !loading && <DemoBadge />}
        <span className="text-sm text-neutral-400">
          {live ? "Live settings from the server" : loading ? "Checking the server…" : "Server unreachable — settings cannot be changed"}
        </span>
      </div>

      {notice && (
        <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>
      )}

      {loading ? (
        <div className="space-y-4"><Skeleton className="h-48" /><Skeleton className="h-64" /><Skeleton className="h-48" /></div>
      ) : view ? (
        <>
          <Card>
            <h2 className="mb-1 text-lg font-semibold">AI titles (optional)</h2>
            <p className="mb-4 text-sm text-neutral-400">
              An OpenAI-compatible key makes ClipForge write smarter titles and descriptions. Only transcript text is ever sent — never video or audio.
              Without a key, titles still work using built-in templates.
            </p>
            <div className="space-y-3">
              <p className="text-sm">
                <span className="text-neutral-400">API key: </span>
                <StatusLine configured={view.llm.api_key.configured} masked={view.llm.api_key.masked} />
              </p>
              <label className="block text-sm">
                <span className="text-neutral-400">New API key (leave blank to keep the saved one)</span>
                <input type="password" autoComplete="off" className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={llmKey} onChange={(e) => setLlmKey(e.target.value)} placeholder="sk-…" />
              </label>
              <div className="flex flex-wrap gap-2">
                <PrimaryButton
                  onClick={() => save({ llm_api_key: llmKey })}
                  disabled={saving || !live || !llmKey.trim()}
                >
                  Save key
                </PrimaryButton>
                {view.llm.api_key.configured && (
                  <SecondaryButton onClick={() => save({ llm_api_key: "" })} disabled={saving || !live}>Remove key</SecondaryButton>
                )}
                <SecondaryButton onClick={() => test("llm", "The AI service")} disabled={testing === "llm" || !live}>
                  {testing === "llm" ? "Testing…" : "Test connection"}
                </SecondaryButton>
              </div>
              {testResults.llm && (
                <div className={`rounded-lg border p-3 text-sm ${testResults.llm.ok ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" : "border-red-500/40 bg-red-500/10 text-red-200"}`}>
                  <p className="font-medium">{testResults.llm.summary}</p>
                  {testResults.llm.nextStep && <p className="mt-1 text-neutral-300">{testResults.llm.nextStep}</p>}
                </div>
              )}
              <details className="text-sm">
                <summary className="cursor-pointer text-neutral-400 hover:text-neutral-200">Advanced AI settings (provider, model)</summary>
                <div className="mt-3 space-y-3">
                  <label className="block">
                    <span className="text-neutral-400">Base URL</span>
                    <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={llmBaseUrl} onChange={(e) => setLlmBaseUrl(e.target.value)} placeholder="https://api.openai.com/v1" />
                  </label>
                  <label className="block">
                    <span className="text-neutral-400">Model</span>
                    <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={llmModel} onChange={(e) => setLlmModel(e.target.value)} placeholder="gpt-4o-mini" />
                  </label>
                  <PrimaryButton onClick={() => save({ llm_base_url: llmBaseUrl, llm_model: llmModel })} disabled={saving || !live}>Save AI settings</PrimaryButton>
                </div>
              </details>
            </div>
          </Card>

          <Card>
            <CardHeader title="Gemini — the AI engine" hint="Optional, but smarter than the built-in engine" />
            <GeminiSetup />
          </Card>

          {/* Performance (v0.8.0) — the app measures this machine and picks
              a profile; the override here wins until set back to Auto. */}
          <Card>
            <h2 className="mb-1 text-lg font-semibold">Performance</h2>
            <p className="mb-4 text-sm text-neutral-400">
              ClipForge measures this machine (CPU, RAM, GPU) and picks a profile so clipping stays smooth.
              You can override it here — Auto lets the app decide again.
            </p>
            {!perfLoaded ? (
              <Skeleton className="h-24" />
            ) : perf === null ? (
              <p className="text-sm text-neutral-400">Performance profiles are not available in this build yet — the profile service has not landed on the server.</p>
            ) : (
              <div className="space-y-4">
                <dl className="space-y-1 text-sm">
                  <div className="flex justify-between gap-2"><dt className="text-neutral-400">Active profile</dt><dd className="font-semibold capitalize">{perf.profile}{perf.override ? " (your override)" : " (auto)"}</dd></div>
                  {perf.reason && <div className="flex justify-between gap-2"><dt className="text-neutral-400">Why this profile</dt><dd className="max-w-md text-right">{perf.reason}</dd></div>}
                  <div className="flex justify-between gap-2"><dt className="text-neutral-400">CPU</dt><dd>{perf.cpu_cores === null ? "Unknown" : `${perf.cpu_cores} cores`}</dd></div>
                  <div className="flex justify-between gap-2"><dt className="text-neutral-400">RAM</dt><dd>{perf.ram_gb === null ? "Unknown" : `${perf.ram_gb} GB`}</dd></div>
                  <div className="flex justify-between gap-2"><dt className="text-neutral-400">GPU</dt><dd>{perf.gpu ?? "None detected"}</dd></div>
                </dl>
                <fieldset>
                  <legend className="text-sm font-medium text-neutral-100">Override profile</legend>
                  <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Performance profile">
                    {(["auto", "low", "balanced", "high"] as const).map((p) => (
                      <button
                        key={p}
                        type="button"
                        role="radio"
                        aria-checked={perf.profile === p}
                        disabled={perfSaving || !live}
                        onClick={() => savePerf(p)}
                        className={`rounded-md border px-3 py-1.5 text-sm capitalize ${perf.profile === p ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"} disabled:cursor-not-allowed disabled:opacity-50`}
                      >
                        {p === "auto" ? "Auto" : p === "low" ? "Low-end" : p === "balanced" ? "Balanced" : "High performance"}
                      </button>
                    ))}
                  </div>
                </fieldset>
                {perfNotice && <p className="rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-sm text-sky-100">{perfNotice}</p>}
                <div className="space-y-2 text-sm text-neutral-400">
                  <p><span className="font-medium text-neutral-200">Auto</span> — the app picks from this machine&apos;s CPU, RAM and GPU.</p>
                  <p><span className="font-medium text-neutral-200">Low-end</span> — smallest transcription model, low-resolution previews, 1 job at a time, animations off. For older or low-RAM machines.</p>
                  <p><span className="font-medium text-neutral-200">Balanced</span> — medium transcription model, standard previews, 2 jobs at a time. The default on most machines.</p>
                  <p><span className="font-medium text-neutral-200">High performance</span> — largest transcription model, full-resolution previews, up to 4 jobs at a time, full animations. Needs a fast CPU/GPU and plenty of RAM.</p>
                </div>
              </div>
            )}
          </Card>

          <Card>
            <h2 className="mb-1 text-lg font-semibold">Channels</h2>
            <p className="mb-4 text-sm text-neutral-400">
              Connect each platform once with the guided wizard — it shows you where to create the developer app and where to paste the keys.
              Connection status lives on the <Link className="text-sky-400 underline" href="/channels">Channels page</Link>.
            </p>
            <ul className="space-y-3">
              {PLATFORMS.map((p) => {
                const c = view.credentials[p];
                const configured = c?.client_id.configured === true || c?.client_secret.configured === true;
                const result = testResults[p];
                return (
                  <li key={p} className="rounded-lg border border-neutral-800 p-4">
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="font-semibold">{PLATFORM_LABELS[p]}</span>
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${configured ? "bg-emerald-500/15 text-emerald-300" : "bg-amber-500/15 text-amber-300"}`}>
                        {configured ? "App keys saved" : "Not set up yet"}
                      </span>
                      <span className="ml-auto flex flex-wrap gap-2">
                        <SecondaryButton href={`/channels/connect?platform=${p}`}>{configured ? "Change keys" : "Set up"}</SecondaryButton>
                        {configured && (
                          <SecondaryButton onClick={() => test(p, PLATFORM_LABELS[p])} disabled={testing === p || !live}>
                            {testing === p ? "Testing…" : "Test connection"}
                          </SecondaryButton>
                        )}
                      </span>
                    </div>
                    <p className="mt-1 text-sm text-neutral-400">
                      {configured
                        ? "Keys are saved on the server. Next: press Connect in the wizard to sign in."
                        : "Not connected — the wizard takes about five minutes."}
                    </p>
                    {result && (
                      <div className={`mt-3 rounded-lg border p-3 text-sm ${result.ok ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" : "border-red-500/40 bg-red-500/10 text-red-200"}`}>
                        <p className="font-medium">{result.summary}</p>
                        {result.nextStep && <p className="mt-1 text-neutral-300">{result.nextStep}</p>}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </Card>

          <Card>
            <h2 className="mb-2 text-lg font-semibold">Updates</h2>
            <UpdateSection />
          </Card>

          <Card>
            <h2 className="mb-2 text-lg font-semibold">Publishing safety</h2>
            <Toggle
              checked={dryRun}
              label="Dry-run mode"
              hint="On: nothing is sent to any platform — publishes return the payload that would be sent. Turn off only with credentials configured and approved posts."
              onChange={(v) => { setDryRun(v); save({ dry_run: v }); }}
              disabled={saving || !live}
            />
            <p className="mt-1 text-xs text-neutral-500">Dry-run source: {view.dry_run_source === "settings" ? "this setting" : "CLIPFORGE_DRY_RUN environment variable"}. Approval-first still applies: nothing publishes without your approval on the queue page.</p>
            <div className="mt-4 space-y-2">
              <h3 className="text-sm font-medium">Default privacy per platform</h3>
              {PLATFORMS.map((p) => (
                <label key={p} className="flex items-center gap-3 text-sm">
                  <span className="w-24 text-neutral-400">{PLATFORM_LABELS[p]}</span>
                  <select
                    className="rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5"
                    value={privacy[p] ?? "private"}
                    onChange={(e) => {
                      const next = { ...privacy, [p]: e.target.value };
                      setPrivacy(next);
                      save({ default_privacy: next });
                    }}
                    disabled={saving || !live}
                  >
                    <option value="private">Private</option>
                    <option value="public">Public</option>
                    <option value="unlisted">Unlisted (YouTube)</option>
                  </select>
                </label>
              ))}
            </div>
          </Card>

          <Card>
            <h2 className="mb-2 text-lg font-semibold">This app</h2>
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between gap-2"><dt className="text-neutral-400">Data folder</dt><dd className="truncate font-mono text-xs">{view.data_dir}</dd></div>
              <div className="flex justify-between gap-2"><dt className="text-neutral-400">Secret storage</dt><dd>{view.secret_backend === "os-keyring" ? "OS keyring" : "Encrypted file (no OS keyring found — see docs)"}</dd></div>
              <div className="flex justify-between gap-2">
                <dt className="text-neutral-400">FFmpeg</dt>
                <dd className="min-w-0 flex-1">
                  {view.ffmpeg.found ? (
                    <>Found ({view.ffmpeg.path}{view.ffmpeg.version ? ` — ${view.ffmpeg.version}` : ""})</>
                  ) : (
                    <FfmpegInstallSection onInstalled={() => { load().catch(() => {}); }} />
                  )}
                </dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-neutral-400">yt-dlp</dt>
                <dd className="min-w-0 flex-1">
                  {view.yt_dlp.found ? (
                    <>Found ({view.yt_dlp.path}{view.yt_dlp.version ? ` — ${view.yt_dlp.version}` : ""})</>
                  ) : (
                    <YtDlpInstallSection onInstalled={() => { load().catch(() => {}); }} />
                  )}
                </dd>
              </div>
            </dl>
            <p className="mt-3 text-sm text-neutral-400">
              Need the first-run guide again?{" "}
              <button className="text-sky-400 underline" onClick={replayOnboarding}>Replay onboarding</button>
              {" "}or open the{" "}
              <Link className="text-sky-400 underline" href="/setup">full setup wizard</Link>.
            </p>
          </Card>

          <Card>
            <h2 className="mb-2 text-lg font-semibold">Monetization thresholds</h2>
            <p className="mb-3 text-sm text-neutral-400">Thresholds are data, not promises — payouts depend on qualified views, originality, country, and platform policy.</p>
            <ul className="space-y-3">
              {view.monetization_thresholds.map((t, i) => (
                <li key={`${t.platform}-${i}`} className="text-sm">
                  <span className="font-medium">{PLATFORM_LABELS[t.platform] ?? t.platform} — {t.program}</span>
                  <span className="ml-2 text-neutral-400">
                    {Object.entries(t.threshold).map(([k, v]) => `${k}: ${v.toLocaleString()}`).join(" · ")}
                  </span>
                  {t.note && <p className="mt-0.5 text-xs text-neutral-500">{t.note}</p>}
                </li>
              ))}
            </ul>
          </Card>

          <Card>
            <button
              onClick={() => setShowAdvanced((v) => !v)}
              className="flex w-full items-center justify-between text-left"
              aria-expanded={showAdvanced}
            >
              <span>
                <span className="text-lg font-semibold">Advanced</span>
                <span className="ml-2 text-xs text-neutral-500">Raw settings, for support and debugging only</span>
              </span>
              <span className="text-sm text-neutral-400">{showAdvanced ? "Hide ▲" : "Show ▼"}</span>
            </button>
            {showAdvanced && (
              <div className="mt-4">
                <p className="mb-2 text-xs text-neutral-500">This is the full settings object exactly as the server returns it. Secrets are masked — only the last 4 characters ever appear. Change settings using the sections above, not by editing this.</p>
                <pre className="max-h-96 overflow-auto rounded-lg bg-neutral-950 p-4 font-mono text-xs leading-relaxed text-neutral-300">{JSON.stringify(view, null, 2)}</pre>
                <p className="mt-3 text-sm text-neutral-400">
                  Need the first-run guide again?{" "}
                  <button className="text-sky-400 underline" onClick={replayOnboarding}>Replay onboarding</button>
                </p>
              </div>
            )}
          </Card>
        </>
      ) : (
        <Card>
          <p className="text-sm text-neutral-400">The server did not answer. Start the server, then reload this page.</p>
        </Card>
      )}
    </div>
  );
}
