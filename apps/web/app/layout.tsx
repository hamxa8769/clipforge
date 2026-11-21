import "./globals.css";
import Link from "next/link";
import DryRunBanner from "@/components/DryRunBanner";
import { FfmpegMissingBanner } from "@/components/FfmpegInstaller";
import StatusBar from "@/components/StatusBar";
import { UpdateBanner } from "@/components/Updater";
import Onboarding from "@/components/Onboarding";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/automation", label: "Automation" },
  { href: "/channels", label: "Channels" },
  { href: "/queue", label: "Approval Queue" },
  { href: "/schedule", label: "Schedule" },
  { href: "/monetization", label: "Monetization" },
  { href: "/settings", label: "Settings" },
];

// Plain server component (no data fetching here): the dry-run banner is a
// client component that checks /health in the browser, so this layout can
// be statically exported. `next dev` / standalone behaviour is unchanged.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <UpdateBanner />
        <header className="border-b border-neutral-800 bg-neutral-900">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-4">
            <Link href="/" className="text-xl font-bold tracking-tight">ClipForge</Link>
            <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400">v0.4.2</span>
            <nav className="ml-auto flex flex-wrap gap-1">
              {NAV.map((n) => (
                <Link key={n.href} href={n.href} className="rounded-md px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-800 hover:text-white">{n.label}</Link>
              ))}
            </nav>
          </div>
          <DryRunBanner />
        </header>
        <StatusBar />
        <FfmpegMissingBanner />
        <main className="mx-auto max-w-6xl px-4 py-8">{children}</main>
        <footer className="border-t border-neutral-800 py-6 text-center text-xs text-neutral-500">
          ClipForge v0.4.2 — approval-first publishing. Nothing posts without your approval.
        </footer>
        <Onboarding />
      </body>
    </html>
  );
}
