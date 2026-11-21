// Export Hub (v0.8.0) — one-click publish with platform rules enforced
// in the UI, a copyright-scan step, and a thumbnail preview next to the
// clip preview.
//
// Platform rules (enforced before publish, never silently):
//   YouTube Shorts ≤ 60s · TikTok ≤ 60s · Instagram Reels ≤ 90s ·
//   Facebook Reels ≤ 90s · X (video ≤ 140s).
// A clip longer than a platform's limit is flagged for that platform and
// the publish button for it is disabled, with an auto-trim note.
// The copyright scan (POST /publish/scan) runs before any publish: a
// severe flag blocks publishing with a warning — the scan is a check,
// never a guarantee that a platform will accept the clip.
//
// Rights guardrail: clips are made ONLY from your own videos or Creative
// Commons / licensed / public-domain videos.
"use client";
import { useState } from "react";
import { apiPostJson, publishToX, scanPublish, type PublishScanFlag } from "@/lib/api";
import {
  Badge, Card, CardHeader, EmptyState, ErrorBanner, PageHeader,
  PrimaryButton, SecondaryButton,
} from "@/components/ui";

const GUARDRAIL =
  "Only publish clips from your own videos or Creative Commons / licensed / public-domain videos. "
  + "Re-uploading another creator's video can get the clip removed or the channel struck — even as a Short.";

interface PlatformRule {
  id: string; label: string; maxSeconds: number; aspect: string;
}

const PLATFORMS: PlatformRule[] = [
  { id: "youtube", label: "YouTube Shorts", maxSeconds: 60, aspect: "9:16" },
  { id: "tiktok", label: "TikTok", maxSeconds: 60, aspect: "9:16" },
  { id: "instagram", label: "Instagram Reels", maxSeconds: 90, aspect: "9:16" },
  { id: "facebook", label: "Facebook Reels", maxSeconds: 90, aspect: "9:16" },
  { id: "x", label: "X (Twitter)", maxSeconds: 140, aspect: "16:9" },
];

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export default function ExportPage() {
  const [clipId, setClipId] = useState("");
  const [clipUrl, setClipUrl] = useState("");
  const [clipDuration, setClipDuration] = useState(45);
  const [clipAspect, setClipAspect] = useState("9:16");
  const [thumbnailUrl, setThumbnailUrl] = useState("");
  const [caption, setCaption] = useState("");
  const [hashtags, setHashtags] = useState("");
  const [selected, setSelected] = useState<string[]>(["youtube"]);
  const [flags, setFlags] = useState<PublishScanFlag[] | null>(null);
  const [scanned, setScanned] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [publishing, setPublishing] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [published, setPublished] = useState<string[]>([]);

  const safeDuration = Number.isFinite(clipDuration) && clipDuration > 0 ? clipDuration : 0;
  const severe = (flags ?? []).some((f) => f.severity.toLowerCase() === "severe" || f.severity.toLowerCase() === "high" || f.severity.toLowerCase() === "error");
  const blocked = scanned && severe;

  function togglePlatform(id: string) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]));
  }

  async function onScan() {
    if (!clipId.trim()) { setScanError("Enter a clip ID first (the ID of the clip in your approval queue)."); return; }
    setScanning(true);
    setScanError(null);
    const { flags: found, error } = await scanPublish(clipId.trim());
    setScanning(false);
    setScanned(true);
    if (error) { setScanError(error + " The scan service may not be installed in this build yet."); setFlags(null); return; }
    setFlags(found);
  }

  async function onPublish(p: PlatformRule) {
    if (!clipId.trim()) { setNotice("Enter a clip ID first."); return; }
    if (!scanned) { setNotice("Run the copyright scan first — publishing is blocked until the scan has run."); return; }
    if (blocked) { setNotice("Publishing is blocked: the copyright scan found a severe flag. Fix the clip first."); return; }
    if (safeDuration > p.maxSeconds) { setNotice(`${p.label} only takes clips up to ${p.maxSeconds}s — this clip is ${fmtTime(safeDuration)}. Trim it in Clip Studio first.`); return; }
    setPublishing(p.id);
    setNotice(null);
    const text = [caption.trim(), hashtags.trim()].filter(Boolean).join(" ");
    const res = p.id === "x"
      ? await publishToX(clipId.trim(), text)
      : await apiPostJson(`/posts/${encodeURIComponent(clipId.trim())}/publish`, { platform: p.id, caption: text });
    setPublishing(null);
    if (!res.ok) { setNotice(res.error ?? `Could not publish to ${p.label}.`); return; }
    setPublished((prev) => [...prev, p.id]);
    setNotice(`Published to ${p.label}.`);
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Export Hub"
        intro="Check a clip against each platform's rules, run the copyright scan, then publish — one click per platform."
        actions={<Badge tone="info">Pro · v0.8.0</Badge>}
      />
      <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-100" role="note">{GUARDRAIL}</div>
      {notice && <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>}
      {scanError && <ErrorBanner message={scanError} onRetry={onScan} />}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="glass">
          <CardHeader title="Clip" hint="The clip to export" />
          <div className="space-y-3">
            <label className="block text-sm">
              <span className="text-neutral-400">Clip ID</span>
              <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={clipId} onChange={(e) => setClipId(e.target.value)} placeholder="e.g. clip_01 — from the approval queue" aria-label="Clip ID" />
            </label>
            <label className="block text-sm">
              <span className="text-neutral-400">Clip file URL (preview)</span>
              <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={clipUrl} onChange={(e) => setClipUrl(e.target.value)} placeholder="https://…/clip_01.mp4" aria-label="Clip file URL" />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="text-neutral-400">Duration (seconds)</span>
                <input type="number" min={1} className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={clipDuration} onChange={(e) => setClipDuration(Number(e.target.value) || 0)} aria-label="Clip duration in seconds" />
              </label>
              <label className="block text-sm">
                <span className="text-neutral-400">Aspect ratio</span>
                <select className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={clipAspect} onChange={(e) => setClipAspect(e.target.value)} aria-label="Clip aspect ratio">
                  <option value="9:16">9:16 (vertical)</option>
                  <option value="1:1">1:1 (square)</option>
                  <option value="16:9">16:9 (landscape)</option>
                </select>
              </label>
            </div>
            <label className="block text-sm">
              <span className="text-neutral-400">Thumbnail image URL (optional)</span>
              <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={thumbnailUrl} onChange={(e) => setThumbnailUrl(e.target.value)} placeholder="https://…/thumb.jpg" aria-label="Thumbnail image URL" />
            </label>
          </div>
        </Card>

        <Card className="glass">
          <CardHeader title="Preview" hint="Clip next to its thumbnail" />
          <div className="grid grid-cols-2 gap-3">
            <div>
              <p className="mb-1 text-xs font-medium text-neutral-400">Clip</p>
              {clipUrl ? (
                <video src={clipUrl} controls className="aspect-[9/16] w-full rounded-lg bg-black object-cover" aria-label="Clip preview" />
              ) : (
                <div className="flex aspect-[9/16] items-center justify-center rounded-lg border border-dashed border-neutral-700 bg-neutral-950 p-3 text-center text-xs text-neutral-500">No clip preview — paste a clip file URL.</div>
              )}
            </div>
            <div>
              <p className="mb-1 text-xs font-medium text-neutral-400">Thumbnail</p>
              {thumbnailUrl ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={thumbnailUrl} alt="Thumbnail preview" className="aspect-[9/16] w-full rounded-lg bg-black object-cover" />
              ) : (
                <div className="flex aspect-[9/16] items-center justify-center rounded-lg border border-dashed border-neutral-700 bg-neutral-950 p-3 text-center text-xs text-neutral-500">No thumbnail yet — paste a thumbnail URL, or the platform will use the first frame.</div>
              )}
            </div>
          </div>
        </Card>
      </div>

      <Card className="glass">
        <CardHeader title="Caption & hashtags" hint="Used for every platform you publish to" />
        <div className="space-y-3">
          <label className="block text-sm">
            <span className="text-neutral-400">Caption</span>
            <textarea rows={2} className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm" value={caption} onChange={(e) => setCaption(e.target.value)} placeholder="The hook line for this clip…" aria-label="Caption" />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Hashtags</span>
            <input className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm" value={hashtags} onChange={(e) => setHashtags(e.target.value)} placeholder="#shorts #gaming" aria-label="Hashtags" />
          </label>
        </div>
      </Card>

      <Card className="glass">
        <CardHeader title="Platform rules" hint="Checked against this clip before publishing" />
        <ul className="space-y-3">
          {PLATFORMS.map((p) => {
            const tooLong = safeDuration > p.maxSeconds;
            const wrongAspect = p.aspect !== "any" && clipAspect !== p.aspect && p.id !== "x";
            const checked = selected.includes(p.id);
            return (
              <li key={p.id} className="flex flex-wrap items-center gap-3 rounded-lg border border-neutral-800 p-3">
                <label className="flex items-center gap-2 text-sm font-medium">
                  <input type="checkbox" className="h-4 w-4 accent-amber-400" checked={checked} onChange={() => togglePlatform(p.id)} aria-label={`Publish to ${p.label}`} />
                  {p.label}
                </label>
                <span className="text-xs text-neutral-400">Max {p.maxSeconds}s · {p.aspect}</span>
                {tooLong ? (
                  <span className="rounded-full bg-red-500/15 px-2 py-0.5 text-xs font-medium text-red-300">Too long — {fmtTime(safeDuration)} &gt; {p.maxSeconds}s. Auto-trim: the clip will be cut to the first {p.maxSeconds}s in Clip Studio before publishing.</span>
                ) : (
                  <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300">Fits ({fmtTime(safeDuration)} ≤ {p.maxSeconds}s)</span>
                )}
                {wrongAspect && <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-300">Aspect is {clipAspect} — {p.label} prefers {p.aspect}; it may be cropped.</span>}
                {published.includes(p.id) && <Badge tone="success">Published ✓</Badge>}
              </li>
            );
          })}
        </ul>
        {selected.length === 0 && <p className="mt-3 text-sm text-neutral-400">Tick at least one platform above.</p>}
      </Card>

      <Card className="glass">
        <CardHeader title="Copyright scan" hint="Required before publishing — a check, not a guarantee" />
        <p className="text-sm text-neutral-400">
          The scan checks the clip for known copyright and policy problems. A clean scan does <strong className="text-neutral-200">not</strong> guarantee
          a platform will accept the clip — platforms run their own checks (Content ID and reused-content review) after upload.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <PrimaryButton onClick={onScan} disabled={scanning || !clipId.trim()}>{scanning ? "Scanning…" : scanned ? "Scan again" : "Run copyright scan"}</PrimaryButton>
          {!scanned && <span className="self-center text-sm text-amber-200">Publishing stays blocked until the scan has run.</span>}
        </div>
        {scanned && flags !== null && (
          <div className="mt-4">
            {flags.length === 0 ? (
              <p className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-100">No flags — the scan found nothing to warn about. This is not a guarantee; the platform still runs its own checks.</p>
            ) : (
              <ul className="space-y-2">
                {flags.map((f, i) => (
                  <li key={i} className={`rounded-lg border p-3 text-sm ${f.severity.toLowerCase() === "severe" || f.severity.toLowerCase() === "high" || f.severity.toLowerCase() === "error" ? "border-red-500/40 bg-red-500/10 text-red-100" : f.severity.toLowerCase() === "warning" ? "border-amber-500/40 bg-amber-500/10 text-amber-100" : "border-neutral-700 bg-neutral-950 text-neutral-300"}`}>
                    <span className="font-medium">{f.type || "Flag"} · {f.severity}</span>
                    <span className="mt-0.5 block">{f.message}</span>
                  </li>
                ))}
              </ul>
            )}
            {blocked && (
              <p className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm font-medium text-red-100" role="alert">
                Publishing is blocked: the scan found a severe flag. Fix the clip (remove the flagged audio/footage) and scan again.
              </p>
            )}
          </div>
        )}
      </Card>

      <Card className="glass">
        <CardHeader title="Publish" hint="One click per platform — approval-first still applies on the queue" />
        {selected.length === 0 ? (
          <EmptyState title="No platform selected" body="Tick a platform in the rules list above, then come back here to publish." />
        ) : (
          <div className="flex flex-wrap gap-2">
            {PLATFORMS.filter((p) => selected.includes(p.id)).map((p) => {
              const tooLong = safeDuration > p.maxSeconds;
              const disabled = publishing !== null || !scanned || blocked || tooLong;
              return (
                <PrimaryButton
                  key={p.id}
                  onClick={() => onPublish(p)}
                  disabled={disabled}
                >
                  {publishing === p.id ? `Publishing to ${p.label}…` : `Publish to ${p.label}`}
                </PrimaryButton>
              );
            })}
          </div>
        )}
        <p className="mt-3 text-xs text-neutral-500">
          {!scanned ? "Run the copyright scan first." : blocked ? "Blocked by a severe scan flag." : "Clips longer than a platform's limit cannot publish — trim them in Clip Studio first."}
        </p>
        <div className="mt-3"><SecondaryButton href="/studio">Open Clip Studio to trim</SecondaryButton></div>
      </Card>
    </div>
  );
}
