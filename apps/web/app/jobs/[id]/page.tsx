import { apiGet, type Candidate, type Job } from "@/lib/api";
import { demoCandidates, demoJobs } from "@/lib/mock";
import ReviewClient from "./ReviewClient";

export default async function JobReview({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const job = (await apiGet<Job>(`/jobs/${id}`)) ?? demoJobs.find((j) => j.id === id) ?? demoJobs[0];
  const candidates = (await apiGet<Candidate[]>(`/jobs/${id}/candidates`)) ?? demoCandidates.filter((c) => c.job_id === id || id === "job-001");
  const live = (await apiGet<unknown>("/health")) !== null;
  return (
    <div className="space-y-2">
      <p className="text-sm text-neutral-400">Job {job.id} · {job.source} · {job.status}</p>
      <h1 className="sr-only">{job.title}</h1>
      <ReviewClient candidates={candidates} demo={!live} />
    </div>
  );
}
