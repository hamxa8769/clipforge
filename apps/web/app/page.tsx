// Client component: data is fetched in the browser (useEffect) instead of
// at render time on the server, so this page can be statically exported
// (`output: "export"`). Same UI, same API contract (lib/api.ts), same
// demo-data fallback + "Demo data" badge when the API is unreachable.
// Initial state is the demo data, so the page renders content immediately
// and swaps in live data once the fetch returns.
"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { apiGet, getJobs, PROCESSING_STATUSES, type Job } from "@/lib/api";
import { demoJobs } from "@/lib/mock";
import { Card, DemoBadge, StatusPill } from "@/components/ui";

export default function Dashboard() {
  const [jobs, setJobs] = useState<Job[]>(demoJobs);
  const [live, setLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetched, health] = await Promise.all([getJobs(), apiGet<unknown>("/health")]);
      if (cancelled) return;
      if (fetched !== null) setJobs(fetched);
      setLive(health !== null);
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  const ready = jobs.filter((j) => j.status === "ready").length;
  // Statuses are normalised in getJobs(): the backend sends "pending", never
  // "queued" — see lib/api.ts normalizeJobStatus.
  const processing = jobs.filter((j) => PROCESSING_STATUSES.includes(j.status)).length;
  const totalCandidates = jobs.reduce((n, j) => n + (j.candidates ?? 0), 0);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">Dashboard</h1>
        {!live && <DemoBadge />}
        <span className="ml-auto text-sm text-neutral-400">API: {live ? "connected" : "unreachable — demo data"}</span>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["Total jobs", String(jobs.length)],
          ["Ready to review", String(ready)],
          ["Processing", String(processing)],
          ["Clip candidates", String(totalCandidates)],
        ].map(([label, value]) => (
          <Card key={label}><p className="text-sm text-neutral-400">{label}</p><p className="mt-1 text-3xl font-bold">{value}</p></Card>
        ))}
      </div>
      <Card>
        <h2 className="mb-4 text-lg font-semibold">Pipeline</h2>
        <ol className="flex flex-wrap gap-2 text-sm">
          {["Ingest", "Transcribe", "Score clips", "Review", "SEO variants", "Approve", "Schedule", "Publish"].map((s, i) => (
            <li key={s} className="flex items-center gap-2"><span className="flex h-6 w-6 items-center justify-center rounded-full bg-neutral-800 text-xs">{i + 1}</span>{s}{i < 7 && <span className="text-neutral-600">→</span>}</li>
          ))}
        </ol>
        <p className="mt-3 text-sm text-neutral-400">Nothing publishes without approval. TikTok posts as an inbox draft until the Content Posting API audit is passed.</p>
      </Card>
      <Card>
        <h2 className="mb-4 text-lg font-semibold">Jobs</h2>
        <ul className="divide-y divide-neutral-800">
          {jobs.map((j) => (
            <li key={j.id} className="flex flex-wrap items-center gap-3 py-3">
              <div className="min-w-0 flex-1">
                <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="font-medium hover:underline">{j.title}</Link>
                {/* Dates are locale-formatted in the browser; the static
                    prerender uses the build machine's locale, so suppress
                    the hydration warning for this text. */}
                <p className="truncate text-sm text-neutral-400" suppressHydrationWarning>{j.source} · {new Date(j.created_at).toLocaleString()}</p>
              </div>
              <span className="text-sm text-neutral-400">{j.candidates} candidates</span>
              <StatusPill status={j.status} />
              <Link href={`/jobs/view/?id=${encodeURIComponent(j.id)}`} className="rounded-md bg-neutral-800 px-3 py-1.5 text-sm hover:bg-neutral-700">Review</Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
