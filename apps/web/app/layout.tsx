import Link from "next/link";
import { getHealth } from "@/lib/api";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/queue", label: "Approval Queue" },
  { href: "/schedule", label: "Schedule" },
  { href: "/monetization", label: "Monetization" },
  { href: "/settings", label: "Settings" },
];

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const health = await getHealth();
  const dryRun = health?.dry_run ?? true; // Unknown API state: show dry-run banner (safe default).
  return (
    <html lang="en">
      <body>
        <header className="border-b border-neutral-800 bg-neutral-900">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-4">
            <Link href="/" className="text-xl font-bold tracking-tight">ClipForge</Link>
            <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400">v0.1.0</span>
            <nav className="ml-auto flex flex-wrap gap-1">
              {NAV.map((n) => (
                <Link key={n.href} href={n.href} className="rounded-md px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-800 hover:text-white">{n.label}</Link>
              ))}
            </nav>
          </div>
          {dryRun && (
            <div className="bg-amber-500/15 px-4 py-2 text-center text-sm font-medium text-amber-300" role="status">
              Dry-run mode — nothing will be published to any platform. {health ? "API reports dry-run." : "API unreachable; showing demo data."}
            </div>
          )}
        </header>
        <main className="mx-auto max-w-6xl px-4 py-8">{children}</main>
        <footer className="border-t border-neutral-800 py-6 text-center text-xs text-neutral-500">
          ClipForge v0.1.0 — approval-first publishing. Nothing posts without your approval.
        </footer>
      </body>
    </html>
  );
}
