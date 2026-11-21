// Setup page (v0.5.0) — the first-run wizard as a full page, replayable
// from Settings. Same five steps as the first-run modal:
// FFmpeg -> Gemini key -> channel -> niche & source -> turn on the agent.
// Progress is stored on the server, so leaving and coming back resumes
// where you left off.
"use client";
import Link from "next/link";
import { SetupWizard } from "@/components/Onboarding";

export default function SetupPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Set up ClipForge</h1>
        <p className="mt-1 text-sm text-neutral-400">
          The full setup in five steps. Skip anything — you can finish it later from here or from Settings.
        </p>
        <p className="mt-2 text-sm">
          <Link href="/" className="text-sky-400 underline">← Back to Dashboard</Link>
        </p>
      </div>
      <SetupWizard inline />
    </div>
  );
}
