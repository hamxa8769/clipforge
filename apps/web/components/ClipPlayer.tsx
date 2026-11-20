"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import type { Candidate } from "@/lib/api";
import { clipVideoUrl, sourceVideoUrl, trimCandidate, type Job } from "@/lib/api";

/**
 * Real video player + timeline trim editor for one clip candidate.
 *
 * - HTML5 <video> (no new dependencies). Plays the rendered 9:16 clip from
 *   GET /media/clips/{job_id}/{filename}; if that file is not available it
 *   falls back to the job's source upload, and if neither plays it says so
 *   instead of showing a broken player.
 * - Timeline: an honest time ruler (tick marks + labels) over the video's
 *   real duration, with draggable start/end handles, a playhead, and the
 *   selected range shaded. No fake waveform — ClipForge has no audio-peak
 *   data on this page, and invented peaks would mislead trimming.
 * - Numeric start/end inputs are kept as the keyboard-accessible fallback.
 * - "Preview trim" plays only the selected range. "Save trim" calls the
 *   real backend endpoint POST /jobs/{id}/candidates/{n}/trim (422s from
 *   the server are shown, not swallowed).
 *
 * Contract note: the API returns trim-aware start_s/end_s but not the
 * candidate's ORIGINAL span, so the editor can only narrow within the span
 * it was given — it cannot widen a previous trim back out. Handles are
 * clamped to [origStart, origEnd] for that reason (see docs/api.md).
 */

function fmt(t: number): string {
  if (!Number.isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

/** Pick a ruler step so there are roughly 8–12 ticks across the duration. */
function rulerStep(duration: number): number {
  const steps = [1, 2, 5, 10, 15, 30, 60, 120];
  for (const s of steps) if (duration / s <= 12) return s;
  return 300;
}

type DragTarget = "start" | "end" | "seek" | null;

export default function ClipPlayer({
  candidate,
  job,
  demo,
}: {
  candidate: Candidate;
  job: Job;
  demo: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);

  // The span the API gave us is the clamp window (see contract note above).
  const origStart = candidate.start_s;
  const origEnd = candidate.end_s;

  const [videoSrc, setVideoSrc] = useState<string | null>(() => clipVideoUrl(candidate));
  const [triedSourceFallback, setTriedSourceFallback] = useState(false);
  const [videoFailed, setVideoFailed] = useState(false);
  const [duration, setDuration] = useState<number>(origEnd); // metadata overrides this
  const [currentTime, setCurrentTime] = useState(0);
  const [start, setStart] = useState(origStart);
  const [end, setEnd] = useState(origEnd);
  const [drag, setDrag] = useState<DragTarget>(null);
  const [previewing, setPreviewing] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [note, setNote] = useState("");
  const [noteKind, setNoteKind] = useState<"info" | "error" | "ok">("info");

  const previewRef = useRef(false);
  const clampStart = useCallback((v: number) => Math.min(Math.max(v, origStart), end - 0.1), [origStart, end]);
  const clampEnd = useCallback((v: number) => Math.max(Math.min(v, origEnd), start + 0.1), [origEnd, start]);

  function showNote(kind: "info" | "error" | "ok", text: string) {
    setNoteKind(kind);
    setNote(text);
  }

  // --- video events -------------------------------------------------------
  function onLoadedMetadata() {
    const v = videoRef.current;
    if (v && Number.isFinite(v.duration) && v.duration > 0) setDuration(v.duration);
  }
  function onTimeUpdate() {
    const v = videoRef.current;
    if (!v) return;
    setCurrentTime(v.currentTime);
    if (previewRef.current && v.currentTime >= end) {
      v.pause();
      previewRef.current = false;
      setPreviewing(false);
    }
  }
  function onVideoError() {
    // First failure: try the job's source upload. Second failure: give up honestly.
    if (!triedSourceFallback) {
      const fallback = sourceVideoUrl(job);
      setTriedSourceFallback(true);
      if (fallback) {
        setVideoSrc(fallback);
        return;
      }
    }
    setVideoFailed(true);
  }

  // --- seeking / dragging ---------------------------------------------------
  function timeFromClientX(clientX: number): number {
    const track = trackRef.current;
    if (!track) return 0;
    const rect = track.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    return frac * duration;
  }

  function onTrackPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    const t = timeFromClientX(e.clientX);
    // Clicking near a handle starts a drag; clicking elsewhere seeks.
    const nearStart = Math.abs(t - start) < duration * 0.03;
    const nearEnd = Math.abs(t - end) < duration * 0.03;
    if (nearStart) setDrag("start");
    else if (nearEnd) setDrag("end");
    else {
      setDrag("seek");
      seekTo(t);
    }
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
  }

  function onTrackPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    if (!drag) return;
    const t = timeFromClientX(e.clientX);
    if (drag === "start") setStart(clampStart(t));
    else if (drag === "end") setEnd(clampEnd(t));
    else seekTo(t);
  }

  function endDrag() {
    setDrag(null);
    setSaved(false);
  }

  function seekTo(t: number) {
    const v = videoRef.current;
    const clamped = Math.min(Math.max(t, 0), duration);
    if (v) v.currentTime = clamped;
    setCurrentTime(clamped);
  }

  function togglePlay() {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      previewRef.current = false;
      setPreviewing(false);
      void v.play();
    } else v.pause();
  }

  function previewTrim() {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = start;
    setCurrentTime(start);
    previewRef.current = true;
    setPreviewing(true);
    void v.play();
  }

  // Stop a trim preview if the user drags a handle mid-preview.
  useEffect(() => {
    if (previewRef.current && (start > currentTime || end < currentTime)) {
      previewRef.current = false;
      setPreviewing(false);
      videoRef.current?.pause();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start, end]);

  // --- save trim ------------------------------------------------------------
  async function saveTrim() {
    const s = Math.round(start * 10) / 10;
    const e = Math.round(end * 10) / 10;
    if (e - s < 0.5) {
      showNote("error", "Trim must be at least 0.5s long.");
      return;
    }
    setSaving(true);
    setSaved(false);
    if (demo) {
      // Demo mode: no API to call; record the trim locally only.
      showNote("info", `Trim ${fmt(s)} – ${fmt(e)} recorded locally (demo mode — API unreachable, nothing saved on the server).`);
      setSaving(false);
      setSaved(true);
      return;
    }
    const res = await trimCandidate(job.id, candidate.n, s, e);
    setSaving(false);
    if (res.ok) {
      setSaved(true);
      showNote("ok", `Trim saved on the server: ${fmt(s)} – ${fmt(e)} (${(e - s).toFixed(1)}s).`);
    } else {
      showNote("error", `Could not save trim: ${res.error ?? "request failed"}.`);
    }
  }

  // --- render ---------------------------------------------------------------
  const pct = (t: number) => `${(Math.min(Math.max(t, 0), duration) / duration) * 100}%`;
  const step = rulerStep(duration);
  const ticks: number[] = [];
  for (let t = 0; t <= duration + 1e-6; t += step) ticks.push(Math.min(t, duration));
  if (ticks[ticks.length - 1] < duration) ticks.push(duration);
  const trimDuration = Math.max(0, end - start);

  return (
    <div className="mt-4">
      <div className="flex flex-col gap-4 sm:flex-row">
        {/* Player — 9:16 portrait */}
        <div className="mx-auto w-full max-w-[270px] shrink-0">
          <div className="relative aspect-[9/16] w-full overflow-hidden rounded-lg bg-black">
            {!videoFailed && videoSrc ? (
              <video
                ref={videoRef}
                src={videoSrc}
                className="h-full w-full object-contain"
                controls={false}
                playsInline
                preload="metadata"
                onLoadedMetadata={onLoadedMetadata}
                onTimeUpdate={onTimeUpdate}
                onPlay={() => setIsPlaying(true)}
                onPause={() => { setIsPlaying(false); previewRef.current = false; setPreviewing(false); }}
                onError={onVideoError}
                onClick={togglePlay}
              />
            ) : (
              <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center">
                <p className="text-sm font-medium text-neutral-300">No playable video yet</p>
                <p className="text-xs leading-relaxed text-neutral-500">
                  {candidate.file_path
                    ? "The rendered clip file is not reachable from this browser (the API media endpoint may be down or the file was rendered on another machine)."
                    : "This candidate has no rendered clip file yet — the job may still be rendering. The trim timeline below still works against the candidate's time span."}
                </p>
              </div>
            )}
          </div>
          <div className="mt-2 flex items-center gap-2">
            <button onClick={togglePlay} disabled={videoFailed || !videoSrc} className="rounded-md bg-neutral-800 px-3 py-1.5 text-sm hover:bg-neutral-700 disabled:opacity-40">
              {isPlaying ? "Pause" : "Play"}
            </button>
            <button onClick={previewTrim} disabled={videoFailed || !videoSrc} className="rounded-md bg-amber-500/20 px-3 py-1.5 text-sm font-medium text-amber-300 hover:bg-amber-500/30 disabled:opacity-40">
              {previewing ? "Previewing trim…" : "Preview trim"}
            </button>
            <span className="ml-auto font-mono text-xs text-neutral-400">
              {fmt(currentTime)} / {fmt(duration)}
            </span>
          </div>
          <p className="mt-1 text-xs text-neutral-500">
            {videoSrc && !videoFailed
              ? triedSourceFallback
                ? "Playing the source upload (rendered clip unavailable)."
                : "Playing the rendered 9:16 clip."
              : "Timeline works without video — times are the candidate's own span."}
          </p>
        </div>

        {/* Timeline + trim controls */}
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">Trim timeline</p>
          <p className="text-xs text-neutral-500">Drag the handles, click the ruler to seek, or type exact times below. Range is clamped to the candidate span {fmt(origStart)} – {fmt(origEnd)}.</p>

          {/* Ruler + track */}
          <div className="mt-3 select-none">
            <div className="relative h-5">
              {ticks.map((t) => (
                <span key={t} className="absolute -translate-x-1/2 font-mono text-[10px] text-neutral-500" style={{ left: pct(t) }}>
                  {fmt(t)}
                </span>
              ))}
            </div>
            <div
              ref={trackRef}
              role="slider"
              aria-label="Trim timeline"
              aria-valuemin={origStart}
              aria-valuemax={origEnd}
              aria-valuenow={start}
              className="relative h-12 cursor-pointer touch-none rounded-md bg-neutral-950"
              onPointerDown={onTrackPointerDown}
              onPointerMove={onTrackPointerMove}
              onPointerUp={endDrag}
              onPointerLeave={endDrag}
            >
              {/* tick marks */}
              {ticks.map((t) => (
                <span key={t} className="absolute top-0 h-2 w-px bg-neutral-700" style={{ left: pct(t) }} />
              ))}
              {/* dimmed outside the trim range */}
              <div className="absolute inset-y-0 left-0 rounded-l-md bg-neutral-800/70" style={{ width: pct(start) }} />
              <div className="absolute inset-y-0 right-0 rounded-r-md bg-neutral-800/70" style={{ width: `calc(100% - ${pct(end)})` }} />
              {/* selected range */}
              <div className="absolute inset-y-0 bg-amber-500/25" style={{ left: pct(start), width: `calc(${pct(end)} - ${pct(start)})` }} />
              {/* playhead */}
              <div className="absolute inset-y-0 w-0.5 bg-white" style={{ left: pct(currentTime) }} />
              {/* handles */}
              <div
                className="absolute top-0 h-full w-3 -translate-x-1/2 cursor-ew-resize rounded bg-amber-400"
                style={{ left: pct(start) }}
                title={`Trim start ${fmt(start)}`}
              />
              <div
                className="absolute top-0 h-full w-3 -translate-x-1/2 cursor-ew-resize rounded bg-amber-400"
                style={{ left: pct(end) }}
                title={`Trim end ${fmt(end)}`}
              />
            </div>
            <div className="mt-1 flex justify-between text-[10px] text-neutral-500">
              <span>0:00.0</span>
              <span>Selected: {fmt(start)} – {fmt(end)} ({trimDuration.toFixed(1)}s)</span>
              <span>{fmt(duration)}</span>
            </div>
          </div>

          {/* Numeric fallback */}
          <div className="mt-4 flex flex-wrap items-end gap-4">
            <label className="text-sm">Trim start (s)
              <input
                type="number" min={origStart} max={origEnd} step={0.1} value={Number(start.toFixed(1))}
                onChange={(e) => { setStart(clampStart(Number(e.target.value))); setSaved(false); }}
                className="mt-1 block w-24 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5"
              />
            </label>
            <label className="text-sm">Trim end (s)
              <input
                type="number" min={origStart} max={origEnd} step={0.1} value={Number(end.toFixed(1))}
                onChange={(e) => { setEnd(clampEnd(Number(e.target.value))); setSaved(false); }}
                className="mt-1 block w-24 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5"
              />
            </label>
            <span className="text-sm text-neutral-400">Duration: {trimDuration.toFixed(1)}s</span>
            <button
              onClick={saveTrim}
              disabled={saving}
              className="ml-auto rounded-md bg-amber-500 px-4 py-2 text-sm font-medium text-black hover:bg-amber-400 disabled:opacity-50"
            >
              {saving ? "Saving…" : saved ? "Trim saved ✓" : "Save trim"}
            </button>
          </div>
          {note && (
            <p className={`mt-2 text-sm ${noteKind === "error" ? "text-red-300" : noteKind === "ok" ? "text-emerald-300" : "text-neutral-400"}`} role="status">
              {note}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
