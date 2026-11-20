// Client component: job + candidates + liveness are fetched in the browser
// (useEffect) instead of at render time on the server, so this page can be
// statically exported. Same UI, same API contract (lib/api.ts), same demo
// fallback as before (unknown id falls back to the first demo job, exactly
// like the old server version).
"use client";
import { useEffect, useState } from "react";
import { getJob, apiGet, type Candidate, type Job } from "@/lib/api";
import { demoCandidates, demoJobs } from "@/lib/mock";
import ReviewClient from "./ReviewClient";

export default function JobReviewClient({ id }: { id: string }) {
  const [job, setJob] = useState<Job>(demoJobs.find((j) => j.id === id) ?? demoJobs[0]);
  const [candidates, setCandidates] = useState<Candidate[]>(
    demoCandidates.filter((c) => c.job_id === id || id === "job-001"),
  );
  const [live, setLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetchedJob, fetchedCandidates, health] = await Promise.all([
        getJob(id),
        apiGet<Candidate[]>(`/jobs/${id}/candidates`),
        apiGet<unknown>("/health"),
      ]);
      if (cancelled) return;
      if (fetchedJob !== null) setJob(fetchedJob);
      if (fetchedCandidates !== null) setCandidates(fetchedCandidates);
      setLive(health !== null);
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);
  return (
    <div className="space-y-2">
      <p className="text-sm text-neutral-400">Job {job.id} · {job.source} · {job.status}</p>
      <h1 className="sr-only">{job.title}</h1>
      <ReviewClient candidates={candidates} job={job} demo={!live} />
    </div>
  );
}
