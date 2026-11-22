// Clip Studio (v0.8.0) — AI clip suggestions + manual timeline editing.
//
//   - Paste your own video URL (or a clip file URL) for the preview, the
//     duration, and transcript lines ("start-end: text") — the analysis
//     needs transcript text (at least 15 seconds of it) to score anything.
//     POST /detection/analyze returns AI suggestions with a viral score
//     0-100 and a per-signal breakdown.
//   - The timeline at the bottom shows the chosen clip range; drag the
//     handles or type start/end. Trim snaps the range to the selected
//     suggestion; Ripple-cut marks the range as cut out of the clip.
//   - Manual Pro Mode (caption style, punch-in zoom, aspect ratio) is a
//     clearly-labelled preview-only section — simple by default. The
//     settings are not saved or sent to the renderer: Studio has no
//     render step yet, so the section says so plainly instead of
//     promising the settings are "applied when rendered".
//
// Rights guardrail: clips are made ONLY from your own videos or Creative
// Commons / licensed / public-domain videos — never another creator's
// standard-licensed video. Until the analysis endpoint lands, the page
// says so plainly and never shows invented suggestions.
"use client";
import { useCallback, useRef, useState } from "react";
import { analyzeDetection, type DetectionCandidate } from "@/lib/api";
import {
  Badge, Card, CardHeader, EmptyState, ErrorBanner, PageHeader,
  PrimaryButton, SecondaryButton, Skeleton,
} from "@/components/ui";

const GUARDRAIL =
  "Clips are made only from your own videos or Creative Commons / licensed / public-domain videos. "
  + "Never re-upload another creator's standard-licensed video.";

const SIGNALS: { key: keyof Pick<DetectionCandidate, "hook_score" | "peak_score" | "payoff_score" | "emotion_score" | "visual_score" | "completeness_score">; label: string }[] = [
  { key: "hook_score", label: "Hook" },
  { key: "peak_score", label: "Peak" },
  { key: "payoff_score", label: "Payoff" },
  { key: "emotion_score", label: "Emotion" },
  { key: "visual_score", label: "Visual" },
  { key: "completeness_score", label: "Completeness" },
];

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function scoreTone(score: number): string {
  if (score >= 70) return "bg-emerald-500/15 text-emerald-300";
  if (score >= 40) return "bg-amber-500/15 text-amber-300";
  return "bg-neutral-700/50 text-neutral-300";
}

function SignalBar({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-24 shrink-0 text-xs text-neutral-400">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-neutral-800">
        <div className="h-full rounded-full bg-amber-400 transition-all" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
      </div>
      <span className="w-8 shrink-0 text-right text-xs font-medium text-neutral-200">{Math.round(value)}</span>
    </div>
  );
}

export default function StudioPage() {
  const [videoUrl, setVideoUrl] = useState("");
  const [duration, setDuration] = useState(600);
  const [niche, setNiche] = useState("");
  const [transcript, setTranscript] = useState(
    "0-15: welcome back to the channel\n15-40: the big reveal",
  );
  const [candidates, setCandidates] = useState<DetectionCandidate[] | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Manual clip range (seconds) + ripple cuts.
  const [clipStart, setClipStart] = useState(0);
  const [clipEnd, setClipEnd] = useState(60);
  const [cuts, setCuts] = useState<{ start: number; end: number }[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [captionStyle, setCaptionStyle] = useState("bold");
  const [punchIn, setPunchIn] = useState(false);
  const [aspect, setAspect] = useState<"9:16" | "1:1" | "16:9">("9:16");
  const [notice, setNotice] = useState<string | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);

  const safeDuration = Number.isFinite(duration) && duration > 0 ? duration : 600;
  const startPct = (clipStart / safeDuration) * 100;
  const endPct = (clipEnd / safeDuration) * 100;

  function parseSegments(): { start: number; end: number; text?: string }[] {
    const lines = transcript.split("\n").map((l) => l.trim()).filter(Boolean);
    const parsed: { start: number; end: number; text?: string }[] = [];
    for (const line of lines) {
      const m = line.match(/^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*:?\s*(.*)$/);
      if (m) parsed.push({ start: Number(m[1]), end: Number(m[2]), text: m[3] || undefined });
    }
    if (parsed.length === 0) return [{ start: 0, end: safeDuration }];
    return parsed;
  }

  async function onAnalyze() {
    setAnalyzing(true);
    setError(null);
    setCandidates(null);
    if (parseSegments().length === 0 || transcript.trim() === "") {
      setAnalyzing(false);
      setError("Paste transcript lines first — one per line, like “0-15: the hook”. Without a transcript the analysis has nothing to score.");
      return;
    }
    const { candidates: found, error: err } = await analyzeDetection(parseSegments(), safeDuration, niche.trim() || undefined);
    setAnalyzing(false);
    if (err) { setError(err); return; }
    setCandidates(found);
    if (found.length === 0) setError("No clip suggestions came back for this video — try a longer transcript or a different niche.");
  }

  function pickCandidate(i: number) {
    const c = (candidates ?? [])[i];
    if (!c) return;
    setSelected(i);
    setClipStart(Math.max(0, Math.min(c.start, safeDuration)));
    setClipEnd(Math.max(c.start, Math.min(c.end, safeDuration)));
    setNotice(`Clip range set to ${fmtTime(c.start)} – ${fmtTime(c.end)} (${c.title || "untitled suggestion"}).`);
  }

  function trimToSelected() {
    if (selected === null) { setNotice("Pick an AI suggestion first, then Trim."); return; }
    pickCandidate(selected);
  }

  function rippleCut() {
    if (clipEnd <= clipStart) { setNotice("Pick a range to cut first (end must be after start)."); return; }
    setCuts((prev) => [...prev, { start: clipStart, end: clipEnd }]);
    setNotice(`Cut ${fmtTime(clipStart)} – ${fmtTime(clipEnd)} out of the clip. The rest ripples together.`);
  }

  // Drag a timeline handle: pointer position -> seconds.
  const dragHandle = useCallback((which: "start" | "end") => (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const el = timelineRef.current;
    if (!el) return;
    const move = (ev: PointerEvent) => {
      const rect = el.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
      const t = Math.round(frac * safeDuration * 10) / 10;
      if (which === "start") setClipStart((prev) => Math.max(0, Math.min(t, clipEnd - 1)));
      else setClipEnd((prev) => Math.min(safeDuration, Math.max(t, clipStart + 1)));
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }, [clipEnd, clipStart, safeDuration]);

  const aspectClass = aspect === "9:16" ? "aspect-[9/16] max-w-[280px]" : aspect === "1:1" ? "aspect-square max-w-[360px]" : "aspect-video w-full";
  const selectedCandidate = selected !== null ? (candidates ?? [])[selected] ?? null : null;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Clip Studio"
        intro="AI clip suggestions for your own video, a preview, and a simple timeline to trim the clip — pro controls are below, out of the way."
        actions={<Badge tone="info">Pro · v0.8.0</Badge>}
      />
      <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-100" role="note">{GUARDRAIL}</div>
      {notice && <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>}
      {error && <ErrorBanner message={error} onRetry={onAnalyze} />}

      <Card className="glass">
        <CardHeader title="Source video" hint="Your own video, or a licensed / public-domain one" />
        <div className="grid gap-3 md:grid-cols-[1fr_160px_200px]">
          <label className="block text-sm">
            <span className="text-neutral-400">Video file URL (for the preview)</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={videoUrl} onChange={(e) => setVideoUrl(e.target.value)} placeholder="https://…/my-video.mp4 — or leave empty and analyse by transcript" aria-label="Video file URL" />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Duration (seconds)</span>
            <input type="number" min={1} className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={duration} onChange={(e) => setDuration(Number(e.target.value) || 0)} aria-label="Duration in seconds" />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Niche (optional)</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={niche} onChange={(e) => setNiche(e.target.value)} placeholder="e.g. gaming" aria-label="Niche" />
          </label>
        </div>
        <label className="mt-3 block text-sm">
          <span className="text-neutral-400">Transcript lines (required — one per line, “start-end: text”, e.g. 12-24: the hook)</span>
          <textarea rows={3} className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={transcript} onChange={(e) => setTranscript(e.target.value)} placeholder={"0-15: welcome back to the channel\n15-40: the big reveal"} aria-label="Transcript lines" />
        </label>
        <div className="mt-3 flex flex-wrap gap-2">
          <PrimaryButton onClick={onAnalyze} disabled={analyzing}>{analyzing ? "Analysing…" : "Analyse — find viral clips"}</PrimaryButton>
          <span className="self-center text-xs text-neutral-500">Analysis needs transcript lines — a clip needs at least 15 seconds of transcript to score. Paste the lines above.</span>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
        {/* Preview + timeline */}
        <div className="space-y-4">
          <Card className="glass">
            <CardHeader title="Preview" hint={videoUrl ? "Your video file" : "Paste a video URL above to preview"} />
            {videoUrl ? (
              <div className="flex justify-center">
                <video key={videoUrl} src={videoUrl} controls className={`w-full rounded-lg bg-black ${aspectClass}`} aria-label="Clip preview" />
              </div>
            ) : (
              <div className={`mx-auto flex items-center justify-center rounded-lg border border-dashed border-neutral-700 bg-neutral-950 text-sm text-neutral-500 ${aspectClass}`}>
                No preview yet — paste a video file URL above.
              </div>
            )}
            {selectedCandidate && (
              <p className="mt-3 text-sm text-neutral-300">
                Selected: <span className="font-medium text-neutral-100">{selectedCandidate.title || "Untitled suggestion"}</span>{" "}
                · {fmtTime(selectedCandidate.start)} – {fmtTime(selectedCandidate.end)} · viral score {Math.round(selectedCandidate.score)}/100
              </p>
            )}
          </Card>

          <Card className="glass">
            <CardHeader title="Timeline" hint="Drag the handles or type the times" />
            <div ref={timelineRef} className="relative h-12 rounded-lg bg-neutral-950" role="group" aria-label="Clip timeline">
              <div className="absolute inset-y-0 rounded-lg bg-amber-400/25" style={{ left: `${startPct}%`, width: `${Math.max(0, endPct - startPct)}%` }} />
              {cuts.map((c, i) => (
                <div key={i} className="absolute inset-y-0 bg-red-500/40" style={{ left: `${(c.start / safeDuration) * 100}%`, width: `${((c.end - c.start) / safeDuration) * 100}%` }} title={`Cut out: ${fmtTime(c.start)} – ${fmtTime(c.end)}`} />
              ))}
              <div
                onPointerDown={dragHandle("start")}
                role="slider" aria-label="Clip start" aria-valuenow={Math.round(clipStart)} aria-valuemin={0} aria-valuemax={safeDuration}
                className="absolute top-0 h-full w-3 -translate-x-1/2 cursor-ew-resize rounded bg-amber-400"
                style={{ left: `${startPct}%` }}
              />
              <div
                onPointerDown={dragHandle("end")}
                role="slider" aria-label="Clip end" aria-valuenow={Math.round(clipEnd)} aria-valuemin={0} aria-valuemax={safeDuration}
                className="absolute top-0 h-full w-3 -translate-x-1/2 cursor-ew-resize rounded bg-amber-400"
                style={{ left: `${endPct}%` }}
              />
              <span className="absolute left-2 top-1 text-xs text-neutral-400">0:00</span>
              <span className="absolute right-2 top-1 text-xs text-neutral-400">{fmtTime(safeDuration)}</span>
            </div>
            <div className="mt-3 flex flex-wrap items-end gap-3">
              <label className="text-sm">
                <span className="text-neutral-400">Start (s)</span>
                <input type="number" min={0} max={safeDuration} step={0.1} className="ml-2 w-24 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-sm" value={clipStart} onChange={(e) => setClipStart(Math.max(0, Math.min(Number(e.target.value) || 0, clipEnd - 0.1)))} aria-label="Clip start in seconds" />
              </label>
              <label className="text-sm">
                <span className="text-neutral-400">End (s)</span>
                <input type="number" min={0} max={safeDuration} step={0.1} className="ml-2 w-24 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-sm" value={clipEnd} onChange={(e) => setClipEnd(Math.min(safeDuration, Math.max(Number(e.target.value) || 0, clipStart + 0.1)))} aria-label="Clip end in seconds" />
              </label>
              <span className="text-sm text-neutral-300">Length: {fmtTime(Math.max(0, clipEnd - clipStart))}</span>
              <span className="flex flex-wrap gap-2">
                <SecondaryButton onClick={trimToSelected} disabled={selected === null}>Trim to suggestion</SecondaryButton>
                <SecondaryButton onClick={rippleCut}>Ripple-cut range</SecondaryButton>
                {cuts.length > 0 && <SecondaryButton onClick={() => { setCuts([]); setNotice("Cuts cleared."); }}>Undo cuts ({cuts.length})</SecondaryButton>}
              </span>
            </div>
            {cuts.length > 0 && (
              <ul className="mt-3 space-y-1 text-xs text-neutral-400">
                {cuts.map((c, i) => <li key={i}>Cut out: {fmtTime(c.start)} – {fmtTime(c.end)}</li>)}
              </ul>
            )}
          </Card>

          <Card className="glass">
            <details>
              <summary className="cursor-pointer text-lg font-semibold text-neutral-100">Manual Pro Mode <span className="ml-1 text-xs font-normal text-neutral-500">— caption style, zoom &amp; aspect ratio</span></summary>
              <div className="mt-4 space-y-4">
                <label className="block text-sm">
                  <span className="text-neutral-400">Caption style</span>
                  <select className="mt-1 w-full max-w-xs rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={captionStyle} onChange={(e) => setCaptionStyle(e.target.value)} aria-label="Caption style">
                    <option value="bold">Bold bottom (default)</option>
                    <option value="karaoke">Karaoke (word by word)</option>
                    <option value="minimal">Minimal</option>
                    <option value="none">No captions</option>
                  </select>
                </label>
                <label className="flex items-start gap-3 text-sm">
                  <input type="checkbox" className="mt-0.5 h-4 w-4 accent-amber-400" checked={punchIn} onChange={(e) => setPunchIn(e.target.checked)} />
                  <span><span className="font-medium text-neutral-100">Punch-in zoom</span><span className="mt-0.5 block text-xs text-neutral-400">Zoom in slightly on the speaker during the clip for a punchier look.</span></span>
                </label>
                <fieldset>
                  <legend className="text-sm font-medium text-neutral-100">Aspect ratio</legend>
                  <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Aspect ratio">
                    {(["9:16", "1:1", "16:9"] as const).map((a) => (
                      <button key={a} type="button" role="radio" aria-checked={aspect === a} onClick={() => setAspect(a)} className={`rounded-md border px-3 py-1.5 text-sm ${aspect === a ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}>{a}{a === "9:16" ? " (Shorts / Reels)" : a === "1:1" ? " (Feed)" : " (Landscape)"}</button>
                    ))}
                  </div>
                </fieldset>
                <p className="text-xs text-neutral-500">Preview only for now — these settings are not saved and are not sent to the renderer yet; there is no render step from Studio. Use them to preview the look, then create the clip from a job. Captions: {captionStyle} · Punch-in: {punchIn ? "on" : "off"} · Aspect: {aspect}.</p>
              </div>
            </details>
          </Card>
        </div>

        {/* AI suggestions */}
        <div className="space-y-4">
          <h2 className="text-lg font-semibold text-neutral-100">AI clip suggestions</h2>
          {analyzing ? (
            <div className="space-y-3"><Skeleton className="h-44" /><Skeleton className="h-44" /></div>
          ) : candidates === null ? (
            <EmptyState title="No suggestions yet" body="Press “Analyse — find viral clips” above. Suggestions come from the analysis service for your own video — nothing here is invented." />
          ) : candidates.length === 0 ? (
            <EmptyState title="No suggestions found" body="The analysis service returned no clips for this video. Try adding transcript lines or a different niche." />
          ) : (
            <ul className="space-y-3">
              {candidates.map((c, i) => (
                <li key={i} className={`rounded-xl border p-4 transition-colors ${selected === i ? "border-amber-400 bg-amber-400/5" : "border-neutral-800 bg-neutral-900"}`}>
                  <div className="flex items-start gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="font-semibold text-neutral-50">{c.title || `Clip ${i + 1}`}</p>
                      <p className="mt-0.5 text-sm text-neutral-400">{fmtTime(c.start)} – {fmtTime(c.end)} · {fmtTime(Math.max(0, c.end - c.start))} long</p>
                    </div>
                    <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${scoreTone(c.score)}`}>Viral {Math.round(c.score)}/100</span>
                  </div>
                  <div className="mt-3 space-y-1.5">
                    {SIGNALS.map((s) => <SignalBar key={s.key} label={s.label} value={c[s.key]} />)}
                  </div>
                  {c.reason && <p className="mt-3 text-xs leading-relaxed text-neutral-400">{c.reason}</p>}
                  <div className="mt-3">
                    <PrimaryButton onClick={() => pickCandidate(i)}>{selected === i ? "Selected ✓" : "Use this clip"}</PrimaryButton>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
