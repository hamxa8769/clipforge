import Link from "next/link";
import { apiGet, type Job } from "@/lib/api";
import { demoJobs } from "@/lib/mock";
import { Card, DemoBadge, StatusPill } from "@/components/ui";

export default async function Dashboard() {
  const jobs = (await apiGet<Job[]>("/jobs")) ?? demoJobs;
  const live = (await apiGet<unknown>("/health")) !== null;
  const ready = jobs.filter((j) => j.status === "ready").length;
  const processing = jobs.filter((j) => ["queued", "transcribing", "scoring"].includes(j.status)).length;
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
                <Link href={`/jobs/${j.id}`} className="font-medium hover:underline">{j.title}</Link>
                <p className="truncate text-sm text-neutral-400">{j.source} · {new Date(j.created_at).toLocaleString()}</p>
              </div>
              <span className="text-sm text-neutral-400">{j.candidates} candidates</span>
              <StatusPill status={j.status} />
              <Link href={`/jobs/${j.id}`} className="rounded-md bg-neutral-800 px-3 py-1.5 text-sm hover:bg-neutral-700">Review</Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
