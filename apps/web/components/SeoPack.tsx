// SEO pack per clip (v0.11.0, Worker B) — ClipNova-style, simple.
//
// One pack per clip, one tab per platform: title options to pick from,
// description, plain search tags, hashtags, and one plain-language tip.
// Every field has its own Copy button, plus a Copy-all for the whole
// pack. Picking a title option only changes what gets copied — it never
// edits the clip or posts anything.
"use client";
import { useState } from "react";
import type { SeoPack } from "@/lib/api";

const PLATFORMS = ["youtube", "tiktok", "instagram", "facebook", "x"] as const;

async function copyText(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard API unavailable (older WebView / no permission): fall back
    // to the hidden-textarea trick rather than telling the user it failed.
    try {
      const el = document.createElement("textarea");
      el.value = text;
      el.style.position = "fixed";
      el.style.opacity = "0";
      document.body.appendChild(el);
      el.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(el);
      return ok;
    } catch {
      return false;
    }
  }
}

export function CopyButton({ text, label, small }: { text: string; label: string; small?: boolean }) {
  const [copied, setCopied] = useState(false);
  async function onCopy() {
    const ok = await copyText(text);
    if (!ok) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }
  return (
    <button
      type="button"
      onClick={() => { void onCopy(); }}
      className={`rounded-md bg-neutral-800 px-2.5 font-medium text-neutral-100 hover:bg-neutral-700 ${small ? "py-1 text-xs" : "py-1.5 text-sm"}`}
    >
      {copied ? "Copied ✓" : label}
    </button>
  );
}

export default function SeoPack({ pack }: { pack: SeoPack }) {
  const [platform, setPlatform] = useState<string>("youtube");
  // The title option the user picked per platform (default: option 1).
  const [pickedTitle, setPickedTitle] = useState<Record<string, string>>({});
  const [copiedAll, setCopiedAll] = useState(false);
  const variant = pack.platforms[platform];
  if (!variant) return null;
  const title = pickedTitle[platform] ?? variant.title;

  async function onCopyAll() {
    const ok = await copyText(pack.copy_all);
    if (!ok) return;
    setCopiedAll(true);
    window.setTimeout(() => setCopiedAll(false), 2000);
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-neutral-100">SEO pack — {pack.keyword}</h3>
        <span className="text-xs text-neutral-500">Titles, description, tags &amp; hashtags, ready to paste.</span>
        <button
          type="button"
          onClick={() => { void onCopyAll(); }}
          className="ml-auto rounded-md bg-amber-400 px-3 py-1.5 text-sm font-medium text-neutral-950 hover:bg-amber-300"
        >
          {copiedAll ? "Copied ✓" : "Copy all"}
        </button>
      </div>

      <div className="mt-3 flex gap-1 border-b border-neutral-800">
        {PLATFORMS.map((p) => {
          const v = pack.platforms[p];
          if (!v) return null;
          return (
            <button
              key={p}
              type="button"
              onClick={() => setPlatform(p)}
              className={`rounded-t-md px-3 py-2 text-sm ${platform === p ? "bg-neutral-800 font-medium text-white" : "text-neutral-400 hover:text-white"}`}
            >
              {v.label}
            </button>
          );
        })}
      </div>

      <div className="space-y-4 rounded-b-lg bg-neutral-950 p-4 text-sm">
        <div>
          <div className="flex items-center justify-between gap-2">
            <p className="font-medium text-neutral-200">Title — pick one</p>
            <CopyButton text={title} label="Copy title" small />
          </div>
          <ul className="mt-2 space-y-1.5">
            {variant.title_options.map((option) => {
              const selected = option === title;
              return (
                <li key={option}>
                  <button
                    type="button"
                    onClick={() => setPickedTitle((prev) => ({ ...prev, [platform]: option }))}
                    aria-pressed={selected}
                    className={`w-full rounded-lg border px-3 py-2 text-left ${selected ? "border-amber-400/70 bg-amber-400/10 text-neutral-100" : "border-neutral-800 bg-neutral-900 text-neutral-300 hover:border-neutral-600"}`}
                  >
                    {option} <span className="text-neutral-500">({option.length} chars)</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>

        <div>
          <div className="flex items-center justify-between gap-2">
            <p className="font-medium text-neutral-200">Description</p>
            <CopyButton text={variant.description} label="Copy description" small />
          </div>
          <p className="mt-1 whitespace-pre-wrap text-neutral-300">{variant.description}</p>
        </div>

        <div>
          <div className="flex items-center justify-between gap-2">
            <p className="font-medium text-neutral-200">Search tags <span className="font-normal text-neutral-500">(plain words, no # — paste into the tags box on YouTube)</span></p>
            <CopyButton text={variant.tags.join(", ")} label="Copy tags" small />
          </div>
          <p className="mt-1 text-neutral-300">{variant.tags.join(", ")}</p>
        </div>

        <div>
          <div className="flex items-center justify-between gap-2">
            <p className="font-medium text-neutral-200">Hashtags</p>
            <CopyButton text={variant.hashtags.map((t) => `#${t}`).join(" ")} label="Copy hashtags" small />
          </div>
          <p className="mt-1 text-neutral-300">{variant.hashtags.map((t) => `#${t}`).join(" ")}</p>
        </div>

        <p className="text-xs text-neutral-500">{variant.tip}</p>
        {variant.warnings.length > 0 && (
          <ul className="space-y-1 text-xs text-amber-200">
            {variant.warnings.map((w) => <li key={w}>⚠ {w}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}
