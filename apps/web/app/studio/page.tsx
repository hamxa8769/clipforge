// Clip Studio (v0.8.0) — AI clip suggestions + manual timeline editing.
// Facecam Studio (v0.10.0) — a REAL render step: split-screen
// facecam/gameplay layouts, draggable boxes on a 9:16 phone canvas,
// PiP / Zoom / Blur Fill / Fullscreen modes, per-creator presets, and
// animated captions, all rendered by POST /facecam/render through the
// same render_clip the job queue uses. The layout + caption style are
// also accepted on POST /jobs (facecam_layout) so queued jobs render
// every candidate with the layout.
//
//   - Paste your own video URL (or a clip file URL) for the preview, the
//     duration, and transcript lines ("start-end: text") — the analysis
//     needs transcript text (at least 15 seconds of it) to score anything.
//     POST /detection/analyze returns AI suggestions with a viral score
//     0-100 and a per-signal breakdown.
//   - The timeline at the bottom shows the chosen clip range; drag the
//     handles or type start/end. Trim snaps the range to the selected
//     suggestion; Ripple-cut marks the range as cut out of the clip.
//
// Rights guardrail: clips are made ONLY from your own videos or Creative
// Commons / licensed / public-domain videos — never another creator's
// standard-licensed video. Until the analysis endpoint lands, the page
// says so plainly and never shows invented suggestions.
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { analyzeDetection, API_URL, type DetectionCandidate } from "@/lib/api";
import {
  boxToStyle,
  captionYFrac,
  clampBox,
  defaultFacecamLayout,
  deleteFacecamPreset,
  getFacecamPresets,
  getFacecamStyles,
  pipRectFrac,
  renderFacecamClip,
  saveFacecamPreset,
  splitBandFrac,
  transcriptLinesToWords,
  type CaptionStyleInfo,
  type FacecamBox,
  type FacecamLayout,
  type FacecamMode,
  type FacecamRenderResult,
} from "@/lib/facecam";
import {
  Badge, Card, CardHeader, EmptyState, ErrorBanner, PageHeader,
  PrimaryButton, SecondaryButton, Skeleton,
} from "@/components/ui";

const GUARDRAIL =
  "Make sure you have the right to use the videos you clip — your own video, "
  + "a licensed video, or a use YouTube permits. You are responsible for the "
  + "rights to what you post.";

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
  const [captionStyle, setCaptionStyle] = useState("bounce");
  const [aspect, setAspect] = useState<"9:16" | "1:1" | "16:9">("9:16");
  const [notice, setNotice] = useState<string | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);

  // Facecam Studio (v0.10.0) — layout, presets, render.
  const [facecamLayout, setFacecamLayout] = useState<FacecamLayout>(() => defaultFacecamLayout("split"));
  const [captionStyles, setCaptionStyles] = useState<CaptionStyleInfo[]>([]);
  const [presetCreator, setPresetCreator] = useState("default");
  const [presetName, setPresetName] = useState("");
  const [presetNames, setPresetNames] = useState<string[]>([]);
  const [ownContent, setOwnContent] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [renderResult, setRenderResult] = useState<FacecamRenderResult | null>(null);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [videoDims, setVideoDims] = useState<{ w: number; h: number } | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    getFacecamStyles().then((s) => {
      if (!cancelled && s) setCaptionStyles(s.caption_styles);
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getFacecamPresets(presetCreator).then((p) => {
      if (!cancelled && p) setPresetNames(Object.keys(p).sort());
    });
    return () => { cancelled = true; };
  }, [presetCreator]);

  async function refreshPresets() {
    const p = await getFacecamPresets(presetCreator);
    if (p) setPresetNames(Object.keys(p).sort());
  }

  async function onSavePreset() {
    if (!presetName.trim()) { setNotice("Give the preset a name first."); return; }
    const res = await saveFacecamPreset(presetCreator, presetName.trim(), facecamLayout);
    if (res === null) { setNotice("Could not reach the server to save the preset."); return; }
    if (res.error) { setNotice(`Could not save preset: ${res.error}`); return; }
    setNotice(`Preset “${presetName.trim()}” saved for creator “${presetCreator}”.`);
    await refreshPresets();
  }

  async function onLoadPreset(name: string) {
    const p = await getFacecamPresets(presetCreator);
    if (!p || !p[name]) { setNotice(`Preset “${name}” not found.`); return; }
    setFacecamLayout(p[name]);
    setPresetName(name);
    setNotice(`Preset “${name}” loaded — drag the boxes to adjust it.`);
  }

  async function onDeletePreset(name: string) {
    const ok = await deleteFacecamPreset(presetCreator, name);
    setNotice(ok ? `Preset “${name}” deleted.` : `Could not delete preset “${name}”.`);
    await refreshPresets();
  }

  async function onRenderFacecam() {
    setRendering(true);
    setRenderError(null);
    setRenderResult(null);
    if (!videoUrl.trim()) {
      setRendering(false);
      setRenderError("Paste a video file URL above first — the render needs a source video.");
      return;
    }
    if (clipEnd - clipStart < 0.5) {
      setRendering(false);
      setRenderError("Pick a clip range of at least 0.5 s on the timeline.");
      return;
    }
    const words = transcriptLinesToWords(parseSegments());
    const res = await renderFacecamClip({
      source_url: videoUrl.trim(),
      own_content_declaration: ownContent,
      start_s: clipStart,
      end_s: clipEnd,
      layout: facecamLayout,
      caption_style: captionStyle === "none" ? "bold-pop" : captionStyle,
      words: captionStyle === "none" ? [] : words,
    });
    setRendering(false);
    if (res === null) { setRenderError("Could not reach the server to render."); return; }
    if (res.error) { setRenderError(res.error); return; }
    if (res.result) {
      setRenderResult(res.result);
      setNotice(`Facecam clip rendered: ${res.result.render.width}×${res.result.render.height}, ${res.result.render.duration_s.toFixed(1)} s, ${res.result.render.video_codec}.`);
    }
  }

  // Drag a layout box on the 9:16 canvas: pointer position (in the
  // displayed video rect) -> normalised source-frame coordinates.
  // The video is fitted (object-contain) into the 9:16 canvas, so the
  // video rect is a pure function of the two aspect ratios.
  const videoRect = useCallback(() => {
    if (!videoDims) return { x: 0, y: 0, w: 1, h: 1 };
    const canvasAspect = 9 / 16;
    const videoAspect = videoDims.w / Math.max(1, videoDims.h);
    if (videoAspect >= canvasAspect) {
      const h = canvasAspect / videoAspect;
      return { x: 0, y: (1 - h) / 2, w: 1, h };
    }
    const w = videoAspect / canvasAspect;
    return { x: (1 - w) / 2, y: 0, w, h: 1 };
  }, [videoDims])();

  function overlayStyle(box: FacecamBox): React.CSSProperties {
    const b = clampBox(box);
    return {
      left: `${(videoRect.x + b.x * videoRect.w) * 100}%`,
      top: `${(videoRect.y + b.y * videoRect.h) * 100}%`,
      width: `${b.w * videoRect.w * 100}%`,
      height: `${b.h * videoRect.h * 100}%`,
    };
  }

  const dragBox = useCallback((which: "facecam" | "gameplay", resize: boolean) => (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const vw = rect.width * videoRect.w;
    const vh = rect.height * videoRect.h;
    if (vw <= 0 || vh <= 0) return;
    const startBox = { ...(which === "facecam" ? facecamLayout.facecam : facecamLayout.gameplay) };
    const startX = e.clientX;
    const startY = e.clientY;
    const move = (ev: PointerEvent) => {
      const dx = (ev.clientX - startX) / vw;
      const dy = (ev.clientY - startY) / vh;
      const next = clampBox(resize
        ? { ...startBox, w: startBox.w + dx, h: startBox.h + dy }
        : { ...startBox, x: startBox.x + dx, y: startBox.y + dy });
      setFacecamLayout((prev) => ({ ...prev, [which]: next }));
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }, [facecamLayout.facecam, facecamLayout.gameplay, videoRect]);

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
            <CardHeader title="Facecam Studio" hint="Drag the boxes on the 9:16 phone canvas — then render the clip for real" />
            <div className="flex flex-wrap gap-6">
              {/* 9:16 phone canvas with draggable layout boxes */}
              <div className="shrink-0">
                <div ref={canvasRef} className="relative aspect-[9/16] w-[280px] overflow-hidden rounded-lg border border-neutral-700 bg-black">
                  {videoUrl ? (
                    <video
                      key={videoUrl}
                      src={videoUrl}
                      controls
                      className="absolute inset-0 h-full w-full object-contain"
                      aria-label="Facecam Studio canvas preview"
                      onLoadedMetadata={(e) => {
                        const v = e.currentTarget;
                        if (v.videoWidth > 0) setVideoDims({ w: v.videoWidth, h: v.videoHeight });
                      }}
                    />
                  ) : (
                    <div className="absolute inset-0 flex items-center justify-center p-4 text-center text-sm text-neutral-500">
                      Paste a video file URL above to see the source under the boxes.
                    </div>
                  )}
                  {/* Split divider + caption line (canvas fractions) */}
                  {facecamLayout.mode === "split" && (
                    <div className="pointer-events-none absolute inset-x-0 h-0.5 bg-cyan-400" style={{ top: `${splitBandFrac(facecamLayout) * 100}%` }} title="Facecam / gameplay divider" />
                  )}
                  <div className="pointer-events-none absolute inset-x-0 border-t border-dashed border-amber-300/70" style={{ top: `${captionYFrac(facecamLayout) * 100}%` }} title="Caption line" />
                  {/* PiP rect (canvas fractions) */}
                  {facecamLayout.mode === "pip" && (
                    <div className="pointer-events-none absolute rounded border-2 border-dashed border-cyan-300/80" style={boxToStyle(pipRectFrac(facecamLayout))} title="Facecam PiP position" />
                  )}
                  {/* Draggable source boxes (facecam cyan, gameplay amber) */}
                  <div
                    role="slider" aria-label="Facecam box" aria-valuetext={`x ${Math.round(facecamLayout.facecam.x * 100)}%, y ${Math.round(facecamLayout.facecam.y * 100)}%, ${Math.round(facecamLayout.facecam.w * 100)}% wide`}
                    onPointerDown={dragBox("facecam", false)}
                    className="absolute cursor-move touch-none rounded border-2 border-cyan-400 bg-cyan-400/10"
                    style={overlayStyle(facecamLayout.facecam)}
                    title="Facecam box — drag to move, drag the corner to resize"
                  >
                    <span className="absolute left-1 top-0.5 text-[10px] font-bold uppercase tracking-wide text-cyan-200">Facecam</span>
                    <div onPointerDown={dragBox("facecam", true)} className="absolute -bottom-1.5 -right-1.5 h-4 w-4 cursor-nwse-resize rounded-sm border border-cyan-200 bg-cyan-400" title="Resize facecam box" />
                  </div>
                  <div
                    role="slider" aria-label="Gameplay box" aria-valuetext={`x ${Math.round(facecamLayout.gameplay.x * 100)}%, y ${Math.round(facecamLayout.gameplay.y * 100)}%, ${Math.round(facecamLayout.gameplay.w * 100)}% wide`}
                    onPointerDown={dragBox("gameplay", false)}
                    className="absolute cursor-move touch-none rounded border-2 border-amber-400 bg-amber-400/10"
                    style={overlayStyle(facecamLayout.gameplay)}
                    title="Gameplay box — drag to move, drag the corner to resize"
                  >
                    <span className="absolute left-1 top-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-200">Gameplay</span>
                    <div onPointerDown={dragBox("gameplay", true)} className="absolute -bottom-1.5 -right-1.5 h-4 w-4 cursor-nwse-resize rounded-sm border border-amber-200 bg-amber-400" title="Resize gameplay box" />
                  </div>
                </div>
                <p className="mt-2 w-[280px] text-xs text-neutral-500">
                  Boxes are fractions of the source frame, so a preset works on any resolution. Cyan = facecam crop, amber = gameplay crop, dashed line = caption line.
                </p>
              </div>

              {/* Layout + caption + preset controls */}
              <div className="min-w-[240px] flex-1 space-y-4">
                <fieldset>
                  <legend className="text-sm font-medium text-neutral-100">Layout mode</legend>
                  <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Layout mode">
                    {(["split", "pip", "zoom", "blur_fill", "fullscreen"] as const).map((m) => (
                      <button key={m} type="button" role="radio" aria-checked={facecamLayout.mode === m} onClick={() => setFacecamLayout((prev) => ({ ...prev, mode: m as FacecamMode }))} className={`rounded-md border px-3 py-1.5 text-sm ${facecamLayout.mode === m ? "border-cyan-400 bg-cyan-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}>
                        {m === "split" ? "Split screen" : m === "pip" ? "Picture-in-picture" : m === "zoom" ? "Zoom + blur" : m === "blur_fill" ? "Blur fill" : "Fullscreen"}
                      </button>
                    ))}
                  </div>
                  <p className="mt-1 text-xs text-neutral-500">
                    {facecamLayout.mode === "split" && "Facecam on top, gameplay below, glowing divider between them."}
                    {facecamLayout.mode === "pip" && "Gameplay fills the frame; the facecam sits in a corner."}
                    {facecamLayout.mode === "zoom" && "Gameplay zoomed over a blurred copy of the frame."}
                    {facecamLayout.mode === "blur_fill" && "16:9 video over a blurred background — no facecam box used."}
                    {facecamLayout.mode === "fullscreen" && "Gameplay fills the frame — no facecam box used."}
                  </p>
                </fieldset>
                {facecamLayout.mode === "split" && (
                  <label className="block text-sm">
                    <span className="text-neutral-400">Facecam share of the height ({Math.round(facecamLayout.split_ratio * 100)}%)</span>
                    <input type="range" min={0.2} max={0.8} step={0.05} value={facecamLayout.split_ratio} onChange={(e) => setFacecamLayout((prev) => ({ ...prev, split_ratio: Number(e.target.value) }))} className="mt-1 w-full accent-cyan-400" aria-label="Facecam share of the height" />
                  </label>
                )}
                {facecamLayout.mode === "pip" && (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="block text-sm">
                      <span className="text-neutral-400">PiP corner</span>
                      <select className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={facecamLayout.pip_pos} onChange={(e) => setFacecamLayout((prev) => ({ ...prev, pip_pos: e.target.value as FacecamLayout["pip_pos"] }))} aria-label="PiP corner">
                        <option value="top-right">Top right</option>
                        <option value="top-left">Top left</option>
                        <option value="bottom-left">Bottom left</option>
                        <option value="bottom-right">Bottom right</option>
                      </select>
                    </label>
                    <label className="block text-sm">
                      <span className="text-neutral-400">PiP width ({Math.round(facecamLayout.pip_size_frac * 100)}% of frame)</span>
                      <input type="range" min={0.1} max={0.6} step={0.05} value={facecamLayout.pip_size_frac} onChange={(e) => setFacecamLayout((prev) => ({ ...prev, pip_size_frac: Number(e.target.value) }))} className="mt-1 w-full accent-cyan-400" aria-label="PiP width" />
                    </label>
                  </div>
                )}
                {facecamLayout.mode === "split" && (
                  <label className="flex items-start gap-3 text-sm">
                    <input type="checkbox" className="mt-0.5 h-4 w-4 accent-cyan-400" checked={facecamLayout.divider} onChange={(e) => setFacecamLayout((prev) => ({ ...prev, divider: e.target.checked }))} />
                    <span><span className="font-medium text-neutral-100">Glowing divider line</span><span className="mt-0.5 block text-xs text-neutral-400">Dark bar + cyan line between facecam and gameplay.</span></span>
                  </label>
                )}
                <label className="block text-sm">
                  <span className="text-neutral-400">Caption style (word-by-word, burned into the render)</span>
                  <select className="mt-1 w-full max-w-xs rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={captionStyle} onChange={(e) => setCaptionStyle(e.target.value)} aria-label="Caption style">
                    <option value="none">No captions</option>
                    {captionStyles.length > 0 ? captionStyles.map((s) => (
                      <option key={s.id} value={s.id}>{s.label}{s.animated ? " (animated)" : ""}</option>
                    )) : (
                      <>
                        <option value="bounce">Bounce (animated)</option>
                        <option value="hormozi">Hormozi (Cyan Pop) (animated)</option>
                        <option value="mrbeast">MrBeast (Yellow/Orange) (animated)</option>
                        <option value="karaoke">Karaoke (Color Wipe) (animated)</option>
                        <option value="hype">Hype (animated)</option>
                        <option value="clean">Clean (animated)</option>
                        <option value="bold-pop">Bold Pop</option>
                        <option value="karaoke-fill">Karaoke Fill</option>
                        <option value="boxed">Boxed</option>
                      </>
                    )}
                  </select>
                </label>
                <p className="text-xs text-neutral-500">Caption words come from the transcript lines above, spaced evenly across each line — estimated timing, labelled as such in the render. For exact word timing, run a job (Whisper) instead.</p>

                <fieldset className="rounded-lg border border-neutral-800 p-3">
                  <legend className="px-1 text-sm font-medium text-neutral-100">Per-creator preset</legend>
                  <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
                    <label className="block text-sm">
                      <span className="text-neutral-400">Creator</span>
                      <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={presetCreator} onChange={(e) => setPresetCreator(e.target.value)} aria-label="Creator name" />
                    </label>
                    <label className="block text-sm">
                      <span className="text-neutral-400">Preset name</span>
                      <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={presetName} onChange={(e) => setPresetName(e.target.value)} placeholder="e.g. GTA facecam" aria-label="Preset name" />
                    </label>
                    <span className="self-end"><SecondaryButton onClick={onSavePreset}>Save preset</SecondaryButton></span>
                  </div>
                  {presetNames.length > 0 && (
                    <ul className="mt-3 space-y-1">
                      {presetNames.map((n) => (
                        <li key={n} className="flex items-center gap-2 text-sm">
                          <button type="button" onClick={() => onLoadPreset(n)} className="font-medium text-cyan-300 hover:underline">{n}</button>
                          <span className="text-neutral-500">— load</span>
                          <button type="button" onClick={() => onDeletePreset(n)} className="text-neutral-500 hover:text-red-300">delete</button>
                        </li>
                      ))}
                    </ul>
                  )}
                </fieldset>

                <label className="flex items-start gap-3 text-sm">
                  <input type="checkbox" className="mt-0.5 h-4 w-4 accent-amber-400" checked={ownContent} onChange={(e) => setOwnContent(e.target.checked)} />
                  <span><span className="font-medium text-neutral-100">This is my own video (or I have the right to clip it)</span><span className="mt-0.5 block text-xs text-neutral-400">Needed for YouTube sources. You are responsible for the rights to what you post.</span></span>
                </label>
                <div className="flex flex-wrap items-center gap-2">
                  <PrimaryButton onClick={onRenderFacecam} disabled={rendering}>{rendering ? "Rendering…" : `Render facecam clip (${fmtTime(clipStart)} – ${fmtTime(clipEnd)})`}</PrimaryButton>
                  <span className="text-xs text-neutral-500">Renders the timeline range (max 5 min) with this layout + captions, 1080×1920.</span>
                </div>
                {renderError && <ErrorBanner message={renderError} onRetry={onRenderFacecam} />}
                {renderResult && (
                  <div className="space-y-2 rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm">
                    <p className="font-medium text-emerald-100">Rendered — {renderResult.render.width}×{renderResult.render.height} · {renderResult.render.duration_s.toFixed(1)} s · {renderResult.render.video_codec} · {(renderResult.render.size_bytes / 1024).toFixed(0)} KB</p>
                    <video src={`${API_URL}${renderResult.media_url}`} controls className="aspect-[9/16] w-full max-w-[220px] rounded-lg bg-black" aria-label="Rendered facecam clip" />
                    <p>
                      <a className="text-cyan-300 hover:underline" href={`${API_URL}${renderResult.media_url}`} target="_blank" rel="noreferrer">Download the clip</a>
                      {renderResult.ass_media_url && (<> · <a className="text-cyan-300 hover:underline" href={`${API_URL}${renderResult.ass_media_url}`} target="_blank" rel="noreferrer">Captions (.ass)</a></>)}
                    </p>
                  </div>
                )}
              </div>
            </div>
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
