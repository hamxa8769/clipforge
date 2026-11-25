// Dashboard one-box (v0.11.0) — ClipNova's simple flow, re-implemented
// for ClipForge: one prominent box, paste a video or channel link,
// the app finds the video(s), clipping starts in one click.
//
// * Video link → POST /resolve answers with the video → one
//   "Start clipping" button starts the job through the existing
//   pipeline (ingest → transcribe → score → review). No wizard, no
//   licence-check round trip first.
// * Channel link → POST /resolve lists the channel's newest videos
//   (yt-dlp flat playlist, no API key needed; richer API listing when
//   a YouTube Data API key is saved) → the picker shows title /
//   duration / thumbnail, the user ticks one or several → each starts
//   its own clip job. Facecam, captions and CapCut export stay
//   available from Review, as before — this box only changes how a
//   job starts, not what the pipeline does with it.
//
// Rights (v0.11.0, same as NewClipRun): no rights checkbox or notice
// in this box — the job is sent with own_content_declaration=true so
// the server's ingest accepts the YouTube URL (ingest still requires
// the declaration server-side; the UI no longer asks for it — the
// user is responsible for the rights to what they clip, stated in
// the release notes, not in the UI). Verified live: a job sent
// without the declaration fails at ingest ("YouTube ingest requires
// own_content=True"), so a checkbox that defaults off would only
// ever produce a job that cannot download.
"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  createYoutubeClipJob, formatDuration, formatPublishedAt, resolveUrl,
  type ResolvedVideo, type ResolveResult,
} from "@/lib/api";
import { Card, CardHeader, PrimaryButton, SecondaryButton, Spinner, TextInput } from "./ui";

function fmtViews(n: number): string {
  if (!n) return "";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M views`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K views`;
  return `${n} views`;
}

export default function OneBox({ live, onJobStarted }: { live: boolean; onJobStarted?: () => void }) {
  const [url, setUrl] = useState("");
  const [resolving, setResolving] = useState(false);
  const [result, setResult] = useState<ResolveResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [starting, setStarting] = useState(false);
  const [startedJobIds, setStartedJobIds] = useState<string[]>([]);
  const [startNote, setStartNote] = useState<string | null>(null);

  // Deep link: /?resolve=<video-or-channel-url> fills the box and
  // resolves at once, so a channel link shared from anywhere lands
  // straight on the picker. Runs once, only when the server is live.
  const deepLinked = useRef(false);
  useEffect(() => {
    if (deepLinked.current || !live) return;
    let param = "";
    try { param = new URLSearchParams(window.location.search).get("resolve") ?? ""; } catch { param = ""; }
    if (!param.trim()) return;
    deepLinked.current = true;
    setUrl(param.trim());
    void resolveNow(param.trim());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  const offlineReason = "The server is not answering — clip runs start when the API is back.";
  const videos = result?.videos ?? [];
  const selectedVideos = videos.filter((v) => selected.has(v.video_id));

  function resetForNewUrl(next: string) {
    setUrl(next);
    setResult(null);
    setSelected(new Set());
    setStartedJobIds([]);
    setStartNote(null);
    setError(null);
  }

  async function resolveNow(trimmed: string) {
    setResolving(true);
    setError(null);
    setResult(null);
    setSelected(new Set());
    setStartedJobIds([]);
    setStartNote(null);
    const { result: found, error: err } = await resolveUrl(trimmed);
    setResolving(false);
    if (err || !found) {
      setError(err ?? "Could not resolve that link.");
      return;
    }
    setResult(found);
  }

  async function onResolve(e?: React.FormEvent) {
    e?.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) { setError("Paste a YouTube video or channel link first."); return; }
    await resolveNow(trimmed);
  }

  async function startVideos(toStart: ResolvedVideo[]) {
    if (toStart.length === 0) return;
    setStarting(true);
    setError(null);
    setStartNote(null);
    const started: string[] = [];
    let failed = 0;
    let lastError: string | null = null;
    for (const video of toStart) {
      const res = await createYoutubeClipJob({
        source_url: video.url,
        title: video.title,
        source_license: "own",
        source_title: video.title,
        source_channel: video.channel_title,
        own_content_declaration: true,
      });
      if (res.ok) {
        const data = (res.data ?? {}) as Record<string, unknown>;
        if (typeof data.id === "string") started.push(data.id);
      } else {
        failed += 1;
        lastError = res.error ?? "Could not start the clip job.";
      }
    }
    setStarting(false);
    setStartedJobIds(started);
    if (started.length > 0) {
      setStartNote(
        failed > 0
          ? `${started.length} clip job${started.length === 1 ? "" : "s"} started, ${failed} failed — ${lastError ?? "try the failed one again."}`
          : `${started.length} clip job${started.length === 1 ? "" : "s"} started — cutting clips now.`,
      );
      onJobStarted?.();
    } else {
      setError(lastError ?? "Could not start the clip job.");
    }
  }

  function toggleVideo(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <Card className="card-hover">
      <CardHeader
        title="Clip a video"
        hint="Paste a video or channel link — the app finds the video and clipping starts in one click."
        action={<Link href="/pipeline" className="text-sm font-medium text-amber-300 hover:text-amber-200">Pipeline →</Link>}
      />
      {!live && (
        <p className="mb-4 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-200" role="note">{offlineReason}</p>
      )}

      <form onSubmit={(e) => void onResolve(e)}>
        <div className="flex flex-col gap-2 sm:flex-row">
          <TextInput
            id="onebox-url"
            type="url"
            value={url}
            onChange={(e) => resetForNewUrl(e.target.value)}
            placeholder="https://www.youtube.com/watch?v=… or https://www.youtube.com/@channel"
            disabled={!live || resolving}
            aria-label="Video or channel link"
            className="flex-1"
          />
          {/* The button is the form's submit: no onClick here, or the
              resolve would run twice (click + submit). */}
          <PrimaryButton disabled={!live || resolving || !url.trim()}>
            {resolving ? <Spinner label="Finding videos…" /> : "Find videos"}
          </PrimaryButton>
        </div>
      </form>

      {error && (
        <p className="mt-4 rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-200" role="alert">{error}</p>
      )}

      {/* One video: title + channel + duration, one button to start. */}
      {result?.kind === "video" && result.video && (
        <div className="mt-4 rounded-lg border border-neutral-800 bg-neutral-950 p-3">
          <div className="flex flex-wrap items-start gap-3">
            {result.video.thumbnail_url && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={result.video.thumbnail_url} alt="" className="h-20 w-36 shrink-0 rounded-md object-cover" />
            )}
            <div className="min-w-0 flex-1">
              <p className="font-medium text-neutral-100">{result.video.title || "Untitled video"}</p>
              <p className="mt-0.5 text-sm text-neutral-400">
                {result.video.channel_title || result.channel?.title}
                {result.video.duration_s !== null && ` · ${formatDuration(result.video.duration_s)}`}
                {formatPublishedAt(result.video.published_at) && ` · ${formatPublishedAt(result.video.published_at)}`}
              </p>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <PrimaryButton onClick={() => void startVideos([result.video!])} disabled={!live || starting}>
                  {starting ? <Spinner label="Starting…" /> : "Start clipping"}
                </PrimaryButton>
                <span className="text-xs text-neutral-500">One click — ingest, transcribe, score, then review. Facecam, captions and CapCut export are in Review.</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Channel / playlist: pick one or several videos, then clip. */}
      {(result?.kind === "channel" || result?.kind === "playlist") && (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-neutral-100">
              {result.channel?.title || "Channel"} — {videos.length} newest video{videos.length === 1 ? "" : "s"}
            </p>
            <span className="text-xs text-neutral-500">Newest first — tick one or several, then clip.</span>
            {videos.length > 1 && (
              <span className="ml-auto flex gap-2 text-xs">
                <button
                  type="button"
                  onClick={() => setSelected(new Set(videos.map((v) => v.video_id)))}
                  className="font-medium text-amber-300 hover:text-amber-200"
                >
                  Select all
                </button>
                <button
                  type="button"
                  onClick={() => setSelected(new Set())}
                  className="font-medium text-neutral-400 hover:text-neutral-200"
                >
                  Clear
                </button>
              </span>
            )}
          </div>
          <ul className="mt-3 space-y-2">
            {videos.map((video) => (
              <li key={video.video_id} className={`flex items-start gap-3 rounded-lg border px-3 py-2.5 ${selected.has(video.video_id) ? "border-amber-400/60 bg-amber-400/5" : "border-neutral-800 bg-neutral-950 hover:border-neutral-700"}`}>
                <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3">
                  <input
                    type="checkbox"
                    checked={selected.has(video.video_id)}
                    onChange={() => toggleVideo(video.video_id)}
                    disabled={!live}
                    aria-label={`Pick ${video.title}`}
                    className="mt-1 h-4 w-4 shrink-0 accent-amber-400"
                  />
                  {video.thumbnail_url && (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={video.thumbnail_url} alt="" className="h-14 w-24 shrink-0 rounded-md object-cover" />
                  )}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-neutral-100">{video.title || "Untitled video"}</span>
                    <span className="mt-0.5 block text-xs text-neutral-400">
                      {video.channel_title || result.channel?.title}
                      {video.duration_s !== null && ` · ${formatDuration(video.duration_s)}`}
                      {formatPublishedAt(video.published_at) && ` · ${formatPublishedAt(video.published_at)}`}
                      {video.view_count > 0 && ` · ${fmtViews(video.view_count)}`}
                    </span>
                  </span>
                </label>
                {/* Outside the label on purpose: a button inside a
                    label would also toggle the row's checkbox. */}
                <SecondaryButton
                  onClick={() => void startVideos([video])}
                  disabled={!live || starting}
                >
                  {starting ? "Starting…" : "Clip"}
                </SecondaryButton>
              </li>
            ))}
          </ul>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <PrimaryButton onClick={() => void startVideos(selectedVideos)} disabled={!live || starting || selectedVideos.length === 0}>
              {starting ? <Spinner label="Starting…" /> : `Clip selected (${selectedVideos.length})`}
            </PrimaryButton>
            {selectedVideos.length === 0 && (
              <span className="text-xs text-neutral-500">Tick a video above — or press Clip on one row to start it right away.</span>
            )}
          </div>
        </div>
      )}

      {startedJobIds.length > 0 && startNote && (
        <p className="mt-4 rounded-lg bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200" role="status">
          {startNote}{" "}
          <Link href="/pipeline" className="font-medium underline hover:text-emerald-100">Watch it in the Pipeline →</Link>
        </p>
      )}
    </Card>
  );
}
