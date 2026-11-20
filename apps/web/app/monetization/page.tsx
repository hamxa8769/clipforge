import { apiGet, type Monetization } from "@/lib/api";
import { demoMonetization } from "@/lib/mock";
import { Card, DemoBadge } from "@/components/ui";

function fmt(n: number) { return n.toLocaleString(); }

export default async function MonetizationPage() {
  const items = (await apiGet<Monetization[]>("/monetization")) ?? demoMonetization;
  const live = (await apiGet<unknown>("/health")) !== null;
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3"><h1 className="text-2xl font-bold">Monetization tracker</h1>{!live && <DemoBadge />}</div>
      <p className="text-sm text-neutral-400">Progress toward each platform&apos;s monetization threshold. Thresholds are from the Nov 2026 research — re-verify on the platform&apos;s own pages before relying on them. ClipForge never promises earnings.</p>
      <div className="grid gap-4 md:grid-cols-2">
        {items.map((m) => (
          <Card key={m.platform}>
            <div className="flex items-center gap-3">
              <h2 className="text-lg font-semibold">{m.platform}</h2>
              <span className="text-sm text-neutral-400">{m.program}</span>
              <span className="ml-auto text-lg font-bold">{m.progress_pct}%</span>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-neutral-800"><div className="h-full bg-emerald-500" style={{ width: `${Math.min(100, m.progress_pct)}%` }} /></div>
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
