import "./globals.css";
import DryRunBanner from "@/components/DryRunBanner";
import { FfmpegMissingBanner } from "@/components/FfmpegInstaller";
import Sidebar from "@/components/Sidebar";
import StatusBar from "@/components/StatusBar";
import { UpdateBanner } from "@/components/Updater";
import Onboarding from "@/components/Onboarding";

// Desktop-app shell (v0.5.0): fixed sidebar on the left, content in the
// middle, a status bar pinned to the bottom — like a real desktop app,
// not a web page in a window. Plain server component (no data fetching
// here): the banners and status bar are client components that check
// /health in the browser, so this layout can be statically exported.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="flex h-screen overflow-hidden bg-neutral-950">
          <Sidebar version="0.8.1" />
          <div className="flex min-w-0 flex-1 flex-col">
            <UpdateBanner />
            <DryRunBanner />
            <main className="min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto max-w-7xl px-6 py-6">{children}</div>
            </main>
            <FfmpegMissingBanner />
            <StatusBar />
          </div>
        </div>
        <Onboarding />
      </body>
    </html>
  );
}
