// Channels (v0.3.0) — one card per platform from the real backend
// (GET /channels): connection status, account name/avatar, scopes
// granted, per-channel daily cap, and Connect / Reconnect / Disconnect.
//
// Status is never hardcoded and never faked: "expired" means the stored
// OAuth token is past expiry (Reconnect), "not connected" means no token
// at all. Where platform review/audit is pending the card says so
// plainly and offers the working fallback (manual-post pack / inbox
// draft). Tokens are handled server-side only — this UI never sees one.
//
// Analytics snapshots (GET /analytics) are shown underneath when they
// exist; demo fallback is labelled demo throughout.
"use client";
import { useCallback, useEffect, useState } from "react";
import {
  connectX, disconnectChannel, getAnalytics, getChannels, getXStatus,
  latestSnapshotPerPlatform, reconnectChannel, refreshChannel, startOAuth,
  type AnalyticsSnapshot, type ChannelStatus, type XStatus,
} from "@/lib/api";
import { demoAnalytics } from "@/lib/mock";
import { openExternal } from "@/lib/tauri";
import { Card, DemoBadge, EmptyState, fmtCompact, fmtNum, Avatar, Badge, PageHeader, PlatformDot, platformLabel, PrimaryButton, SecondaryButton, Skeleton } from "@/components/ui";
import { useServerStatus } from "@/components/ServerStatusProvider";

const PLATFORMS = ["youtube", "instagram", "facebook", "tiktok"] as const;

/** Demo channel cards shown only when the API is unreachable. */
const DEMO_CHANNELS: ChannelStatus[] = (["youtube", "instagram", "facebook", "tiktok"] as const).map((p) => ({
  platform: p,
  display_name: platformLabel(p),
  status: p === "youtube" ? "connected" : "not_connected",
  account_name: p === "youtube" ? "@clipforge-demo" : "",
  avatar_url: "",
  account_ref: "",
  scopes: [],
  daily_cap: p === "youtube" ? 6 : p === "instagram" ? 100 : p === "facebook" ? 30 : 5,
  expires_at: null,
  source: p === "youtube" ? "oauth" : "none",
  review_notice: p === "tiktok"
    ? "TikTok Content Posting API audit is pending: unaudited apps can only create private / inbox drafts."
    : "",
  fallback: "Manual-post pack: download the clip + SEO copy and post it yourself.",
  can_refresh: false,
  health: p === "youtube" ? "connected" : "not_connected",
  health_reason: p === "youtube" ? "Token valid (demo)." : "Not connected yet (demo).",
  health_next_step: "",
  expires_in_s: null,
  needs_refresh: false,
  last_error: null,
  last_checked_at: null,
  checked_at: null,
}));

export default function ChannelsPage() {
  const [channels, setChannels] = useState<ChannelStatus[] | null>(null);
  const [analytics, setAnalytics] = useState<AnalyticsSnapshot[] | null>(null);
  // Liveness is the shared server status (v0.9.3).
  const server = useServerStatus();
  const live = server.live;
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [xStatus, setXStatus] = useState<XStatus | null>(null);
  const [xLoaded, setXLoaded] = useState(false);

  const load = useCallback(async () => {
    const [fetched, fetchedAnalytics, fetchedX] = await Promise.all([
      getChannels(), getAnalytics(), getXStatus(),
    ]);
    const isLive = live;
    setXStatus(fetchedX);
    setXLoaded(true);
    if (!isLive) {
      if (server.status === "reconnecting") { setChannels(DEMO_CHANNELS); setAnalytics(demoAnalytics); }
    }
    else { setChannels(fetched ?? []); setAnalytics(fetchedAnalytics ?? []); }
    // `live` / `server.status` in deps: when the server comes back,
    // load changes identity and the mount effect below re-runs, so
    // this page recovers on its own.
  }, [live, server.status]);

  useEffect(() => {
    let cancelled = false;
    load().catch(() => { if (!cancelled && server.status === "reconnecting") { setChannels(DEMO_CHANNELS); setAnalytics(demoAnalytics); } });
    return () => { cancelled = true; };
  }, [load, server.status]);

  async function connect(platform: string) {
    setBusy(platform);
    setNotice(null);
    const started = await startOAuth(platform);
    setBusy(null);
    if (!started) {
      setNotice(`${platformLabel(platform)} is not configured yet — save the app client id and secret in Settings first.`);
      return;
    }
    // openExternal() opens the sign-in page in the system browser. A
    // plain window.open() opens NOTHING inside the Tauri desktop app —
    // v0.4.1 claimed the page was opened while nothing happened. Only
    // report "opened" when the open actually succeeded.
    const opened = await openExternal(started.authorize_url);
    if (!opened) {
      setNotice(`Could not open the ${platformLabel(platform)} sign-in page in your browser — copy this link and paste it into Chrome, grant access there, then come back and refresh this page: ${started.authorize_url}`);
      return;
    }
    setNotice(`Opened ${platformLabel(platform)} in your browser — grant access there, then come back and this page will show Connected (refresh the page after granting).`);
  }

  async function refresh(platform: string) {
    setBusy(platform);
    const res = await refreshChannel(platform);
    setBusy(null);
    if (!res.ok) { setNotice(res.error ?? "Refresh failed."); return; }
    setNotice(`${platformLabel(platform)} token refreshed.`);
    await load();
  }

  // Reconnect (v0.9.0): try a silent token refresh first — that fixes
  // the common case (expired token, live refresh token) without making
  // the user sign in again. Only when the server answers 409 (the
  // stored grant is dead) fall through to the full OAuth sign-in.
  async function reconnect(platform: string) {
    setBusy(platform);
    setNotice(null);
    const res = await reconnectChannel(platform);
    setBusy(null);
    if (res.ok) {
      setNotice(`${platformLabel(platform)} reconnected — token refreshed.`);
      await load();
      return;
    }
    if (res.status === 409) {
      setNotice(`The saved ${platformLabel(platform)} sign-in has expired — opening the sign-in page to connect again.`);
      await connect(platform);
      return;
    }
    setNotice(res.error ?? "Reconnect failed.");
  }

  async function disconnect(platform: string) {
    setBusy(platform);
    const res = await disconnectChannel(platform);
    setBusy(null);
    if (!res.ok) { setNotice(res.error ?? "Disconnect failed."); return; }
    setNotice(`${platformLabel(platform)} disconnected.`);
    await load();
  }

  // X Reconnect (v0.9.0): same silent-refresh-first rule as the other
  // platforms — X refresh works now (HTTP Basic auth); only a dead
  // grant falls through to the full PKCE sign-in.
  async function reconnectXAccount() {
    setBusy("x");
    setNotice(null);
    const res = await reconnectChannel("x");
    setBusy(null);
    if (res.ok) {
      setNotice("X reconnected — token refreshed.");
      await load();
      return;
    }
    if (res.status === 409) {
      setNotice("The saved X sign-in has expired — opening the X sign-in page to connect again.");
      await connectXAccount();
      return;
    }
    setNotice(res.error ?? "X reconnect failed.");
  }

  async function connectXAccount() {
    setBusy("x");
    setNotice(null);
    const { url, error } = await connectX();
    setBusy(null);
    if (!url) {
      setNotice(error ?? "X connect is not available in this build yet — the X service has not landed on the server.");
      return;
    }
    const opened = await openExternal(url);
    if (!opened) {
      setNotice(`Could not open the X sign-in page in your browser — copy this link and paste it into Chrome, grant access there, then come back and refresh this page: ${url}`);
      return;
    }
    setNotice("Opened X in your browser — grant access there, then come back and this page will show Connected (refresh the page after granting).");
  }

  const loading = channels === null;
  const latest = latestSnapshotPerPlatform(analytics ?? []);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Channels"
        intro="Connect your YouTube, Instagram, Facebook and TikTok accounts. ClipForge never sees your password — sign-in happens on the platform's own page."
        actions={
          <>
            {!live && !loading && <DemoBadge />}
            <span className="text-sm text-neutral-400">{live ? "Live channel status from the API" : loading ? "Checking the API…" : "Server not answering — demo data"}</span>
          </>
        }
      />

      {notice && (
        <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>
      )}

      {loading ? (
        <div className="grid gap-4 md:grid-cols-2"><Skeleton className="h-64" /><Skeleton className="h-64" /><Skeleton className="h-64" /><Skeleton className="h-64" /></div>
      ) : (channels ?? []).length === 0 ? (
        <EmptyState title="No channel data" body="The API answered but returned no channels. Check the server is running the v0.3.0 build." action={<PrimaryButton href="/settings">Open Settings</PrimaryButton>} />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {PLATFORMS.map((p) => {
            const card = (channels ?? []).find((c) => c.platform === p);
            if (!card) return null;
            const snap = latest.get(p);
            const connected = card.status === "connected";
            return (
              <Card key={p}>
                <div className="flex items-center gap-3">
                  <Avatar name={card.account_name || card.display_name} src={card.avatar_url} platform={p} />
                  <div className="min-w-0 flex-1">
                    <h2 className="flex items-center gap-2 text-lg font-semibold text-neutral-50">
                      <PlatformDot platform={p} /> {card.display_name}
                    </h2>
                    <p className="truncate text-sm text-neutral-400">{card.account_name || card.account_ref || "Not connected yet"}</p>
                  </div>
                  {card.health === "connected" ? <Badge tone="success">● Connected</Badge>
                    : card.health === "expiring_soon" ? <Badge tone="warning">● Expiring soon</Badge>
                    : card.health === "disconnected" ? <Badge tone="danger">● Disconnected</Badge>
                    : <Badge tone="warning">○ Not connected</Badge>}
                </div>

                {(card.health === "expiring_soon" || card.health === "disconnected") && (
                  <div className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
                    <p className="text-xs leading-relaxed text-amber-100/90">{card.health_reason}</p>
                    {card.health_next_step && (
                      <p className="mt-1 text-xs leading-relaxed text-neutral-400">{card.health_next_step}</p>
                    )}
                  </div>
                )}

                <dl className="mt-3 space-y-1 text-sm">
                  <div className="flex justify-between gap-2"><dt className="text-neutral-400">Account</dt><dd className="truncate">{card.account_name || card.account_ref || "—"}</dd></div>
                  <div className="flex justify-between gap-2"><dt className="text-neutral-400">Daily cap</dt><dd>{card.daily_cap} posts/day</dd></div>
                  <div className="flex justify-between gap-2"><dt className="text-neutral-400">Token</dt><dd>{card.source === "oauth" ? "OAuth (encrypted on server)" : card.source === "env" ? "Environment variable" : "None"}</dd></div>
                  {card.expires_at && (
                    <div className="flex justify-between gap-2"><dt className="text-neutral-400">Token expires</dt><dd>{new Date(card.expires_at).toLocaleString()}</dd></div>
                  )}
                </dl>

                {card.scopes.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1">
                    {card.scopes.map((s) => (
                      <span key={s} className="rounded-full bg-neutral-800 px-2 py-0.5 font-mono text-[11px] text-neutral-300">{s}</span>
                    ))}
                  </div>
                )}

                {snap ? (
                  <div className="mt-4 grid grid-cols-3 gap-3 border-t border-neutral-800 pt-4">
                    <div><p className="text-xs text-neutral-500">Views</p><p className="mt-0.5 text-lg font-bold">{fmtCompact(snap.views)}</p></div>
                    <div><p className="text-xs text-neutral-500">{p === "youtube" ? "Subscribers" : "Followers"}</p><p className="mt-0.5 text-lg font-bold">{fmtCompact(snap.followers)}</p></div>
                    <div>
                      <p className="text-xs text-neutral-500">{p === "youtube" ? "Watch hours" : "Viewed %"}</p>
                      <p className="mt-0.5 text-lg font-bold">{p === "youtube" ? fmtNum(Math.round(snap.watch_hours)) : snap.viewed_pct !== null ? `${Math.round(snap.viewed_pct)}%` : "—"}</p>
                    </div>
                  </div>
                ) : (
                  <p className="mt-4 border-t border-neutral-800 pt-4 text-sm text-neutral-400">No analytics snapshot yet — stats appear after the first ingest.</p>
                )}
                {snap && snap.signals.length > 0 && (
                  <ul className="mt-3 space-y-1">
                    {snap.signals.slice(0, 2).map((s, i) => (
                      <li key={i} className="rounded-md bg-neutral-950 px-3 py-2 text-xs text-neutral-300">{s}</li>
                    ))}
                  </ul>
                )}

                {card.review_notice && (
                  <div className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
                    <p className="text-xs leading-relaxed text-amber-100/90">{card.review_notice}</p>
                    <p className="mt-2 text-xs leading-relaxed text-neutral-400">{card.fallback}</p>
                  </div>
                )}

                <div className="mt-4 flex flex-wrap gap-2">
                  {connected || card.status === "expired" || card.health === "disconnected" || card.health === "expiring_soon" ? (
                    <>
                      <SecondaryButton onClick={() => reconnect(p)} disabled={busy === p}>
                        Reconnect
                      </SecondaryButton>
                      {card.can_refresh && (
                        <SecondaryButton onClick={() => refresh(p)} disabled={busy === p}>Refresh token</SecondaryButton>
                      )}
                      <SecondaryButton onClick={() => disconnect(p)} disabled={busy === p}>Disconnect</SecondaryButton>
                    </>
                  ) : (
                    <PrimaryButton href={`/channels/connect?platform=${p}`}>Connect</PrimaryButton>
                  )}
                </div>
                {!live && <p className="mt-2 text-xs text-neutral-500">Connect needs the live API — start the server first.</p>}
              </Card>
            );
          })}
        </div>
      )}

      {/* X (Twitter) — v0.8.0. Status from GET /x/status, connect via
          POST /x/connect. X API access is paid/restricted: the card says
          so plainly and never shows a fake connected state. */}
      <Card>
        <div className="flex items-center gap-3">
          <Avatar name="X (Twitter)" platform="x" />
          <div className="min-w-0 flex-1">
            <h2 className="flex items-center gap-2 text-lg font-semibold text-neutral-50">
              <PlatformDot platform="x" /> X (Twitter)
            </h2>
            <p className="truncate text-sm text-neutral-400">{xStatus?.account || "Not connected yet"}</p>
          </div>
          {!xLoaded ? <Skeleton className="h-6 w-24" />
            : xStatus === null ? <Badge tone="neutral">Not available in this build</Badge>
            : xStatus.connected ? <Badge tone="success">● Connected</Badge>
            : <Badge tone="warning">○ Not connected</Badge>}
        </div>
        <p className="mt-3 text-sm text-neutral-400">
          {xStatus?.note || "X posting goes through the X API, which is a paid and restricted tier — free X API access does not include posting. Connecting may require an approved developer account and a paid X API plan."}
        </p>
        <p className="mt-2 text-sm text-neutral-400">
          Until X approves the app and a paid tier is active, clips for X stay as drafts / manual-post packs — ClipForge will not claim a post went out when it did not.
        </p>
        <div className="mt-4 flex flex-wrap gap-2">
          {xStatus?.connected ? (
            <SecondaryButton onClick={reconnectXAccount} disabled={busy === "x"}>Reconnect X</SecondaryButton>
          ) : (
            <PrimaryButton onClick={connectXAccount} disabled={busy === "x" || !live}>
              {busy === "x" ? "Opening…" : "Connect X"}
            </PrimaryButton>
          )}
          {!live && <span className="self-center text-xs text-neutral-500">Connect needs the live API — start the server first.</span>}
        </div>
      </Card>

      <Card>
        <h2 className="mb-2 text-lg font-semibold">How connecting works</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-neutral-400">
          <li>The Connect wizard walks you through it: create the platform app, paste its client id and secret, then press Connect. The redirect URI shown there must be registered in the platform app.</li>
          <li>Connect opens the platform&apos;s own sign-in page. ClipForge never asks for your password or cookies; the token is stored encrypted on the server.</li>
          <li>Each platform gates public posting behind its own review/audit; until that passes, ClipForge stays in draft / dry-run mode for that platform.</li>
        </ul>
        <div className="mt-4"><PrimaryButton href="/channels/connect">Open the Connect wizard</PrimaryButton></div>
      </Card>
    </div>
  );
}
