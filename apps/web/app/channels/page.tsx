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
  apiGet, disconnectChannel, getAnalytics, getChannels, latestSnapshotPerPlatform,
  refreshChannel, startOAuth,
  type AnalyticsSnapshot, type ChannelStatus,
} from "@/lib/api";
import { demoAnalytics } from "@/lib/mock";
import { Card, DemoBadge, EmptyState, fmtCompact, fmtNum, PlatformDot, platformLabel, PrimaryButton, SecondaryButton, Skeleton } from "@/components/ui";

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
}));

function statusPill(status: ChannelStatus["status"]): string {
  if (status === "connected") return "bg-emerald-500/15 text-emerald-300";
  if (status === "expired") return "bg-red-500/15 text-red-300";
  return "bg-amber-500/15 text-amber-300";
}

function statusLabel(status: ChannelStatus["status"]): string {
  if (status === "connected") return "Connected";
  if (status === "expired") return "Expired — reconnect";
  return "Not connected";
}

export default function ChannelsPage() {
  const [channels, setChannels] = useState<ChannelStatus[] | null>(null);
  const [analytics, setAnalytics] = useState<AnalyticsSnapshot[] | null>(null);
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [fetched, fetchedAnalytics, health] = await Promise.all([
      getChannels(), getAnalytics(), apiGet<unknown>("/health"),
    ]);
    const isLive = health !== null;
    setLive(isLive);
    if (!isLive) { setChannels(DEMO_CHANNELS); setAnalytics(demoAnalytics); }
    else { setChannels(fetched ?? []); setAnalytics(fetchedAnalytics ?? []); }
  }, []);

  useEffect(() => {
    let cancelled = false;
    load().catch(() => { if (!cancelled) { setChannels(DEMO_CHANNELS); setAnalytics(demoAnalytics); } });
    return () => { cancelled = true; };
  }, [load]);

  async function connect(platform: string) {
    setBusy(platform);
    setNotice(null);
    const started = await startOAuth(platform);
    setBusy(null);
    if (!started) {
      setNotice(`${platformLabel(platform)} is not configured yet — save the app client id and secret in Settings first.`);
      return;
    }
    window.open(started.authorize_url, "_blank", "noopener");
    setNotice(`Opened ${platformLabel(platform)} in a new tab — grant access there, then come back and this page will show Connected (refresh the page after granting).`);
  }

  async function refresh(platform: string) {
    setBusy(platform);
    const res = await refreshChannel(platform);
    setBusy(null);
    if (!res.ok) { setNotice(res.error ?? "Refresh failed."); return; }
    setNotice(`${platformLabel(platform)} token refreshed.`);
    await load();
  }

  async function disconnect(platform: string) {
    setBusy(platform);
    const res = await disconnectChannel(platform);
    setBusy(null);
    if (!res.ok) { setNotice(res.error ?? "Disconnect failed."); return; }
    setNotice(`${platformLabel(platform)} disconnected.`);
    await load();
  }

  const loading = channels === null;
  const latest = latestSnapshotPerPlatform(analytics ?? []);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Channels</h1>
        {!live && !loading && <DemoBadge />}
        <span className="ml-auto text-sm text-neutral-400">{live ? "Live channel status from the API" : loading ? "Checking the API…" : "API unreachable — demo data"}</span>
      </div>

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
                <div className="flex items-center gap-2">
                  {card.avatar_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={card.avatar_url} alt="" className="h-6 w-6 rounded-full" />
                  ) : (
                    <PlatformDot platform={p} />
                  )}
                  <h2 className="text-lg font-semibold">{card.display_name}</h2>
                  <span className={`ml-auto rounded-full px-2 py-0.5 text-xs font-medium ${statusPill(card.status)}`}>
                    {statusLabel(card.status)}
                  </span>
                </div>

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
                  {connected || card.status === "expired" ? (
                    <>
                      <SecondaryButton onClick={() => connect(p)} disabled={busy === p}>
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
