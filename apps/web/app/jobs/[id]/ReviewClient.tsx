"use client";
import { useState } from "react";
import type { Candidate } from "@/lib/api";
import { Card, StatusPill } from "@/components/ui";

const PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"] as const;
const PLATFORM_LABEL: Record<string, string> = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram", facebook: "Facebook" };

function CandidateCard({ c }: { c: Candidate }) {
  const [platform, setPlatform] = useState<string>("youtube");
  const [start, setStart] = useState(c.start_s);
  const [end, setEnd] = useState(c.end_s);
  const [aiLabel, setAiLabel] = useState(true);
  const [status, setStatus] = useState(c.status);
  const [note, setNote] = useState("");
  const seo = c.seo[platform];
  const duration = Math.max(0, end - start);

  async function act(action: "approve" | "reject") {
    // POST to API when live; in demo mode this records the decision locally only.
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/candidates/${c.id}/${action}`, { method: "POST" });
    } catch { /* demo mode: local-only */ }
    setStatus(action === "approve" ? "approved" : "rejected");
    setNote(action === "approve" ? "Approved — sent to the approval queue (demo mode: recorded locally)." : "Rejected (demo mode: recorded locally).");
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

      <div className="mt-4 flex flex-wrap items-end gap-4">
        <label className="text-sm">Trim start (s)
          <input type="number" min={0} value={start} onChange={(e) => setStart(Number(e.target.value))} className="mt-1 block w-24 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5" />
        </label>
        <label className="text-sm">Trim end (s)
          <input type="number" min={0} value={end} onChange={(e) => setEnd(Number(e.target.value))} className="mt-1 block w-24 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5" />
        </label>
        <span className="text-sm text-neutral-400">Duration: {duration}s {duration > 90 && platform !== "youtube" ? "— over 90s: too long for Facebook Reels API" : ""}{duration < 60 && platform === "tiktok" ? "— under 60s: will not earn TikTok Creator Rewards" : ""}</span>
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

export default function ReviewClient({ candidates, demo }: { candidates: Candidate[]; demo: boolean }) {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">Review clips</h1>
        {demo && <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — API unreachable</span>}
      </div>
      {candidates.length === 0 ? (
        <Card><p className="text-neutral-400">No clip candidates yet — this job is still processing.</p></Card>
      ) : candidates.map((c) => <CandidateCard key={c.id} c={c} />)}
    </div>
  );
}
