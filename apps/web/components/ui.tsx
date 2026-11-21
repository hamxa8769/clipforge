"use client";
// Shared design-system primitives for the ClipForge web UI (v0.5.0).
//
// Design tokens (dark, high-fi — one system, every screen):
// - Surface: neutral-950 page, neutral-900 cards, neutral-800 borders/controls.
// - Text: neutral-100 headings, neutral-300 body, neutral-400 secondary, neutral-500 captions.
// - Accent: amber-400 (brand / primary actions), emerald (success/done),
//   sky (info/active), red (error/failed), violet (rendering).
// - Radius: rounded-xl cards, rounded-lg inner panels, rounded-full pills.
// - Type scale: page title text-2xl/700, section text-lg/600, body text-sm.
// - Every screen shows loading / empty / error states — never a blank page
//   and never demo numbers presented as live channel stats.
// - Navigation is always Next <Link>: a plain <a> forces a full-page load
//   (see PrimaryButton note below). External links keep <a target="_blank">.
import type { ReactNode } from "react";
import { useEffect } from "react";
import Link from "next/link";
import { openExternal } from "@/lib/tauri";

export function DemoBadge() {
  return <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — API unreachable</span>;
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-neutral-800 bg-neutral-900 p-5 ${className}`}>{children}</div>;
}

/** Card header row: title + optional right-aligned action. */
export function CardHeader({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <h2 className="text-lg font-semibold text-neutral-100">{title}</h2>
      {hint && <span className="text-xs text-neutral-500">{hint}</span>}
      {action && <span className="ml-auto">{action}</span>}
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = { ready: "bg-emerald-500/15 text-emerald-300", failed: "bg-red-500/15 text-red-300", blocked: "bg-red-500/15 text-red-300", scoring: "bg-amber-500/15 text-amber-300", rendering: "bg-violet-500/15 text-violet-300", review: "bg-sky-500/15 text-sky-300", transcribing: "bg-sky-500/15 text-sky-300", pending: "bg-neutral-700/50 text-neutral-300", approved: "bg-emerald-500/15 text-emerald-300", rejected: "bg-red-500/15 text-red-300", pending_approval: "bg-amber-500/15 text-amber-300", scheduled: "bg-sky-500/15 text-sky-300", published: "bg-emerald-500/15 text-emerald-300", dry_run: "bg-neutral-700/50 text-neutral-300", publishing: "bg-sky-500/15 text-sky-300", draft: "bg-neutral-700/50 text-neutral-300" };
  // Unknown statuses fall back to the "pending" style — never to "queued",
  // which is not a backend status (see lib/api.ts normalizeJobStatus).
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${map[status] ?? map.pending}`}>{status}</span>;
}

/** Small coloured dot identifying a platform everywhere it appears. */
export const PLATFORM_COLORS: Record<string, string> = {
  youtube: "bg-red-500",
  instagram: "bg-pink-500",
  facebook: "bg-blue-500",
  tiktok: "bg-cyan-400",
  x: "bg-neutral-100",
};

export function PlatformDot({ platform, className = "" }: { platform: string; className?: string }) {
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${PLATFORM_COLORS[platform.toLowerCase()] ?? "bg-neutral-500"} ${className}`} aria-hidden="true" />;
}

export const PLATFORM_LABELS: Record<string, string> = {
  youtube: "YouTube",
  instagram: "Instagram",
  facebook: "Facebook",
  tiktok: "TikTok",
  x: "X (Twitter)",
};

export function platformLabel(p: string): string {
  return PLATFORM_LABELS[p.toLowerCase()] ?? p;
}

/** Compact number: 412,000 -> "412k", 1,840 -> "1,840". */
export function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return n.toLocaleString();
}

export function fmtNum(n: number): string {
  return n.toLocaleString();
}

/** Thin progress bar (0–100). Colour: emerald when complete, amber while running, red on failure. */
export function ProgressBar({ value, tone = "default", className = "" }: { value: number; tone?: "default" | "success" | "danger"; className?: string }) {
  const color = tone === "success" ? "bg-emerald-500" : tone === "danger" ? "bg-red-500" : "bg-amber-400";
  return (
    <div className={`h-2 overflow-hidden rounded-full bg-neutral-800 ${className}`} role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}>
      <div className={`h-full ${color} transition-all`} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}

/** Loading skeleton block — shown while the first fetch is in flight. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-neutral-800/70 ${className}`} aria-label="Loading" />;
}

/** Empty state: icon-free, honest, with one clear next step. */
export function EmptyState({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-neutral-700 bg-neutral-900/60 p-6 text-center">
      <p className="font-medium text-neutral-200">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-neutral-400">{body}</p>
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

/** Error state with a retry action — used when a fetch fails after the API was reachable, or on first load. */
export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-500/40 bg-red-500/10 p-4" role="alert">
      <p className="flex-1 text-sm text-red-200">{message}</p>
      {onRetry && (
        <button onClick={onRetry} className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500">
          Retry
        </button>
      )}
    </div>
  );
}

/** Primary (amber) and secondary (neutral) action buttons — one primary per screen.
 *
 * With `href` this is a Next.js <Link>, not a plain <a>: in-app navigation
 * is client-side, like the header nav. A plain <a> forces a full-page
 * load, which used to land on raw API JSON — e.g. "Connect a channel"
 * navigated to /channels/connect and the server answered
 * {"detail": "unknown platform: connect"} because GET /channels/{platform}
 * claims that path before the static export (v0.4.1 server fix in
 * clipforge_api/__main__.py now serves the page for browser navigations
 * too, so a full load works as well — this is the second layer).
 * `onClick` is passed through: the old plain-<a> branch silently dropped
 * it, so the Onboarding wizard's dismiss-on-navigate never ran.
 */
export function PrimaryButton({ children, href, onClick, disabled }: { children: ReactNode; href?: string; onClick?: () => void; disabled?: boolean }) {
  const cls = "inline-flex items-center justify-center rounded-md bg-amber-400 px-4 py-2 text-sm font-semibold text-neutral-950 hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-50";
  if (href) return <Link href={href} onClick={onClick} className={cls}>{children}</Link>;
  return <button onClick={onClick} disabled={disabled} className={cls}>{children}</button>;
}

export function SecondaryButton({ children, href, onClick, disabled }: { children: ReactNode; href?: string; onClick?: () => void; disabled?: boolean }) {
  const cls = "inline-flex items-center justify-center rounded-md bg-neutral-800 px-3 py-1.5 text-sm font-medium text-neutral-200 hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50";
  if (href) return <Link href={href} onClick={onClick} className={cls}>{children}</Link>;
  return <button onClick={onClick} disabled={disabled} className={cls}>{children}</button>;
}

/** Danger button (red) — destructive actions only: disconnect, delete, reject. */
export function DangerButton({ children, href, onClick, disabled }: { children: ReactNode; href?: string; onClick?: () => void; disabled?: boolean }) {
  const cls = "inline-flex items-center justify-center rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50";
  if (href) return <Link href={href} onClick={onClick} className={cls}>{children}</Link>;
  return <button onClick={onClick} disabled={disabled} className={cls}>{children}</button>;
}

/** Small tone pill — success / warning / danger / info / neutral.
 *  Plain words only, never a raw status code or JSON value. */
export type BadgeTone = "success" | "warning" | "danger" | "info" | "neutral";
const BADGE_TONES: Record<BadgeTone, string> = {
  success: "bg-emerald-500/15 text-emerald-300",
  warning: "bg-amber-500/15 text-amber-300",
  danger: "bg-red-500/15 text-red-300",
  info: "bg-sky-500/15 text-sky-300",
  neutral: "bg-neutral-700/50 text-neutral-300",
};
export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: ReactNode }) {
  return <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${BADGE_TONES[tone]}`}>{children}</span>;
}

/** Two-letter initials from a display name / handle ("@clipforge-demo" -> "CL"). */
export function initialsFor(name: string): string {
  const clean = name.replace(/^@/, "").trim();
  if (!clean) return "?";
  const parts = clean.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/** Account avatar: the platform photo when the API returns one, else
 *  initials on the platform colour. Never a broken-image icon. */
export function Avatar({ name, src, platform, size = "md" }: { name: string; src?: string; platform?: string; size?: "sm" | "md" | "lg" }) {
  const dims = size === "lg" ? "h-12 w-12 text-base" : size === "sm" ? "h-7 w-7 text-[11px]" : "h-10 w-10 text-sm";
  const bg = PLATFORM_COLORS[(platform ?? "").toLowerCase()] ?? "bg-neutral-600";
  if (src) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img src={src} alt="" className={`${dims} shrink-0 rounded-full object-cover`} onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
    );
  }
  return (
    <span aria-hidden="true" className={`${dims} inline-flex shrink-0 items-center justify-center rounded-full ${bg} font-semibold text-white`}>
      {initialsFor(name)}
    </span>
  );
}

/** Page header — every screen opens the same way: title, one-line
 *  explanation of what this screen is for, actions on the right. */
export function PageHeader({ title, intro, actions }: { title: string; intro?: string; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-start gap-3">
      <div className="min-w-0 flex-1">
        <h1 className="text-2xl font-bold tracking-tight text-neutral-50">{title}</h1>
        {intro && <p className="mt-1 max-w-2xl text-sm text-neutral-400">{intro}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Big-number stat card (dashboard): label, value, supporting line. */
export function StatCard({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <Card>
      <p className="text-sm text-neutral-400">{label}</p>
      <p className="mt-1 text-3xl font-bold tracking-tight text-neutral-50">{value}</p>
      <p className="mt-1 text-xs leading-relaxed text-neutral-500">{sub}</p>
    </Card>
  );
}

/** Toast — one short message bottom-right, auto-dismisses. Plain
 *  language only; used for confirmations ("FFmpeg installed") and
 *  non-blocking errors ("Could not refresh — showing the last data"). */
export function Toast({ message, tone = "info", onClose }: { message: string; tone?: BadgeTone; onClose: () => void }) {
  useEffect(() => {
    const t = setTimeout(onClose, 5000);
    return () => clearTimeout(t);
  }, [onClose]);
  return (
    <div className={`fixed bottom-12 right-4 z-50 flex max-w-sm items-start gap-3 rounded-lg border px-4 py-3 shadow-lg ${tone === "success" ? "border-emerald-500/40 bg-emerald-950" : tone === "danger" ? "border-red-500/40 bg-red-950" : "border-neutral-700 bg-neutral-900"}`} role="status">
      <p className="flex-1 text-sm text-neutral-100">{message}</p>
      <button onClick={onClose} aria-label="Dismiss" className="text-neutral-400 hover:text-neutral-100">✕</button>
    </div>
  );
}

/**
 * Anchor to an EXTERNAL (http/https) URL — the only external anchor in
 * the UI. A plain `<a target="_blank">` does not open in the desktop
 * app (the Tauri WebView swallows it; v0.4.1 had no capability granting
 * the opener plugin), so the click is intercepted and routed through
 * `openExternal()`, which asks the desktop shell to open the system
 * browser. The `href` stays on the element so the URL is visible on
 * hover and copyable (right-click → Copy link address), and in a plain
 * browser `openExternal` falls back to `window.open`.
 *
 * Internal navigation must keep using Next <Link> / PrimaryButton href —
 * never this component.
 */
export function ExternalLink({ href, className, children }: { href: string; className?: string; children: ReactNode }) {
  return (
    <a
      href={href}
      title={href}
      target="_blank"
      rel="noreferrer"
      className={className}
      onClick={(e) => {
        e.preventDefault();
        void openExternal(href);
      }}
    >
      {children}
    </a>
  );
}

/**
 * Accessible on/off switch. `disabled` renders it locked (used for the
 * dry-run guard, which is server-enforced and cannot be turned off here).
 */
export function Toggle({ checked, onChange, disabled, label, hint }: { checked: boolean; onChange?: (v: boolean) => void; disabled?: boolean; label: string; hint?: string }) {
  return (
    <div className="flex items-start gap-3">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange?.(!checked)}
        className={`mt-0.5 flex h-6 w-11 shrink-0 items-center rounded-full p-0.5 transition-colors ${checked ? "bg-amber-400" : "bg-neutral-700"} ${disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}
      >
        <span className={`h-5 w-5 rounded-full bg-white shadow transition-transform ${checked ? "translate-x-5" : "translate-x-0"}`} />
      </button>
      <span>
        <span className="block text-sm font-medium text-neutral-100">{label}</span>
        {hint && <span className="mt-0.5 block text-xs text-neutral-400">{hint}</span>}
      </span>
    </div>
  );
}
