"use client";
import { useEffect, useState } from "react";
import type { Candidate, Job, SeoPack as SeoPackData } from "@/lib/api";
import { approveCandidate, attributionForClip, exportClipToCapcut, getSeoPack, licenseBadgeLabel, regenerateThumbnail, rejectCandidate, selectThumbnailVariant } from "@/lib/api";
import { summarizeApproveQueue } from "@/lib/review";
import { Card, ExternalLink, StatusPill } from "@/components/ui";
import ClipPlayer from "@/components/ClipPlayer";
import SeoPack from "@/components/SeoPack";

const PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"] as const;
const PLATFORM_LABEL: Record<string, string> = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram", facebook: "Facebook" };

function CandidateCard({ c, job, demo }: { c: Candidate; job: Job; demo: boolean }) {
  const [platform, setPlatform] = useState<string>("youtube");
  const [status, setStatus] = useState(c.status);
  const [note, setNote] = useState("");
  const [noChannel, setNoChannel] = useState(false);
  const seo = c.seo[platform];
  // SEO pack (v0.11.0): title options, description, search tags,
  // hashtags + copy buttons per field, from GET /clips/{id}/seo-pack.
  // In demo mode there is no server, so the pack is never fetched and
  // the plain per-platform variants below are shown instead.
  const [pack, setPack] = useState<SeoPackData | null>(null);
  const [packLoading, setPackLoading] = useState(false);
  useEffect(() => {
    if (demo) return;
    let cancelled = false;
    setPackLoading(true);
    getSeoPack(c.id)
      .then((p) => { if (!cancelled) setPack(p); })
      .catch(() => { if (!cancelled) setPack(null); })
      .finally(() => { if (!cancelled) setPackLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [c.id, demo]);
  // Thumbnail step (v0.7.0): the generated thumbnail, with Regenerate and
  // a variant picker when the API offers variants.
  const variants = c.thumbnail_variants.length > 0 ? c.thumbnail_variants : (c.thumbnail_url ? [c.thumbnail_url] : []);
  const [thumbIdx, setThumbIdx] = useState(0);
  const [thumbUrl, setThumbUrl] = useState<string | null>(c.thumbnail_url);
  const [thumbNote, setThumbNote] = useState("");
  const [regenerating, setRegenerating] = useState(false);
  // CapCut export (v0.10.0): the rendered clip as a CapCut draft folder.
  const [capcutBusy, setCapcutBusy] = useState(false);
  const [capcutNote, setCapcutNote] = useState("");

  async function onExportCapcut() {
    if (demo) {
      setCapcutNote("CapCut export needs the server — demo mode has no rendered clip to export.");
      return;
    }
    setCapcutBusy(true);
    setCapcutNote("");
    const res = await exportClipToCapcut(c.id);
    setCapcutBusy(false);
    if (!res.ok) {
      setCapcutNote(`Could not export to CapCut: ${res.error ?? "request failed"}.`);
      return;
    }
    const data = (res.data ?? {}) as Record<string, unknown>;
    const message = typeof data.message === "string" ? data.message : "CapCut draft ready.";
    setCapcutNote(message);
  }
  // Attribution: the candidate's licence record wins; the job's record
  // fills the gaps (the API merges them the same way server-side).
  const attribution = attributionForClip(c)
    ?? attributionForClip({
      source_license: job.source_license, source_title: job.source_title,
      source_channel: job.source_channel, source_url: job.source,
      attribution_text: job.attribution_text,
    });
  const sourceLicense = c.source_license !== "unknown" ? c.source_license : job.source_license;

  async function onSelectVariant(i: number) {
    const variantNo = c.thumbnails[i]?.variant || i + 1;
    setThumbIdx(i);
    setThumbUrl(variants[i]);
    setThumbNote("");
    if (demo) return; // Demo mode: local selection only, no API to call.
    const res = await selectThumbnailVariant(c.id, variantNo);
    if (!res.ok) {
      setThumbNote(`Could not save the thumbnail choice: ${res.error ?? "request failed"}. The preview above is what you picked, but the server still has the old one.`);
    }
  }

  async function onRegenerateThumb() {
    setRegenerating(true);
    setThumbNote("");
    if (demo) {
      // Demo mode: cycle the built-in variants locally — no API to call.
      const next = variants.length > 1 ? (thumbIdx + 1) % variants.length : thumbIdx;
      setThumbIdx(next);
      setThumbUrl(variants[next] ?? thumbUrl);
      setThumbNote("Thumbnail regenerated locally (demo mode: server not answering). A live regenerate asks the server for a fresh thumbnail.");
      setRegenerating(false);
      return;
    }
    // Real endpoint: POST /clips/{candidate_id}/thumbnail (v0.7.0).
    const res = await regenerateThumbnail(c.id);
    setRegenerating(false);
    if (!res.ok) {
      setThumbNote(`Could not regenerate the thumbnail: ${res.error ?? "the thumbnail endpoint is not available in this build yet"}. The current thumbnail stays in place.`);
      return;
    }
    const data = (res.data ?? {}) as Record<string, unknown>;
    const fresh = typeof data.thumbnail_url === "string" ? data.thumbnail_url : null;
    const thumbs = Array.isArray(data.thumbnails) ? data.thumbnails : [];
    const urls = thumbs.map((t) => (t as Record<string, unknown>).url).filter((u): u is string => typeof u === "string" && !!u);
    if (fresh) {
      setThumbUrl(fresh);
      setThumbNote("Thumbnail regenerated.");
    } else if (urls.length > 0) {
      setThumbUrl(urls[0]);
      setThumbNote("Thumbnail regenerated.");
    } else {
      setThumbNote("Thumbnail regeneration was accepted — the new thumbnail appears once the server finishes it.");
    }
  }

  async function act(action: "approve" | "reject") {
    // Real endpoint: POST /jobs/{jobId}/candidates/{n}/{approve|reject} (docs/api.md).
    // The old code called /candidates/{id}/{action}, which does not exist.
    if (demo) {
      setStatus(action === "approve" ? "approved" : "rejected");
      setNote(action === "approve" ? "Approved — recorded locally (demo mode: server not answering)." : "Rejected (demo mode: recorded locally).");
      return;
    }
    const res = action === "approve" ? await approveCandidate(job.id, c.n) : await rejectCandidate(job.id, c.n);
    if (res.ok) {
      setStatus(action === "approve" ? "approved" : "rejected");
      if (action === "approve") {
        // The server's automation result says what actually happened to
        // the queue — say that, not "sent to the approval queue". With no
        // channel connected the approval creates zero posts, and the old
        // note lied about it.
        const automation = ((res.data as Record<string, unknown> | null)?.automation ?? {}) as Record<string, unknown>;
        const summary = summarizeApproveQueue(automation.queue);
        setNoChannel(false);
        if (summary.total === 0) {
          setNote("Approved — the server did not say what happened to the queue; check the approval queue.");
        } else if (summary.queued.length === 0 && summary.alreadyExisted.length === 0) {
          const reason = summary.skippedNoAccount.length > 0
            ? `no channel is connected yet (${summary.skippedNoAccount.map((p) => PLATFORM_LABEL[p] ?? p).join(", ")})`
            : summary.skippedCap.length > 0 ? "the daily cap was reached" : "no platform accepted it";
          setNote(`Approved — but no post was created: ${reason}. Connect a channel first, then approve again.`);
          setNoChannel(summary.skippedNoAccount.length > 0);
        } else {
          const created = [...summary.queued, ...summary.alreadyExisted].map((p) => PLATFORM_LABEL[p] ?? p).join(", ");
          const skipped = summary.skippedNoAccount.length > 0
            ? ` — skipped ${summary.skippedNoAccount.map((p) => PLATFORM_LABEL[p] ?? p).join(", ")} (no channel connected)`
            : "";
          setNote(`Approved — post created for ${created}${skipped}. See the approval queue.`);
        }
      } else {
        setNote("Rejected.");
      }
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

      {/* Thumbnail step (v0.7.0) */}
      <div className="mt-5 rounded-lg border border-neutral-800 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <h3 className="text-sm font-semibold text-neutral-100">Thumbnail</h3>
          {sourceLicense !== "unknown" && (
            <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${sourceLicense === "cc" ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300" : sourceLicense === "own" ? "border-sky-500/40 bg-sky-500/15 text-sky-300" : "border-neutral-600 bg-neutral-700/50 text-neutral-300"}`}>
              Source: {licenseBadgeLabel(sourceLicense)}
            </span>
          )}
          <button
            type="button"
            onClick={() => { void onRegenerateThumb(); }}
            disabled={regenerating}
            className="ml-auto rounded-md bg-neutral-800 px-3 py-1.5 text-sm font-medium text-neutral-100 hover:bg-neutral-700 disabled:opacity-60"
          >
            {regenerating ? "Regenerating…" : "Regenerate thumbnail"}
          </button>
        </div>
        <div className="mt-3 flex flex-wrap items-start gap-4">
          {thumbUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={thumbUrl} alt={`Generated thumbnail for clip “${c.hook_line}”`} className="h-48 w-auto rounded-lg border border-neutral-800 object-cover" />
          ) : (
            <div className="flex h-48 w-[108px] flex-col items-center justify-center rounded-lg border border-dashed border-neutral-700 bg-neutral-950 p-2 text-center">
              <span className="text-xs text-neutral-400">No thumbnail generated yet — it appears here once the clip renders.</span>
            </div>
          )}
          <div className="min-w-0 flex-1">
            <p className="text-sm text-neutral-400">
              This is the thumbnail that ships with the clip. Regenerate makes a fresh one from the best frame; pick a variant below when the server offers more than one.
            </p>
            {variants.length > 1 && (
              <div className="mt-3">
                <p className="text-xs font-medium text-neutral-300">Variants</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {variants.map((v, i) => (
                    <button
                      key={i}
                      type="button"
                      onClick={() => { void onSelectVariant(i); }}
                      aria-label={`Use thumbnail variant ${i + 1}`}
                      aria-pressed={thumbUrl === v}
                      className={`overflow-hidden rounded-md border-2 ${thumbUrl === v ? "border-amber-400" : "border-neutral-700 hover:border-neutral-500"}`}
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={v} alt={`Thumbnail variant ${i + 1}`} className="h-20 w-auto object-cover" />
                    </button>
                  ))}
                </div>
              </div>
            )}
            {thumbNote && <p className="mt-3 text-sm text-neutral-400">{thumbNote}</p>}
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-4">
        <span className="text-sm text-neutral-400">Clip span: {c.start_s}s – {c.end_s}s</span>
      </div>
      <p className="mt-2 text-xs text-neutral-500">Own-content declaration: this clip is from content you own or have rights to. AI-assisted edits: when captions, reframing or voiceover were AI-generated, label the clip as AI-generated on the platform when you publish — ClipForge does not send that label for you yet.</p>

      <div className="mt-5">
        {pack ? (
          <SeoPack pack={pack} />
        ) : packLoading ? (
          <p className="p-4 text-sm text-neutral-400">Loading the SEO pack…</p>
        ) : (
          <>
            <div className="flex gap-1 border-b border-neutral-800">
              {PLATFORMS.map((p) => (
                <button key={p} onClick={() => setPlatform(p)} className={`rounded-t-md px-3 py-2 text-sm ${platform === p ? "bg-neutral-800 font-medium text-white" : "text-neutral-400 hover:text-white"}`}>{PLATFORM_LABEL[p]}</button>
              ))}
            </div>
            {seo ? (
              <div className="space-y-2 rounded-b-lg bg-neutral-950 p-4 text-sm">
                <p><strong>Title:</strong> {seo.title} <span className="text-neutral-500">({seo.title.length} chars)</span></p>
                <p><strong>Description:</strong> {seo.description}</p>
              {attribution && (
                <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3">
                  <p className="text-xs font-semibold text-emerald-300">Attribution — always included for Creative Commons / public-domain sources (read-only)</p>
                  <p className="mt-1 text-sm text-neutral-200">{attribution}</p>
                  <p className="mt-1 text-xs text-neutral-500">
                    Source: {c.source_title || job.source_title || "Creative Commons video"}{(c.source_channel || job.source_channel) ? ` by ${c.source_channel || job.source_channel}` : ""}{(c.source_url || job.source) ? <> · <ExternalLink className="text-sky-300 underline" href={c.source_url || job.source}>source video</ExternalLink></> : ""}. This credit stays in the published description.
                  </p>
                </div>
              )}
                <p><strong>Hashtags:</strong> {seo.hashtags.join(" ")}</p>
                <p className="text-xs text-neutral-500">
                  {platform === "youtube" && "YouTube: keyword front-loaded, 2–3 hashtags, links in Shorts descriptions are not clickable."}
                  {platform === "tiktok" && "TikTok: keyword in first line; 3–5 hashtags. Clips under 60s will not earn Creator Rewards."}
                  {platform === "instagram" && "Instagram: max 5 hashtags (Dec 2025 cap); keywords in the caption matter more than hashtags."}
                  {platform === "facebook" && "Facebook: short, plain caption, 2–3 niche hashtags; API Reels cap is 90s."}
                </p>
              </div>
            ) : <p className="p-4 text-sm text-neutral-400">No SEO variant for this platform yet.</p>}
          </>
        )}
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <button onClick={() => act("approve")} className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500">Approve</button>
        <button onClick={() => act("reject")} className="rounded-md bg-red-600/80 px-4 py-2 text-sm font-medium hover:bg-red-600">Reject</button>
        <button onClick={() => { void onExportCapcut(); }} disabled={capcutBusy} className="rounded-md bg-neutral-800 px-4 py-2 text-sm font-medium text-neutral-100 hover:bg-neutral-700 disabled:opacity-60">
          {capcutBusy ? "Exporting to CapCut…" : "Export to CapCut"}
        </button>
        {note && <span className="self-center text-sm text-neutral-400">{note}</span>}
        {noChannel && <a href="/channels" className="self-center text-sm text-sky-400 underline">Connect a channel</a>}
      </div>
      {capcutNote && <p className="mt-3 rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-sm text-sky-100">{capcutNote}</p>}
    </Card>
  );
}

export default function ReviewClient({ candidates, job, demo }: { candidates: Candidate[]; job: Job; demo: boolean }) {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">Review clips</h1>
        {demo && <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — server not answering</span>}
      </div>
      {candidates.length === 0 ? (
        <Card><p className="text-neutral-400">{job.status === "failed" ? "No clip candidates — this job failed before producing any. Retry it from the pipeline card above." : job.status === "review" || job.status === "ready" ? "No clip candidates — this run finished, but no moment scored high enough to become a clip. Try a longer or livelier source video, or retry the job." : "No clip candidates yet — this job is still processing."}</p></Card>
      ) : candidates.map((c) => <CandidateCard key={c.id} c={c} job={job} demo={demo} />)}
    </div>
  );
}
