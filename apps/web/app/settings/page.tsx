import { Card } from "@/components/ui";

const ACCOUNTS = [
  { provider: "YouTube", label: "@clipforge-demo", status: "Connected (demo)" },
  { provider: "Instagram", label: "@clipforge.demo", status: "Connected (demo)" },
  { provider: "Facebook", label: "ClipForge Page", status: "Not connected" },
  { provider: "TikTok", label: "@clipforge.demo", status: "Not connected — audit pending" },
];

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Settings</h1>
      <Card>
        <h2 className="mb-4 text-lg font-semibold">Connected accounts</h2>
        <ul className="divide-y divide-neutral-800">
          {ACCOUNTS.map((a) => (
            <li key={a.provider} className="flex flex-wrap items-center gap-3 py-3">
              <span className="font-medium">{a.provider}</span>
              <span className="text-sm text-neutral-400">{a.label}</span>
              <span className="ml-auto text-sm text-neutral-400">{a.status}</span>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-sm text-neutral-400">Demo account list — live account status comes from the API once connected accounts are configured on the server.</p>
      </Card>
      <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-5">
        <h2 className="font-semibold text-amber-200">API keys — server only</h2>
        <p className="mt-2 text-sm text-amber-100/80">API keys and OAuth secrets are entered in <code>.env</code> on the server. Never paste keys, tokens or passwords into this browser UI — there is no field for them here by design. Tokens are stored encrypted on the server; this UI never asks for passwords or cookies.</p>
      </div>
      <Card>
        <h2 className="mb-2 text-lg font-semibold">Publishing safety</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-neutral-400">
          <li>Approval-first: nothing publishes without your approval on the queue page.</li>
          <li>Dry-run mode is shown as a banner on every page while the API reports dry-run.</li>
          <li>TikTok: unaudited apps can only post private drafts or inbox drafts — public Direct Post needs the audit.</li>
        </ul>
      </Card>
    </div>
  );
}
