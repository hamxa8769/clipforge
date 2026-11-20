export function DemoBadge() {
  return <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — API unreachable</span>;
}
export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-neutral-800 bg-neutral-900 p-5 ${className}`}>{children}</div>;
}
export function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = { ready: "bg-emerald-500/15 text-emerald-300", failed: "bg-red-500/15 text-red-300", scoring: "bg-amber-500/15 text-amber-300", transcribing: "bg-sky-500/15 text-sky-300", queued: "bg-neutral-700/50 text-neutral-300", pending: "bg-amber-500/15 text-amber-300", approved: "bg-emerald-500/15 text-emerald-300", rejected: "bg-red-500/15 text-red-300" };
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${map[status] ?? map.queued}`}>{status}</span>;
}
