export function DemoBadge() {
  return <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — API unreachable</span>;
}
export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-neutral-800 bg-neutral-900 p-5 ${className}`}>{children}</div>;
}
export function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = { ready: "bg-emerald-500/15 text-emerald-300", failed: "bg-red-500/15 text-red-300", scoring: "bg-amber-500/15 text-amber-300", rendering: "bg-violet-500/15 text-violet-300", review: "bg-sky-500/15 text-sky-300", transcribing: "bg-sky-500/15 text-sky-300", pending: "bg-neutral-700/50 text-neutral-300", approved: "bg-emerald-500/15 text-emerald-300", rejected: "bg-red-500/15 text-red-300", pending_approval: "bg-amber-500/15 text-amber-300", scheduled: "bg-sky-500/15 text-sky-300", published: "bg-emerald-500/15 text-emerald-300", dry_run: "bg-neutral-700/50 text-neutral-300" };
  // Unknown statuses fall back to the "pending" style — never to "queued",
  // which is not a backend status (see lib/api.ts normalizeJobStatus).
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${map[status] ?? map.pending}`}>{status}</span>;
}
