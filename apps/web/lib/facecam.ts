// Facecam Studio helpers (v0.10.0) — pure box math + API client.
//
// Boxes are normalised fractions of the source frame (0..1), exactly
// as the backend stores them (clipforge.facecam.Box), so a preset
// saved for one video still applies to another resolution. The Studio
// canvas is 9:16 (1080x1920); the source video is drawn into it with
// the layout's mode, and the boxes are drawn on top as draggable
// overlays. All conversions here are pure functions, unit-tested in
// lib/facecam.test.ts.
import { API_URL } from "./api";

export type FacecamMode = "split" | "pip" | "zoom" | "blur_fill" | "fullscreen";
export type PipPosition = "top-right" | "top-left" | "bottom-left" | "bottom-right";

export interface FacecamBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface FacecamLayout {
  mode: FacecamMode;
  facecam: FacecamBox;
  gameplay: FacecamBox;
  split_ratio: number;
  pip_pos: PipPosition;
  pip_size_frac: number;
  divider: boolean;
  caption_y: number | null;
}

export const FACECAM_MODES: readonly FacecamMode[] = [
  "split",
  "pip",
  "zoom",
  "blur_fill",
  "fullscreen",
];

export function defaultFacecamLayout(mode: FacecamMode = "split"): FacecamLayout {
  return {
    mode,
    // Top strip of the source: where a streamer's webcam usually sits.
    facecam: { x: 0, y: 0, w: 1, h: 0.35 },
    gameplay: { x: 0, y: 0, w: 1, h: 1 },
    split_ratio: 0.35,
    pip_pos: "top-right",
    pip_size_frac: 0.35,
    divider: true,
    caption_y: null,
  };
}

/** Clamp a normalised box into the frame, keeping a usable minimum size. */
export function clampBox(box: FacecamBox): FacecamBox {
  const x = Math.min(Math.max(0, box.x), 0.95);
  const y = Math.min(Math.max(0, box.y), 0.95);
  const w = Math.min(Math.max(0.05, box.w), 1 - x);
  const h = Math.min(Math.max(0.05, box.h), 1 - y);
  return { x, y, w, h };
}

/** Normalised box -> CSS percentages for the overlay on the canvas. */
export function boxToStyle(box: FacecamBox): {
  left: string;
  top: string;
  width: string;
  height: string;
} {
  const b = clampBox(box);
  return {
    left: `${b.x * 100}%`,
    top: `${b.y * 100}%`,
    width: `${b.w * 100}%`,
    height: `${b.h * 100}%`,
  };
}

/** Split mode: facecam band height as a fraction of the 9:16 canvas. */
export function splitBandFrac(layout: FacecamLayout): number {
  return Math.min(0.8, Math.max(0.2, layout.split_ratio));
}

/** Caption line (fraction of canvas height) for a layout, mirroring
 *  the backend's layout_geometry() defaults. */
export function captionYFrac(layout: FacecamLayout): number {
  if (layout.caption_y !== null) return layout.caption_y / 1920;
  if (layout.mode === "split") {
    const camH = splitBandFrac(layout);
    return camH + (1 - camH) * 0.42;
  }
  if (layout.mode === "pip" && layout.pip_pos.startsWith("bottom")) return 0.32;
  return 1120 / 1920;
}

/** PiP overlay rect (fractions of the 1080x1920 canvas) for a layout. */
export function pipRectFrac(layout: FacecamLayout): FacecamBox {
  const pipW = layout.pip_size_frac;
  const faceAspect = layout.facecam.w / Math.max(0.01, layout.facecam.h);
  // PiP height follows the facecam box aspect: the canvas is 9:16, so
  // a width fraction of the 1080-wide canvas is (9/16) as tall per
  // unit of aspect.
  const h = (pipW / Math.max(0.01, faceAspect)) * (9 / 16);
  const margin = 26 / 1080;
  const lift = 240 / 1920;
  switch (layout.pip_pos) {
    case "bottom-left":
      return { x: margin, y: 1 - h - lift, w: pipW, h };
    case "bottom-right":
      return { x: 1 - pipW - margin, y: 1 - h - lift, w: pipW, h };
    case "top-left":
      return { x: margin, y: margin * (16 / 9), w: pipW, h };
    default:
      return { x: 1 - pipW - margin, y: margin * (16 / 9), w: pipW, h };
  }
}

export interface CaptionStyleInfo {
  id: string;
  label: string;
  animated: boolean;
  fontsize: number;
}

export interface FacecamStyles {
  caption_styles: CaptionStyleInfo[];
  layout_modes: { id: FacecamMode; label: string }[];
  pip_positions: PipPosition[];
}

async function apiJson<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const res = await fetch(`${API_URL}${path}`, init);
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function getFacecamStyles(): Promise<FacecamStyles | null> {
  return apiJson<FacecamStyles>("/facecam/styles");
}

export async function getFacecamPresets(
  creator = "default",
): Promise<Record<string, FacecamLayout> | null> {
  const raw = await apiJson<{ creator: string; presets: Record<string, FacecamLayout> }>(
    `/facecam/presets?creator=${encodeURIComponent(creator)}`,
  );
  return raw === null ? null : raw.presets;
}

export async function saveFacecamPreset(
  creator: string,
  name: string,
  layout: FacecamLayout,
): Promise<{ error?: string } | null> {
  const raw = await apiJson<{ creator: string; name: string; layout: FacecamLayout } | { detail: string }>(
    "/facecam/presets",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ creator, name, layout }),
    },
  );
  if (raw === null) return null;
  if ("detail" in raw) return { error: raw.detail };
  return {};
}

export async function deleteFacecamPreset(
  creator: string,
  name: string,
): Promise<boolean> {
  try {
    const res = await fetch(
      `${API_URL}/facecam/presets/${encodeURIComponent(creator)}/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    return res.ok;
  } catch {
    return false;
  }
}

export interface FacecamRenderWord {
  text: string;
  start: number;
  end: number;
}

export interface FacecamRenderResult {
  file: string;
  filename: string;
  media_url: string;
  ass_file: string | null;
  ass_media_url: string | null;
  layout: FacecamLayout;
  caption_style: string;
  render: { width: number; height: number; duration_s: number; video_codec: string; size_bytes: number };
}

export async function renderFacecamClip(input: {
  source_path?: string;
  source_url?: string;
  own_content_declaration?: boolean;
  start_s: number;
  end_s: number;
  layout: FacecamLayout;
  caption_style: string;
  words: FacecamRenderWord[];
}): Promise<{ result?: FacecamRenderResult; error?: string } | null> {
  const raw = await apiJson<FacecamRenderResult | { detail: string }>("/facecam/render", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (raw === null) return null;
  if ("detail" in raw) return { error: String(raw.detail) };
  return { result: raw };
}

/** Turn Studio transcript lines ("start-end: text") into word timings
 *  by spacing each line's words evenly across its span. Approximate —
 *  the server burns exactly these times into the captions — so the UI
 *  labels it as estimated word timing. */
export function transcriptLinesToWords(
  lines: { start: number; end: number; text?: string }[],
): FacecamRenderWord[] {
  const words: FacecamRenderWord[] = [];
  for (const line of lines) {
    const text = (line.text || "").trim();
    if (!text) continue;
    const parts = text.split(/\s+/).filter(Boolean);
    if (parts.length === 0) continue;
    const span = Math.max(0.1, line.end - line.start);
    const per = span / parts.length;
    parts.forEach((t, i) => {
      words.push({ text: t, start: line.start + i * per, end: line.start + (i + 1) * per });
    });
  }
  return words.sort((a, b) => a.start - b.start);
}
