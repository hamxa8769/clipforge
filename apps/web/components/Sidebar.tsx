// App sidebar (v0.5.0) — the desktop-app shell: brand + version at the
// top, one nav item per screen, approval-first note at the bottom.
//
// All navigation is Next <Link> (client-side, no full-page reload — a
// plain <a> once landed users on raw API JSON). The active item is the
// current route (usePathname); sub-pages (e.g. /channels/connect) keep
// their parent item highlighted.
"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV: { href: string; label: string; hint: string; icon: React.ReactNode }[] = [
  { href: "/", label: "Dashboard", hint: "Accounts, jobs & health", icon: <path d="M3 12l9-8 9 8M5 10v10h5v-6h4v6h5V10" /> },
  // v0.5.0 (worker 3): Pipeline + Agent pages.
  { href: "/pipeline", label: "Pipeline", hint: "Every step, live, per job", icon: <path d="M4 6h16M4 12h16M4 18h16" /> },
  { href: "/agent", label: "Agent", hint: "Auto-clipping, approval-first", icon: <path d="M12 2a7 7 0 0 1 7 7c0 2.4-1.2 4.2-2.6 5.5V18a2 2 0 0 1-2 2h-4.8a2 2 0 0 1-2-2v-3.5C6.2 13.2 5 11.4 5 9a7 7 0 0 1 7-7zM9 22h6" /> },
  // v0.8.0 (worker B): Clip Studio, Export Hub, Batch Mode.
  { href: "/studio", label: "Studio", hint: "AI clips + timeline editor", icon: <path d="M4 5h16v14H4zM4 9h16M8 5v14" /> },
  { href: "/export", label: "Export", hint: "Publish with platform rules", icon: <path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" /> },
  { href: "/batch", label: "Batch", hint: "Clip up to 20 videos at once", icon: <path d="M4 7h16M4 12h16M4 17h10" /> },
  // v0.6.0 (worker B): Trending page — live niche search + agent loop.
  { href: "/trending", label: "Trending", hint: "What's hot in your niche", icon: <path d="M3 17l6-6 4 4 8-8M15 7h6v6" /> },
  // v0.10.0 (Worker A): Live monitoring — watch a stream, clip its peaks.
  { href: "/live", label: "Live", hint: "Monitor streams, clip peaks", icon: <><circle cx="12" cy="12" r="2" /><path d="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8" /></> },
  { href: "/automation", label: "Automation", hint: "Runner, rules & history", icon: <path d="M4 12a8 8 0 1 1 2.3 5.6M4 12V7m0 5h5" /> },
  { href: "/channels", label: "Channels", hint: "Connect accounts", icon: <path d="M12 3v10m0 0l-3-3m3 3l3-3M5 21h14" /> },
  // v0.11.0 (worker B): Channel SEO — what works on a channel.
  { href: "/channel-seo", label: "Channel SEO", hint: "Top videos & title ideas", icon: <path d="M3 17l6-6 4 4 8-8M15 7h6v6" /> },
  { href: "/queue", label: "Approval Queue", hint: "Review clips before posting", icon: <path d="M9 11l3 3 8-8M5 21h14M5 3h14" /> },
  { href: "/schedule", label: "Schedule", hint: "When clips go out", icon: <path d="M8 3v4M16 3v4M3 9h18M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z" /> },
  { href: "/monetization", label: "Monetization", hint: "Thresholds per platform", icon: <path d="M3 17l6-6 4 4 8-8M15 7h6v6" /> },
  // v0.6.0 (worker C): Growth page — posted-clip stats + advice.
  { href: "/growth", label: "Growth", hint: "Posted clips & what to post next", icon: <path d="M3 17l6-6 4 4 8-8M15 7h6v6" /> },
  { href: "/settings", label: "Settings", hint: "Keys, FFmpeg & app", icon: <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3 1a7 7 0 0 0-2-1.2L14.2 3h-4l-.4 2.7a7 7 0 0 0-2 1.2l-2.3-1-2 3.4 2 1.5A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 2 1.2l.4 2.7h4l.4-2.7a7 7 0 0 0 2-1.2l2.3 1 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z" /> },
];

function NavIcon({ children }: { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5 shrink-0" aria-hidden="true">
      {children}
    </svg>
  );
}

export default function Sidebar({ version }: { version: string }) {
  const pathname = usePathname() ?? "/";
  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-neutral-800 bg-neutral-900">
      <div className="flex items-center gap-2.5 px-5 pb-4 pt-5">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-amber-400 text-lg font-bold text-neutral-950" aria-hidden="true">C</span>
        <span>
          <span className="block text-base font-bold leading-tight tracking-tight text-neutral-50">ClipForge</span>
          <span className="block text-xs leading-tight text-neutral-500">v{version} · desktop</span>
        </span>
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-2" aria-label="Main">
        {NAV.map((n) => {
          const active = n.href === "/" ? pathname === "/" : pathname === n.href || pathname.startsWith(`${n.href}/`);
          return (
            <Link
              key={n.href}
              href={n.href}
              aria-current={active ? "page" : undefined}
              className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors ${
                active ? "bg-neutral-800 font-semibold text-neutral-50" : "text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-100"
              }`}
            >
              <NavIcon>{n.icon}</NavIcon>
              <span className="min-w-0">
                <span className="block leading-tight">{n.label}</span>
                <span className={`block truncate text-xs leading-tight ${active ? "text-neutral-400" : "text-neutral-500"}`}>{n.hint}</span>
              </span>
              {active && <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-amber-400" aria-hidden="true" />}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-neutral-800 px-5 py-4">
        <p className="text-xs leading-relaxed text-neutral-500">
          Approval-first publishing. Nothing posts without your approval.
        </p>
      </div>
    </aside>
  );
}
