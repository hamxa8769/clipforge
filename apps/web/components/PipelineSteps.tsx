// Pipeline visibility (v0.3.0): the 8-stage pipeline (research §2) rendered
// as a stepper with per-stage status, on the job detail/review page and
// reused in compact form on the dashboard job rows.
//
// Stage state comes from lib/api.ts pipelineStageStates(job.status,
// job.failed_stage) — derived from the backend JobStatus enum plus the
// reported failed stage (GET /jobs/{id} detail, API-CONTRACT.md §2).
// When the backend names no failed stage, a failed job leaves every stage
// unmarked and the error banner + retry carry the failure instead.
"use client";
import { PIPELINE_STAGES, pipelineStageStates, type Job, type PipelineStageState } from "@/lib/api";

const STAGE_STYLE: Record<PipelineStageState, { dot: string; label: string }> = {
  done: { dot: "bg-emerald-500 text-neutral-950", label: "Done" },
  active: { dot: "bg-amber-400 text-neutral-950", label: "In progress" },
  failed: { dot: "bg-red-500 text-white", label: "Failed" },
  pending: { dot: "bg-neutral-800 text-neutral-400", label: "Waiting" },
};

export default function PipelineSteps({ job, compact = false }: { job: Job; compact?: boolean }) {
  const states = pipelineStageStates(job.status, job.failed_stage ?? null);
  const activeIdx = states.findIndex((s) => s === "active");
  const failedIdx = states.findIndex((s) => s === "failed");
  return (
    <div>
      <ol className="flex flex-wrap items-center gap-y-2" aria-label="Pipeline progress">
        {PIPELINE_STAGES.map((stage, i) => {
          const state = states[i];
          const style = STAGE_STYLE[state];
          return (
            <li key={stage} className="flex items-center">
              <span
                className="flex items-center gap-2 rounded-full border border-neutral-800 bg-neutral-950 px-2.5 py-1 text-xs"
                title={`${stage}: ${style.label}`}
              >
                <span className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold ${style.dot}`}>
                  {state === "done" ? "✓" : i + 1}
                </span>
                <span className={state === "pending" ? "text-neutral-500" : "text-neutral-200"}>{stage}</span>
                {!compact && state === "active" && <span className="text-amber-300">· in progress</span>}
              </span>
              {i < PIPELINE_STAGES.length - 1 && <span className="mx-1 text-neutral-700" aria-hidden="true">→</span>}
            </li>
          );
        })}
      </ol>
      {!compact && (
        <p className="mt-2 text-xs text-neutral-500">
          {job.status === "failed"
            ? failedIdx >= 0
              ? `Failed at: ${PIPELINE_STAGES[failedIdx]} (backend stage "${job.failed_stage}"). See the error below and retry — the same job runs again.`
              : "This job failed before finishing. The stage it died in was not reported — see the error below and retry."
            : job.status === "ready"
              ? "Clips and SEO variants are ready. Next: approve a clip in the review list below."
              : activeIdx >= 0
                ? `Currently at: ${PIPELINE_STAGES[activeIdx]}. Nothing publishes without approval.`
                : "Waiting to start."}
        </p>
      )}
    </div>
  );
}
