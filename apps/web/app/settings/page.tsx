// API Settings (v0.3.0) — app credentials, LLM key, publishing safety,
// environment status. All values come from GET /settings (masked: only
// the last 4 characters of a secret ever reach this UI) and are saved
// with PUT /settings. Secrets are stored encrypted on the server (OS
// keyring where available, else an encrypted file) — never in the repo,
// never in localStorage, never in a URL. Test Connection hits the real
// API with the stored credential and reports the exact result.
"use client";
import { useCallback, useEffect, useState } from "react";
import {
  getSettings, saveSettings, testConnection,
  type AppSettingsView,
} from "@/lib/api";
import { Card, DemoBadge, PrimaryButton, SecondaryButton, Skeleton, Toggle } from "@/components/ui";

const PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"] as const;
const PLATFORM_LABELS: Record<string, string> = {
  youtube: "YouTube",
  instagram: "Instagram",
  facebook: "Facebook",
  tiktok: "TikTok",
};
const REDIRECT_HELP: Record<string, string> = {
  youtube: "Google Cloud Console → APIs & Services → Credentials → your OAuth client. Add the redirect URI below to Authorized redirect URIs.",
  instagram: "Meta for Developers → your app → Instagram Graph API. The same Meta app serves Instagram and Facebook.",
  facebook: "Meta for Developers → your app → Facebook Login → Settings. Add the redirect URI below to Valid OAuth Redirect URIs.",
  tiktok: "TikTok for Developers → your app → Login Kit. Add the redirect URI below exactly as shown.",
};

function Masked({ configured, masked, source }: { configured: boolean; masked: string; source: string }) {
  if (!configured) return <span className="text-sm text-neutral-500">Not set</span>;
  return (
    <span className="text-sm text-neutral-300">
      <span className="font-mono">{masked || "••••••••"}</span>
      <span className="ml-2 text-xs text-neutral-500">({source === "env" ? "from environment" : "stored encrypted"})</span>
    </span>
  );
}

export default function SettingsPage() {
  const [view, setView] = useState<AppSettingsView | null>(null);
  const [live, setLive] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, string>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  // Form state: empty means "keep the stored value"; a value means "set it".
  const [llmKey, setLlmKey] = useState("");
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [creds, setCreds] = useState<Record<string, { client_id: string; client_secret: string }>>({});
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
        setCreds(Object.fromEntries(PLATFORMS.map((p) => [p, { client_id: "", client_secret: "" }])));
      }
    } finally {
      // The load attempt finished (success or API unreachable). Without
      // this, `loading` (view === null) stays true forever when the API
      // is unreachable and the page shows skeletons instead of the
      // "API did not answer" state below.
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
    setCreds(Object.fromEntries(PLATFORMS.map((p) => [p, { client_id: "", client_secret: "" }])));
    await load();
  }

  async function test(service: string) {
    setTesting(service);
    const res = await testConnection(service);
    setTesting(null);
    const detail = (res.data as { message?: string } | null)?.message;
    setTestResults((prev) => ({
      ...prev,
      [service]: res.ok ? (detail ?? "OK.") : (res.error ?? "Failed."),
    }));
  }

  async function copy(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 2000);
    } catch {
      setCopied(null);
    }
  }

  const loading = view === null && !loaded;
  const redirectBase = "http://127.0.0.1:8000";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold tracking-tight">API Settings</h1>
        {!live && !loading && <DemoBadge />}
        <span className="text-sm text-neutral-400">
          {live ? "Live settings from the API" : loading ? "Checking the API…" : "API unreachable — settings cannot be changed"}
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
            <h2 className="mb-1 text-lg font-semibold">LLM provider (SEO polish)</h2>
            <p className="mb-4 text-sm text-neutral-400">
              OpenAI-compatible endpoint. Only transcript text is ever sent — never video or audio.
              Without a key, SEO stays purely deterministic templates.
            </p>
            <div className="space-y-3">
              <label className="block text-sm">
                <span className="text-neutral-400">Base URL</span>
                <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={llmBaseUrl} onChange={(e) => setLlmBaseUrl(e.target.value)} placeholder="https://api.openai.com/v1" />
              </label>
              <label className="block text-sm">
                <span className="text-neutral-400">Model</span>
                <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={llmModel} onChange={(e) => setLlmModel(e.target.value)} placeholder="gpt-4o-mini" />
              </label>
              <div className="text-sm">
                <span className="text-neutral-400">API key: </span>
                <Masked configured={view.llm.api_key.configured} masked={view.llm.api_key.masked} source={view.llm.api_key.source} />
              </div>
              <label className="block text-sm">
                <span className="text-neutral-400">New API key (leave blank to keep the stored one)</span>
                <input type="password" autoComplete="off" className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={llmKey} onChange={(e) => setLlmKey(e.target.value)} placeholder="sk-…" />
              </label>
              <div className="flex flex-wrap gap-2">
                <PrimaryButton
                  onClick={() => save({ llm_base_url: llmBaseUrl, llm_model: llmModel, ...(llmKey ? { llm_api_key: llmKey } : {}) })}
                  disabled={saving || !live}
                >
                  Save LLM settings
                </PrimaryButton>
                {view.llm.api_key.configured && (
                  <SecondaryButton onClick={() => save({ llm_api_key: "" })} disabled={saving || !live}>Clear key</SecondaryButton>
                )}
                <SecondaryButton onClick={() => test("llm")} disabled={testing === "llm" || !live}>
                  {testing === "llm" ? "Testing…" : "Test connection"}
                </SecondaryButton>
              </div>
              {testResults.llm && <p className="text-sm text-neutral-300">{testResults.llm}</p>}
            </div>
          </Card>

          <Card>
            <h2 className="mb-1 text-lg font-semibold">Platform apps (OAuth)</h2>
            <p className="mb-4 text-sm text-neutral-400">
              One developer app per platform. The client secret is stored encrypted on the server;
              this page only ever shows its last 4 characters. Register the redirect URI exactly as
              shown — the platform refuses the callback otherwise.
            </p>
            <div className="space-y-6">
              {PLATFORMS.map((p) => {
                const c = view.credentials[p];
                const uri = `${redirectBase}/auth/callback/${p}`;
                const draft = creds[p] ?? { client_id: "", client_secret: "" };
                return (
                  <div key={p} className="rounded-lg border border-neutral-800 p-4">
                    <h3 className="font-semibold">{PLATFORM_LABELS[p]}</h3>
                    <p className="mt-1 text-xs leading-relaxed text-neutral-500">{REDIRECT_HELP[p]}</p>
                    <div className="mt-3 space-y-3">
                      <label className="block text-sm">
                        <span className="text-neutral-400">Redirect URI (copy into the platform app)</span>
                        <span className="mt-1 flex gap-2">
                          <input readOnly className="w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-xs" value={uri} />
                          <SecondaryButton onClick={() => copy(uri, p)}>{copied === p ? "Copied" : "Copy"}</SecondaryButton>
                        </span>
                      </label>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="block text-sm">
                          <span className="text-neutral-400">Client ID {c?.client_id.configured ? `(${c.client_id.masked})` : "(not set)"}</span>
                          <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={draft.client_id} onChange={(e) => setCreds((prev) => ({ ...prev, [p]: { ...draft, client_id: e.target.value } }))} placeholder="New client ID (blank = keep)" />
                        </label>
                        <label className="block text-sm">
                          <span className="text-neutral-400">Client secret {c?.client_secret.configured ? `(${c.client_secret.masked})` : "(not set)"}</span>
                          <input type="password" autoComplete="off" className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={draft.client_secret} onChange={(e) => setCreds((prev) => ({ ...prev, [p]: { ...draft, client_secret: e.target.value } }))} placeholder="New secret (blank = keep)" />
                        </label>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <PrimaryButton
                          onClick={() => save({
                            credentials: {
                              [p]: {
                                ...(draft.client_id ? { client_id: draft.client_id } : {}),
                                ...(draft.client_secret ? { client_secret: draft.client_secret } : {}),
                              },
                            },
                          })}
                          disabled={saving || !live || (!draft.client_id && !draft.client_secret)}
                        >
                          Save {PLATFORM_LABELS[p]} credentials
                        </PrimaryButton>
                        {(c?.client_id.configured || c?.client_secret.configured) && (
                          <SecondaryButton onClick={() => save({ credentials: { [p]: { client_id: "", client_secret: "" } } })} disabled={saving || !live}>Clear credentials</SecondaryButton>
                        )}
                        <SecondaryButton onClick={() => test(p)} disabled={testing === p || !live}>
                          {testing === p ? "Testing…" : "Test connection"}
                        </SecondaryButton>
                      </div>
                      {testResults[p] && <p className="text-sm text-neutral-300">{testResults[p]}</p>}
                    </div>
                  </div>
                );
              })}
            </div>
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
            <h2 className="mb-2 text-lg font-semibold">Environment</h2>
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between gap-2"><dt className="text-neutral-400">Data folder</dt><dd className="truncate font-mono text-xs">{view.data_dir}</dd></div>
              <div className="flex justify-between gap-2"><dt className="text-neutral-400">Secret storage</dt><dd>{view.secret_backend === "os-keyring" ? "OS keyring" : "Encrypted file (no OS keyring found — see docs)"}</dd></div>
              <div className="flex justify-between gap-2">
                <dt className="text-neutral-400">FFmpeg</dt>
                <dd>
                  {view.ffmpeg.found ? `Found (${view.ffmpeg.path})` : "Not found — clipping needs it. "}
                  {!view.ffmpeg.found && (
                    <a className="text-sky-400 underline" href={view.ffmpeg.install_url} target="_blank" rel="noreferrer">Install FFmpeg</a>
                  )}
                </dd>
              </div>
            </dl>
            <p className="mt-3 text-sm text-neutral-400">
              Channels are connected on the <a className="text-sky-400 underline" href="/channels">Channels page</a> — status, scopes, daily caps, and Connect / Disconnect live there.
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
        </>
      ) : (
        <Card>
          <p className="text-sm text-neutral-400">The API did not answer. Start the server, then reload this page.</p>
        </Card>
      )}
    </div>
  );
}
