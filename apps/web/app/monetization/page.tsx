// Client component: monetization + liveness are fetched in the browser so
// this page can be statically exported. Same UI, same demo fallback + badge.
//
// v0.3.0: progress is computed with REAL metrics — the latest analytics
// snapshot per platform is passed into GET /monetization?platform=…
// (subscribers / followers / watch_hours / shorts_views / views_30d).
// The bare /monetization endpoint defaults every metric to 0, which
// previously made every live channel read 0% here.
"use client";
import { useEffect, useState } from "react";
import { apiGet, getAnalytics, getMonetizationFor, latestSnapshotPerPlatform, type Monetization } from "@/lib/api";
import { demoAnalytics, demoMonetization } from "@/lib/mock";
import { Card, DemoBadge, ProgressBar, Skeleton } from "@/components/ui";

function fmt(n: number) { return n.toLocaleString(); }

const PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"] as const;

export default function MonetizationPage() {
  const [items, setItems] = useState<Monetization[] | null>(null);
  const [live, setLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetchedAnalytics, health] = await Promise.all([
        getAnalytics(), apiGet<unknown>("/health"),
      ]);
      if (cancelled) return;
      const isLive = health !== null;
      setLive(isLive);
      if (!isLive) { setItems(demoMonetization); return; }
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
      setItems(rows);
    })();
    return () => {
      cancelled = true;
    };
  }, []);
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
            <Card key={`${m.platform}-${m.program}`}>
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
