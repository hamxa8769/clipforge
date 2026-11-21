"use client";
import { useState } from "react";
import type { Candidate, Job } from "@/lib/api";
import { approveCandidate, rejectCandidate } from "@/lib/api";
import { Card, StatusPill } from "@/components/ui";
import ClipPlayer from "@/components/ClipPlayer";

const PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"] as const;
const PLATFORM_LABEL: Record<string, string> = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram", facebook: "Facebook" };

function CandidateCard({ c, job, demo }: { c: Candidate; job: Job; demo: boolean }) {
  const [platform, setPlatform] = useState<string>("youtube");
  const [aiLabel, setAiLabel] = useState(true);
  const [status, setStatus] = useState(c.status);
  const [note, setNote] = useState("");
  const seo = c.seo[platform];

  async function act(action: "approve" | "reject") {
    // Real endpoint: POST /jobs/{jobId}/candidates/{n}/{approve|reject} (docs/api.md).
    // The old code called /candidates/{id}/{action}, which does not exist.
    if (demo) {
      setStatus(action === "approve" ? "approved" : "rejected");
      setNote(action === "approve" ? "Approved — recorded locally (demo mode: API unreachable)." : "Rejected (demo mode: recorded locally).");
      return;
    }
    const res = action === "approve" ? await approveCandidate(job.id, c.n) : await rejectCandidate(job.id, c.n);
    if (res.ok) {
      setStatus(action === "approve" ? "approved" : "rejected");
      setNote(action === "approve" ? "Approved — sent to the approval queue." : "Rejected.");
    } else {
      setNote(`Could not ${action}: ${res.error ?? "request failed"}.`);
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-3xl font-bold">{c.score}<span className="text-base text-neutral-500">/100</span></span>
        <div>
          <p className="text-sm text-neutral-400">Virality score · Hook rating <strong className="text-neutral-200">{c.hook_rating}/10</strong></p>
          <p className="font-medium">“{c.hook_line}”</p>
        </div>
        <span className="ml-auto"><StatusPill status={status} /></span>
      </div>
      <p className="mt-3 text-sm text-neutral-400"><strong className="text-neutral-300">Why this clip:</strong> {c.reason}</p>
      <blockquote className="mt-3 rounded-lg bg-neutral-950 p-3 text-sm italic text-neutral-300">“{c.transcript_excerpt}”</blockquote>

      <ClipPlayer candidate={c} job={job} demo={demo} />

      <div className="mt-4 flex flex-wrap items-center gap-4">
        <span className="text-sm text-neutral-400">Clip span: {c.start_s}s – {c.end_s}s</span>
        <label className="ml-auto flex items-center gap-2 text-sm">
          <input type="checkbox" checked={aiLabel} onChange={(e) => setAiLabel(e.target.checked)} className="h-4 w-4 accent-amber-400" />
          AI-assisted edit — label as AI-generated content
        </label>
      </div>
      <p className="mt-2 text-xs text-neutral-500">Own-content declaration: this clip is from content you own or have rights to. AI label defaults ON when captions, reframing or voiceover were AI-generated; simple cleanup alone generally does not need a label.</p>

      <div className="mt-5">
        <div className="flex gap-1 border-b border-neutral-800">
          {PLATFORMS.map((p) => (
            <button key={p} onClick={() => setPlatform(p)} className={`rounded-t-md px-3 py-2 text-sm ${platform === p ? "bg-neutral-800 font-medium text-white" : "text-neutral-400 hover:text-white"}`}>{PLATFORM_LABEL[p]}</button>
          ))}
        </div>
        {seo ? (
          <div className="space-y-2 rounded-b-lg bg-neutral-950 p-4 text-sm">
            <p><strong>Title:</strong> {seo.title} <span className="text-neutral-500">({seo.title.length} chars)</span></p>
            <p><strong>Description:</strong> {seo.description}</p>
            <p><strong>Hashtags:</strong> {seo.hashtags.join(" ")}</p>
            <p className="text-xs text-neutral-500">
              {platform === "youtube" && "YouTube: keyword front-loaded, 2–3 hashtags, links in Shorts descriptions are not clickable."}
              {platform === "tiktok" && "TikTok: keyword in first line; 3–5 hashtags. Clips under 60s will not earn Creator Rewards."}
              {platform === "instagram" && "Instagram: max 5 hashtags (Dec 2025 cap); keywords in the caption matter more than hashtags."}
              {platform === "facebook" && "Facebook: short, plain caption, 2–3 niche hashtags; API Reels cap is 90s."}
            </p>
          </div>
        ) : <p className="p-4 text-sm text-neutral-400">No SEO variant for this platform yet.</p>}
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <button onClick={() => act("approve")} className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500">Approve</button>
        <button onClick={() => act("reject")} className="rounded-md bg-red-600/80 px-4 py-2 text-sm font-medium hover:bg-red-600">Reject</button>
        {note && <span className="self-center text-sm text-neutral-400">{note}</span>}
      </div>
    </Card>
  );
}

export default function ReviewClient({ candidates, job, demo }: { candidates: Candidate[]; job: Job; demo: boolean }) {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">Review clips</h1>
        {demo && <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — API unreachable</span>}
      </div>
      {candidates.length === 0 ? (
        <Card><p className="text-neutral-400">{job.status === "failed" ? "No clip candidates — this job failed before producing any. Retry it from the pipeline card above." : job.status === "review" || job.status === "ready" ? "No clip candidates — this run finished, but no moment scored high enough to become a clip. Try a longer or livelier source video, or retry the job." : "No clip candidates yet — this job is still processing."}</p></Card>
      ) : candidates.map((c) => <CandidateCard key={c.id} c={c} job={job} demo={demo} />)}
    </div>
  );
}
