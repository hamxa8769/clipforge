import "./globals.css";
import DryRunBanner from "@/components/DryRunBanner";
import { FfmpegMissingBanner } from "@/components/FfmpegInstaller";
import { ServerStatusProvider } from "@/components/ServerStatusProvider";
import Sidebar from "@/components/Sidebar";
import StatusBar from "@/components/StatusBar";
import TopBar from "@/components/TopBar";
import { UpdateBanner } from "@/components/Updater";
import Onboarding from "@/components/Onboarding";

// Desktop-app shell (v0.5.0, top bar added v0.9.0): fixed sidebar on the
// left, a slim top strip (live state, performance profile, notifications
// bell), content in the middle, a status bar pinned to the bottom — like
// a real desktop app, not a web page in a window. Plain server component
// (no data fetching here): the banners, top bar and status bar are
// client components that check /health in the browser, so this layout
// can be statically exported.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="flex h-screen overflow-hidden bg-neutral-950">
          <Sidebar version="0.10.1" />
          <div className="flex min-w-0 flex-1 flex-col">
            {/* One shared server status for the whole app (v0.9.3):
                ServerStatusProvider owns the only /health poll, and the
                top bar, status bar, banners and every page read it from
                useServerStatus() — no component probes /health itself,
                so the UI can no longer say "Live" in one place and
                "unreachable" in another. */}
            <ServerStatusProvider>
              <UpdateBanner />
              <DryRunBanner />
              <TopBar />
              <main className="min-h-0 flex-1 overflow-y-auto">
                <div className="mx-auto max-w-7xl px-6 py-6">{children}</div>
              </main>
              <FfmpegMissingBanner />
              <StatusBar />
            </ServerStatusProvider>
          </div>
        </div>
        <Onboarding />
      </body>
    </html>
  );
}
