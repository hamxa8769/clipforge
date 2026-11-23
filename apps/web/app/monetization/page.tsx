// Client component: monetization + liveness are fetched in the browser so
// this page can be statically exported. Same UI, same demo fallback + badge.
//
// v0.3.0: progress is computed with REAL metrics — the latest analytics
// snapshot per platform is passed into GET /monetization?platform=…
// (subscribers / followers / watch_hours / shorts_views / views_30d).
// The bare /monetization endpoint defaults every metric to 0, which
// previously made every live channel read 0% here.
//
// v0.8.0: revenue table (GET /monetization/revenue — views, RPM and
// earnings per platform, each labelled with its basis: "from platform
// API" or "not reported by API"), an affiliate-link inserter
// (POST /monetization/affiliate) and A/B title/thumbnail tests
// (POST /monetization/ab, GET /monetization/ab/{clip_id}). Earnings are
// only ever shown when the platform API reports them — nothing is
// invented, and an empty state says so plainly.
"use client";
import { useEffect, useState } from "react";
import {
  createAbTest, getAbResults, getAnalytics, getMonetizationFor, getRevenue,
  addAffiliateLink, latestSnapshotPerPlatform,
  type AbVariant, type Monetization, type RevenueRow,
} from "@/lib/api";
import { demoAnalytics, demoMonetization } from "@/lib/mock";
import { Card, CardHeader, DemoBadge, EmptyState, ErrorBanner, PrimaryButton, ProgressBar, SecondaryButton, Skeleton } from "@/components/ui";
import { useServerStatus } from "@/components/ServerStatusProvider";

function fmt(n: number) { return n.toLocaleString(); }

const PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"] as const;

export default function MonetizationPage() {
  const [items, setItems] = useState<Monetization[] | null>(null);
  // Liveness is the shared server status (v0.9.3).
  const server = useServerStatus();
  const live = server.live;
  const [revenue, setRevenue] = useState<RevenueRow[] | null>(null);
  const [revenueLoaded, setRevenueLoaded] = useState(false);

  // Affiliate inserter.
  const [affClipId, setAffClipId] = useState("");
  const [affUrl, setAffUrl] = useState("");
  const [affBusy, setAffBusy] = useState(false);
  const [affNotice, setAffNotice] = useState<string | null>(null);

  // A/B test.
  const [abClipId, setAbClipId] = useState("");
  const [abTitleA, setAbTitleA] = useState("");
  const [abTitleB, setAbTitleB] = useState("");
  const [abThumbB, setAbThumbB] = useState("");
  const [abBusy, setAbBusy] = useState(false);
  const [abNotice, setAbNotice] = useState<string | null>(null);
  const [abResults, setAbResults] = useState<AbVariant[] | null>(null);
  const [abLoading, setAbLoading] = useState(false);

  // Re-runs when liveness flips, so the page recovers on its own
  // when the server comes back.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetchedAnalytics] = await Promise.all([
        getAnalytics(),
      ]);
      if (cancelled) return;
      const isLive = live;
      if (!isLive) {
        if (server.status === "reconnecting") { setItems(demoMonetization); setRevenueLoaded(true); }
        return;
      }
      const latest = latestSnapshotPerPlatform(fetchedAnalytics ?? []);
      const rows: Monetization[] = [];
      for (const p of PLATFORMS) {
        const s = latest.get(p);
        if (!s) continue;
        const r = await getMonetizationFor(p, {
          subscribers: s.followers, followers: s.followers,
          watch_hours: s.watch_hours, shorts_views: s.shorts_views_90d, views_30d: s.views,
        });
        if (r) rows.push(...r);
      }
      if (!cancelled) setItems(rows);
      const rev = await getRevenue();
      if (!cancelled) { setRevenue(rev); setRevenueLoaded(true); }
    })();
    return () => {
      cancelled = true;
    };
  }, [live, server.status]);

  async function onAddAffiliate() {
    if (!affClipId.trim() || !affUrl.trim()) { setAffNotice("Enter a clip ID and an affiliate URL first."); return; }
    setAffBusy(true);
    setAffNotice(null);
    const res = await addAffiliateLink(affClipId.trim(), affUrl.trim());
    setAffBusy(false);
    setAffNotice(res.ok ? "Affiliate link attached to the clip — it will appear in the clip's description when published." : (res.error ?? "Could not attach the affiliate link. The monetization service may not be installed in this build yet."));
  }

  async function onCreateAb() {
    if (!abClipId.trim() || !abTitleA.trim() || !abTitleB.trim()) { setAbNotice("Enter a clip ID and two titles first."); return; }
    setAbBusy(true);
    setAbNotice(null);
    const res = await createAbTest(abClipId.trim(), [
      { title: abTitleA.trim() },
      { title: abTitleB.trim(), ...(abThumbB.trim() ? { thumbnail: abThumbB.trim() } : {}) },
    ]);
    setAbBusy(false);
    if (!res.ok) { setAbNotice(res.error ?? "Could not create the A/B test. The monetization service may not be installed in this build yet."); return; }
    setAbNotice("A/B test created — results appear below once the variants have been shown.");
    await onLoadAb();
  }

  async function onLoadAb() {
    if (!abClipId.trim()) { setAbNotice("Enter a clip ID first."); return; }
    setAbLoading(true);
    const results = await getAbResults(abClipId.trim());
    setAbLoading(false);
    setAbResults(results);
    if (results === null) setAbNotice("A/B results are not available in this build yet.");
  }

  const loading = items === null;
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3"><h1 className="text-2xl font-bold">Monetization tracker</h1>{!live && !loading && <DemoBadge />}</div>
      <p className="text-sm text-neutral-400">Progress toward each platform&apos;s monetization threshold. Thresholds are from the Nov 2026 research — re-verify on the platform&apos;s own pages before relying on them. ClipForge never promises earnings.</p>
      {loading ? (
        <div className="grid gap-4 md:grid-cols-2"><Skeleton className="h-52" /><Skeleton className="h-52" /><Skeleton className="h-52" /><Skeleton className="h-52" /></div>
      ) : items.length === 0 ? (
        <Card><p className="text-neutral-400">No monetization data yet — connect a channel and the tracker will show progress against each platform&apos;s threshold.</p></Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {items.map((m) => (
            <Card key={`${m.platform}-${m.program}`} className="glass">
              <div className="flex items-center gap-3">
                <h2 className="text-lg font-semibold">{m.platform}</h2>
                <span className="text-sm text-neutral-400">{m.program}</span>
                <span className="ml-auto text-lg font-bold">{m.progress_pct}%</span>
              </div>
              <ProgressBar value={m.progress_pct} tone={m.progress_pct >= 100 ? "success" : "default"} className="mt-3" />
              <dl className="mt-4 space-y-1 text-sm">
                {Object.entries(m.threshold).map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-2">
                    <dt className="text-neutral-400">{k.replaceAll("_", " ")}</dt>
                    <dd>{fmt(m.current[k] ?? 0)} / {fmt(v)}</dd>
                  </div>
                ))}
              </dl>
              {m.note && <p className="mt-3 rounded-md bg-amber-500/10 p-3 text-xs text-amber-200">{m.note}</p>}
            </Card>
          ))}
        </div>
      )}

      {/* v0.8.0 — revenue from the platform APIs, with the basis labelled. */}
      <Card className="glass">
        <CardHeader title="Revenue" hint="Only what the platform APIs report — nothing invented" />
        {!revenueLoaded ? (
          <Skeleton className="h-32" />
        ) : revenue === null ? (
          <p className="text-sm text-neutral-400">Revenue reporting is not available in this build yet — the monetization service has not landed on the server.</p>
        ) : revenue.length === 0 ? (
          <EmptyState title="No revenue reported yet" body="No platform has reported earnings for your clips. Revenue appears here only when a platform's API reports it — ClipForge never estimates or invents earnings." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-neutral-500">
                  <th className="py-2 pr-3">Platform</th>
                  <th className="py-2 pr-3 text-right">Views</th>
                  <th className="py-2 pr-3 text-right">RPM</th>
                  <th className="py-2 pr-3 text-right">Earnings</th>
                  <th className="py-2">Basis</th>
                </tr>
              </thead>
              <tbody>
                {revenue.map((r) => (
                  <tr key={r.platform} className="border-t border-neutral-800">
                    <td className="py-2 pr-3 font-medium capitalize">{r.platform}</td>
                    <td className="py-2 pr-3 text-right">{fmt(r.views)}</td>
                    <td className="py-2 pr-3 text-right">{r.rpm === null ? "—" : `${r.currency} ${r.rpm.toFixed(2)}`}</td>
                    <td className="py-2 pr-3 text-right font-medium">{r.earnings === null ? "—" : `${r.currency} ${r.earnings.toFixed(2)}`}</td>
                    <td className="py-2 text-xs text-neutral-400">{r.basis || (r.earnings === null ? "Not reported by API" : "From platform API")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 text-xs text-neutral-500">“Not reported by API” means the platform does not share earnings for this account — it is not zero earnings, it is unknown. RPM and earnings only appear when the platform reports them.</p>
          </div>
        )}
      </Card>

      {/* v0.8.0 — affiliate link inserter. */}
      <Card className="glass">
        <CardHeader title="Affiliate links" hint="Attach a link to a clip's description" />
        <p className="mb-3 text-sm text-neutral-400">Paste an affiliate URL for a clip — it is added to the clip&apos;s description when the clip is published. Only use links for products you genuinely recommend, and disclose that the link is an affiliate link in the description.</p>
        <div className="grid gap-3 md:grid-cols-[220px_1fr_auto]">
          <label className="block text-sm">
            <span className="text-neutral-400">Clip ID</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={affClipId} onChange={(e) => setAffClipId(e.target.value)} placeholder="clip_01" aria-label="Clip ID for affiliate link" />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Affiliate URL</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={affUrl} onChange={(e) => setAffUrl(e.target.value)} placeholder="https://…/product?ref=…" aria-label="Affiliate URL" />
          </label>
          <div className="flex items-end">
            <PrimaryButton onClick={onAddAffiliate} disabled={affBusy}>{affBusy ? "Attaching…" : "Attach link"}</PrimaryButton>
          </div>
        </div>
        {affNotice && <p className="mt-3 rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-sm text-sky-100">{affNotice}</p>}
      </Card>

      {/* v0.8.0 — A/B title / thumbnail tests. */}
      <Card className="glass">
        <CardHeader title="A/B tests — titles & thumbnails" hint="Two variants, measured by views" />
        <p className="mb-3 text-sm text-neutral-400">Create two title variants for a clip (variant B can also carry a different thumbnail). Results below show views per variant once the test has run — no winner is declared until both variants have real views.</p>
        <div className="grid gap-3 md:grid-cols-2">
          <label className="block text-sm">
            <span className="text-neutral-400">Clip ID</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={abClipId} onChange={(e) => setAbClipId(e.target.value)} placeholder="clip_01" aria-label="Clip ID for A/B test" />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Variant B thumbnail URL (optional)</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={abThumbB} onChange={(e) => setAbThumbB(e.target.value)} placeholder="https://…/thumb-b.jpg" aria-label="Variant B thumbnail URL" />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Variant A — title</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={abTitleA} onChange={(e) => setAbTitleA(e.target.value)} placeholder="Title A" aria-label="Variant A title" />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Variant B — title</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={abTitleB} onChange={(e) => setAbTitleB(e.target.value)} placeholder="Title B" aria-label="Variant B title" />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <PrimaryButton onClick={onCreateAb} disabled={abBusy}>{abBusy ? "Creating…" : "Create A/B test"}</PrimaryButton>
          <SecondaryButton onClick={onLoadAb} disabled={abLoading || !abClipId.trim()}>{abLoading ? "Loading…" : "Load results"}</SecondaryButton>
        </div>
        {abNotice && <p className="mt-3 rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-sm text-sky-100">{abNotice}</p>}
        {abResults !== null && (
          <div className="mt-4">
            {abResults.length === 0 ? (
              <p className="text-sm text-neutral-400">No A/B results for this clip yet — the test may still be running, or no variant has been shown.</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wide text-neutral-500">
                    <th className="py-2 pr-3">Variant</th>
                    <th className="py-2 pr-3 text-right">Views</th>
                    <th className="py-2 text-right">CTR</th>
                  </tr>
                </thead>
                <tbody>
                  {abResults.map((v, i) => (
                    <tr key={i} className="border-t border-neutral-800">
                      <td className="py-2 pr-3">
                        <span className="font-medium">{v.title}</span>
                        {v.thumbnail && <span className="block text-xs text-neutral-500">Thumbnail: <span className="font-mono">{v.thumbnail}</span></span>}
                      </td>
                      <td className="py-2 pr-3 text-right">{fmt(v.views)}</td>
                      <td className="py-2 text-right">{v.ctr === null ? "—" : `${(v.ctr * 100).toFixed(1)}%`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold">What not to expect</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-neutral-400">
          <li>Instagram Reels Play bonus ended 31 Aug 2025 — there is no per-view Reels pay on Instagram.</li>
          <li>Facebook&apos;s old In-stream Ads, Ads on Reels and Performance Bonus stopped earning 31 Aug 2025; they are replaced by the unified Content Monetization Program.</li>
          <li>Reused or templated AI content can be demonetized even after thresholds are met — only post content you own, with material creative value.</li>
        </ul>
      </Card>
    </div>
  );
}
