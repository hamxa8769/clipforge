// Guided Connect wizard (v0.4.0) — /channels/connect?platform=youtube
//
// The user complaint was that connecting meant staring at raw settings
// JSON. This wizard replaces that with five plain steps:
//   1. Pick a platform.
//   2. Create the developer app — exact official links, click-by-click
//      steps, redirect URI with a Copy button (from the API, never a
//      hardcoded localhost guess — see lib/api.ts getRedirectUri).
//   3. Paste the Client ID / Client Secret into masked fields, Save.
//   4. Big Connect button — opens the platform's own sign-in page
//      (existing GET /auth/{platform}/start + callback); this page
//      polls GET /channels until the account shows Connected.
//   5. Test Connection (existing POST /settings/test-connection/{service})
//      — connected account name + status in plain words, or the exact
//      error in plain language with the next step.
//
// Connected = green Connected + account name. Unconnected = the next
// action, never a JSON blob. Tokens are handled server-side only.
"use client";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  explainConnectionError, getChannels, getConnectGuide, getRedirectUri, getSettings, saveSettings, startOAuth, testConnection,
  type AppSettingsView, type ChannelStatus, type ConnectGuide,
} from "@/lib/api";
import { guideFor, type PlatformGuide } from "@/lib/connect";
import { openExternal } from "@/lib/tauri";
import { Card, EmptyState, ExternalLink, PlatformDot, platformLabel, PrimaryButton, SecondaryButton, Skeleton } from "@/components/ui";

const PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"] as const;
const STEPS = ["Pick a platform", "Create the app", "Paste the keys", "Connect", "Test"] as const;

function Stepper({ step }: { step: number }) {
  return (
    <ol className="flex flex-wrap gap-2">
      {STEPS.map((label, i) => (
        <li key={label} className={`rounded-full px-3 py-1 text-xs font-medium ${i + 1 === step ? "bg-amber-400 text-neutral-950" : i + 1 < step ? "bg-emerald-500/20 text-emerald-300" : "bg-neutral-800 text-neutral-400"}`}>
          {i + 1 < step ? "✓ " : ""}{i + 1}. {label}
        </li>
      ))}
    </ol>
  );
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* clipboard unavailable */ }
  }
  return <SecondaryButton onClick={copy}>{copied ? "Copied ✓" : label}</SecondaryButton>;
}

function WizardInner() {
  const search = useSearchParams();
  const initial = search.get("platform");
  // ?step=N deep-links straight to that step (e.g. ?step=5 for the test
  // step). With no ?step=, a preselected ?platform=… starts at step 2
  // ("Create the app") — the old code clamped a missing step to 1, so
  // the per-platform Connect buttons on the Channels page
  // (/channels/connect?platform=youtube) landed on step 1 and made the
  // user pick the platform a second time (v0.4.1 fix).
  const stepParam = search.get("step");
  const initialStep = stepParam ? Math.min(5, Math.max(1, Number(stepParam) || 1)) : 0;
  const [platform, setPlatform] = useState<string>(PLATFORMS.includes(initial as (typeof PLATFORMS)[number]) ? (initial as string) : "");
  const [step, setStep] = useState<number>(platform && initialStep ? initialStep : platform ? 2 : 1);
  const [guide, setGuide] = useState<PlatformGuide | null>(null);
  const [serverGuide, setServerGuide] = useState<ConnectGuide | null>(null);
  const [redirect, setRedirect] = useState<{ uri: string; authoritative: boolean } | null>(null);
  const [redirectLoading, setRedirectLoading] = useState(false);
  const [settings, setSettings] = useState<AppSettingsView | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [live, setLive] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [oauthOpened, setOauthOpened] = useState(false);
  const [oauthUrl, setOauthUrl] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [channel, setChannel] = useState<ChannelStatus | null>(null);
  const [testState, setTestState] = useState<{ ok: boolean; summary: string; nextStep: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadSettings = useCallback(async () => {
    setSettingsLoading(true);
    const fetched = await getSettings();
    setLive(fetched !== null);
    setSettings(fetched);
    setSettingsLoading(false);
  }, []);

  useEffect(() => { loadSettings().catch(() => setSettingsLoading(false)); }, [loadSettings]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  async function loadGuide(p: string) {
    // Local guide shows immediately; the server guide (GET
    // /connect/guide/{platform}) replaces it when the server has it —
    // the server's steps and redirect URI are authoritative, and its
    // credential flags say which keys are already saved.
    setGuide(guideFor(p));
    setServerGuide(null);
    setRedirectLoading(true);
    try {
      const g = await getConnectGuide(p);
      setServerGuide(g);
      if (g) {
        setRedirect({ uri: g.redirect_uri, authoritative: true });
      } else {
        setRedirect(await getRedirectUri(p));
      }
    } catch {
      setRedirect(null);
    } finally {
      setRedirectLoading(false);
    }
  }

  function pick(p: string) {
    setPlatform(p);
    loadGuide(p);
    setStep(2);
    setNotice(null);
    setError(null);
  }

  useEffect(() => {
    // Preselected platform (?platform=…): load its guide without
    // resetting the step — ?step=5 deep-links straight to the test step.
    if (platform) loadGuide(platform);
    // Run once on mount for a preselected platform.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function saveCreds() {
    if (!platform) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    const res = await saveSettings({
      credentials: {
        [platform]: {
          ...(clientId.trim() ? { client_id: clientId.trim() } : {}),
          ...(clientSecret.trim() ? { client_secret: clientSecret.trim() } : {}),
        },
      },
    });
    setSaving(false);
    if (!res.ok) { setError(res.error ?? "Could not save the credentials."); return; }
    setClientId("");
    setClientSecret("");
    setNotice("Saved. The keys are stored encrypted on the server — this page never shows them again.");
    await loadSettings();
    setStep(4);
  }

  async function connect() {
    if (!platform) return;
    setError(null);
    setNotice(null);
    setOauthUrl(null);
    const started = await startOAuth(platform);
    if (!started) {
      setError("ClipForge could not start the sign-in — the client ID or secret may be missing or wrong. Go back a step and check the keys.");
      return;
    }
    // openExternal() opens the sign-in page in the system browser. A
    // plain window.open() opens NOTHING inside the Tauri desktop app
    // (the WebView has no new-window handler) — v0.4.1 claimed the page
    // was opened while nothing happened. Only report "opened" when the
    // open actually succeeded; on failure show the link itself so the
    // user can paste it into Chrome by hand.
    const opened = await openExternal(started.authorize_url);
    if (!opened) {
      setOauthUrl(started.authorize_url);
      setError(`ClipForge could not open the ${platformLabel(platform)} sign-in page in your browser. Copy the link below and paste it into Chrome, sign in there, then press "I granted access — check now".`);
      return;
    }
    setOauthOpened(true);
    setWaiting(true);
    setNotice(`Signed-in page opened in your browser. Sign in on ${platformLabel(platform)} and grant access, then come back here — this page checks for the connection automatically.`);
    pollRef.current = setInterval(async () => {
      const channels = await getChannels();
      const found = channels?.find((c) => c.platform === platform);
      if (found) {
        setChannel(found);
        if (found.status === "connected") {
          stopPolling();
          setWaiting(false);
          setStep(5);
        }
      }
    }, 3000);
  }

  async function checkNow() {
    if (!platform) return;
    const channels = await getChannels();
    const found = channels?.find((c) => c.platform === platform);
    setChannel(found ?? null);
    if (found?.status === "connected") {
      stopPolling();
      setWaiting(false);
      setStep(5);
    } else {
      setNotice("Not connected yet — finish signing in on the platform's page, then press this again.");
    }
  }

  async function test() {
    if (!platform) return;
    setTesting(true);
    setTestState(null);
    const res = await testConnection(platform);
    setTesting(false);
    const detail = (res.data as { message?: string } | null)?.message;
    if (res.ok) {
      const account = channel?.account_name || channel?.account_ref || "";
      setTestState({
        ok: true,
        summary: `Connected${account ? ` as ${account}` : ""}. ClipForge can reach ${platformLabel(platform)} with the saved sign-in.`,
        nextStep: channel?.review_notice
          ? `${platformLabel(platform)} still needs its own review before public posting — until then clips save as drafts.`
          : "You can now approve clips for this channel. Nothing publishes without your approval.",
      });
    } else {
      const explained = explainConnectionError(detail ?? res.error);
      setTestState({ ok: false, summary: explained.summary, nextStep: explained.nextStep });
    }
  }

  const creds = platform ? settings?.credentials[platform] : undefined;
  // Saved-key flags: the server guide's presence flags win when the
  // server provides them; otherwise the masked settings view decides.
  const serverCredSaved = (key: string) => serverGuide?.credentials.find((c) => c.key === key)?.saved;
  const idConfigured = serverGuide ? serverCredSaved("client_id") === true : creds?.client_id.configured === true;
  const secretConfigured = serverGuide ? serverCredSaved("client_secret") === true : creds?.client_secret.configured === true;
  const afterNote = serverGuide
    ? [serverGuide.review_notice, serverGuide.fallback].filter(Boolean).join(" ")
    : guide?.afterConnectNote ?? "";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Connect a channel</h1>
        <p className="mt-1 text-sm text-neutral-400">Five steps, about five minutes per platform. ClipForge never asks for your password — you sign in on the platform&apos;s own page.</p>
      </div>

      <Stepper step={step} />

      {error && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-200" role="alert">{error}</div>
      )}
      {notice && (
        <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>
      )}

      {!live && !settingsLoading && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-100">
          The server is not answering, so this wizard cannot save keys or start a sign-in. Start the server, then reload this page.
        </div>
      )}

      {/* Step 1 — pick a platform */}
      {step === 1 && (
        <div className="grid gap-4 sm:grid-cols-2">
          {PLATFORMS.map((p) => (
            <button key={p} onClick={() => pick(p)} className="rounded-xl border border-neutral-800 bg-neutral-900 p-5 text-left hover:border-amber-400/60">
              <span className="flex items-center gap-2"><PlatformDot platform={p} /><span className="text-lg font-semibold">{platformLabel(p)}</span></span>
              <span className="mt-2 block text-sm text-neutral-400">{guideFor(p).whatYouNeed}</span>
              <span className="mt-3 block text-sm font-medium text-amber-300">Set up {platformLabel(p)} →</span>
            </button>
          ))}
        </div>
      )}

      {/* Step 2 — create the developer app */}
      {step === 2 && platform && guide && (
        <Card>
          <h2 className="text-lg font-semibold">Step 2 — Create the {platformLabel(platform)} app</h2>
          <p className="mt-1 text-sm text-neutral-400">Do this once in {serverGuide?.console_name ?? guide.consoleName}. {guide.whatYouNeed}</p>
          {(serverGuide?.console_url || guide.consoleUrl) && (
            <p className="mt-3">
              <ExternalLink className="inline-flex items-center justify-center rounded-md bg-neutral-800 px-3 py-1.5 text-sm font-medium text-neutral-200 hover:bg-neutral-700" href={serverGuide?.console_url || guide.consoleUrl}>
                Open {serverGuide?.console_name ?? guide.consoleName} ↗
              </ExternalLink>
            </p>
          )}
          <ol className="mt-4 space-y-4">
            {(serverGuide ? serverGuide.steps.map((s) => ({ title: s.title, detail: s.detail, link: undefined })) : guide.steps).map((s, i) => (
              <li key={i} className="flex gap-3">
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-neutral-800 text-xs font-bold">{i + 1}</span>
                <div>
                  <p className="text-sm font-medium">{s.title}</p>
                  <p className="mt-0.5 text-sm leading-relaxed text-neutral-400">{s.detail}</p>
                  {s.link && <ExternalLink className="mt-1 inline-block text-sm text-sky-400 underline" href={s.link.url}>{s.link.label} ↗</ExternalLink>}
                </div>
              </li>
            ))}
          </ol>

          <div className="mt-6 rounded-lg border border-neutral-800 bg-neutral-950 p-4">
            <p className="text-sm font-medium">Redirect URI — paste this into the app in step 5 above</p>
            {redirectLoading ? (
              <Skeleton className="mt-2 h-9" />
            ) : redirect ? (
              <>
                <span className="mt-2 flex gap-2">
                  <input readOnly className="w-full rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 font-mono text-xs" value={redirect.uri} aria-label="Redirect URI" />
                  <CopyButton text={redirect.uri} label="Copy" />
                </span>
                <p className="mt-2 text-xs text-neutral-500">
                  {redirect.authoritative
                    ? "This is the exact address the server will use — copied from the server itself."
                    : "This address is built from the server you are connected to. After you save the keys, the wizard double-checks it against the server's own answer."}
                </p>
              </>
            ) : (
              <p className="mt-2 text-sm text-neutral-400">Could not work out the redirect address — check the server is running and reload.</p>
            )}
          </div>

          <div className="mt-5 flex flex-wrap gap-2">
            <PrimaryButton onClick={() => setStep(3)} disabled={!live}>I created the app — continue</PrimaryButton>
            <SecondaryButton onClick={() => setStep(1)}>Change platform</SecondaryButton>
          </div>
        </Card>
      )}

      {/* Step 3 — paste the keys */}
      {step === 3 && platform && (
        <Card>
          <h2 className="text-lg font-semibold">Step 3 — Paste the keys</h2>
          <p className="mt-1 text-sm text-neutral-400">Copy them from the {platformLabel(platform)} app you just created. They are stored encrypted on the server — this page only ever shows the last few characters.</p>
          {settingsLoading ? (
            <div className="mt-4 space-y-3"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>
          ) : (
            <div className="mt-4 space-y-3">
              <p className="text-sm text-neutral-300">
                Client ID: {idConfigured ? <span className="font-mono">{creds?.client_id.masked || "••••••••"}</span> : <span className="text-neutral-500">not set yet</span>}
                {" · "}Client secret: {secretConfigured ? <span className="font-mono">{creds?.client_secret.masked || "••••••••"}</span> : <span className="text-neutral-500">not set yet</span>}
              </p>
              <label className="block text-sm">
                <span className="text-neutral-400">Client ID {idConfigured ? "(leave blank to keep the saved one)" : ""}</span>
                <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="Paste the client ID" autoComplete="off" />
              </label>
              <label className="block text-sm">
                <span className="text-neutral-400">Client secret {secretConfigured ? "(leave blank to keep the saved one)" : ""}</span>
                <input type="password" className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} placeholder="Paste the client secret" autoComplete="off" />
              </label>
              <div className="flex flex-wrap gap-2">
                <PrimaryButton onClick={() => saveCreds()} disabled={saving || !live || (!clientId.trim() && !clientSecret.trim() && !idConfigured && !secretConfigured)}>
                  {saving ? "Saving…" : "Save keys"}
                </PrimaryButton>
                <SecondaryButton onClick={() => setStep(2)}>Back</SecondaryButton>
              </div>
              {idConfigured && secretConfigured && (
                <p className="text-sm text-neutral-400">Keys are already saved for {platformLabel(platform)} — you can press Save keys without pasting anything to continue, or paste new ones to replace them.</p>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Step 4 — connect */}
      {step === 4 && platform && (
        <Card>
          <h2 className="text-lg font-semibold">Step 4 — Connect {platformLabel(platform)}</h2>
          <p className="mt-1 text-sm text-neutral-400">This opens {platformLabel(platform)}&apos;s own sign-in page in your browser. Sign in there and grant access — ClipForge never sees your password. The token is stored encrypted on the server.</p>
          <div className="mt-5 text-center">
            <button
              onClick={() => connect()}
              disabled={!live || waiting}
              className="rounded-lg bg-amber-400 px-8 py-3 text-base font-bold text-neutral-950 hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Connect {platformLabel(platform)}
            </button>
          </div>
          {oauthUrl && !oauthOpened && (
            <div className="mt-5 space-y-2 text-center">
              <p className="text-sm text-neutral-300">The sign-in page did not open automatically — copy this link into your browser:</p>
              <span className="flex gap-2">
                <input readOnly className="w-full rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 font-mono text-xs" value={oauthUrl} aria-label="Sign-in link" />
                <CopyButton text={oauthUrl} label="Copy" />
              </span>
              <p><SecondaryButton onClick={() => connect()}>Try opening the sign-in page again</SecondaryButton></p>
            </div>
          )}
          {oauthOpened && (
            <div className="mt-5 space-y-3 text-center">
              <p className="text-sm text-neutral-300">{waiting ? "Waiting for you to grant access on the platform's page…" : "Checking…"}</p>
              <SecondaryButton onClick={() => checkNow()}>I granted access — check now</SecondaryButton>
              <p className="text-xs text-neutral-500">If the sign-in tab closed without granting access, press Connect again.</p>
            </div>
          )}
          {channel && channel.status !== "connected" && oauthOpened && (
            <p className="mt-4 text-center text-sm text-neutral-400">Current status: {channel.status === "expired" ? "sign-in expired — reconnect" : "not connected yet"}.</p>
          )}
          <div className="mt-5 flex gap-2">
            <SecondaryButton onClick={() => setStep(3)}>Back to keys</SecondaryButton>
          </div>
        </Card>
      )}

      {/* Step 5 — test */}
      {step === 5 && platform && (
        <Card>
          <h2 className="text-lg font-semibold">Step 5 — Test the connection</h2>
          <p className="mt-1 text-sm text-neutral-400">ClipForge checks the saved sign-in against {platformLabel(platform)} and shows who is connected.</p>
          {channel && (
            <p className="mt-4 flex items-center gap-2 text-sm">
              <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300">Connected</span>
              <span>{channel.account_name || channel.account_ref || platformLabel(platform)}</span>
            </p>
          )}
          {afterNote && <p className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs leading-relaxed text-amber-100/90">{afterNote}</p>}
          <div className="mt-4 flex flex-wrap gap-2">
            <PrimaryButton onClick={() => test()} disabled={testing || !live}>{testing ? "Testing…" : "Test connection"}</PrimaryButton>
            <SecondaryButton href="/channels">Back to Channels</SecondaryButton>
          </div>
          {testState && (
            <div className={`mt-4 rounded-lg border p-4 ${testState.ok ? "border-emerald-500/40 bg-emerald-500/10" : "border-red-500/40 bg-red-500/10"}`}>
              <p className={`text-sm font-medium ${testState.ok ? "text-emerald-200" : "text-red-200"}`}>{testState.summary}</p>
              <p className="mt-1 text-sm text-neutral-300">{testState.nextStep}</p>
              {!testState.ok && <div className="mt-3"><SecondaryButton onClick={() => setStep(3)}>Check the keys again</SecondaryButton></div>}
            </div>
          )}
          {testState?.ok && (
            <div className="mt-4"><PrimaryButton href="/channels">Done — back to Channels</PrimaryButton></div>
          )}
        </Card>
      )}

      {step > 1 && (
        <p className="text-center text-xs text-neutral-500">
          Stuck? Double-check the redirect URI matches exactly — no extra spaces — or start over from step 1.
        </p>
      )}
    </div>
  );
}

export default function ConnectWizardPage() {
  return (
    <Suspense fallback={<div className="space-y-4"><Skeleton className="h-10 w-64" /><Skeleton className="h-64" /></div>}>
      <WizardInner />
    </Suspense>
  );
}
