// Notifications bell (v0.9.0) — the header bell with unread count and
// a dropdown panel, wired to the notifications UI aliases (Worker C):
//   GET  /notifications            -> NotificationItem[] (newest first)
//   POST /notifications/read {ids} -> {read: N}  (omit ids = all)
//   POST /notifications/clear      -> {cleared: N}
// An item's `link` is an in-app route (e.g. /queue, /jobs/<id>) or
// null. The panel opens from the #notifications deep link too (used by
// the dashboard "what needs attention" rows and by screenshots). When
// the API is unreachable the bell says so — it never invents
// notifications.
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  clearNotifications, getNotifications, markNotificationsRead,
  type NotificationItem, type NotificationTone,
} from "@/lib/api";
import { IconButton, LoadingState } from "./ui";

const TONE_DOT: Record<NotificationTone, string> = {
  success: "bg-emerald-400",
  warning: "bg-amber-400",
  danger: "bg-red-400",
  info: "bg-sky-400",
};

function fmtTime(iso: string | null): string {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

export default function NotificationsBell() {
  const [items, setItems] = useState<NotificationItem[] | null | undefined>(undefined);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    setItems(await getNotifications());
  }, []);

  useEffect(() => {
    load().catch(() => setItems(null));
    const t = setInterval(() => { load().catch(() => {}); }, 60000);
    return () => clearInterval(t);
  }, [load]);

  // Deep link: #notifications opens the panel on load.
  useEffect(() => {
    if (typeof window !== "undefined" && window.location.hash === "#notifications") setOpen(true);
  }, []);

  // Close on Escape / click outside.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const onClick = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => { document.removeEventListener("keydown", onKey); document.removeEventListener("mousedown", onClick); };
  }, [open ]);

  const unread = items?.filter((n) => !n.read).length ?? 0;

  async function markAllRead() {
    if (!items || unread === 0) return;
    setBusy(true);
    setError(null);
    const res = await markNotificationsRead();
    setBusy(false);
    if (!res.ok) { setError(res.error ?? "Could not mark notifications as read."); return; }
    await load();
  }

  async function clearAll() {
    if (!items || items.length === 0) return;
    setBusy(true);
    setError(null);
    const res = await clearNotifications();
    setBusy(false);
    if (!res.ok) { setError(res.error ?? "Could not clear notifications."); return; }
    await load();
  }

  async function openItem(n: NotificationItem) {
    setOpen(false);
    if (n.read) return;
    await markNotificationsRead([n.id]);
    await load();
  }

  return (
    <div className="relative" ref={panelRef}>
      <IconButton
        label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
        title={items === null ? "Notifications need the server — connect it to see alerts" : "Notifications"}
        active={open}
        onClick={() => setOpen((o) => !o)}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5" aria-hidden="true">
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.7 21a2 2 0 0 1-3.4 0" />
        </svg>
        {unread > 0 && (
          <span className="absolute -right-1.5 -top-1.5 flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-400 px-1 text-[11px] font-bold text-neutral-950" aria-hidden="true">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </IconButton>

      {open && (
        <div className="pop-in absolute right-0 top-11 z-50 w-[380px] max-w-[90vw] overflow-hidden rounded-xl border border-neutral-700 bg-neutral-900 shadow-[var(--shadow-pop)]" role="dialog" aria-label="Notifications">
          <div className="flex items-center gap-2 border-b border-neutral-800 px-4 py-3">
            <h2 className="text-sm font-semibold text-neutral-100">Notifications</h2>
            {unread > 0 && <span className="rounded-full bg-amber-400/15 px-2 py-0.5 text-xs font-medium text-amber-300">{unread} unread</span>}
            <span className="ml-auto flex gap-1">
              <button
                onClick={() => void markAllRead()}
                disabled={busy || unread === 0 || items == null}
                className="rounded-md px-2 py-1 text-xs font-medium text-amber-300 hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy ? "Working…" : "Mark all read"}
              </button>
              <button
                onClick={() => void clearAll()}
                disabled={busy || !items || items.length === 0}
                className="rounded-md px-2 py-1 text-xs font-medium text-neutral-400 hover:bg-neutral-800 hover:text-neutral-200 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Clear
              </button>
            </span>
          </div>
          <div className="max-h-[420px] overflow-y-auto p-2">
            {items === undefined ? (
              <div className="p-2"><LoadingState lines={2} label="Loading notifications…" /></div>
            ) : items === null ? (
              <div className="p-3 text-center">
                <p className="text-sm font-medium text-neutral-200">Notifications aren&apos;t available right now</p>
                <p className="mx-auto mt-1 max-w-xs text-xs leading-relaxed text-neutral-400">
                  The server is unreachable, so there is nothing to show here yet — no alerts are being hidden from you.
                </p>
              </div>
            ) : items.length === 0 ? (
              <p className="p-3 text-center text-sm text-neutral-400">You&apos;re all caught up — no notifications.</p>
            ) : (
              <ul className="space-y-1">
                {items.map((n) => (
                  <li key={n.id}>
                    {n.link ? (
                      <Link href={n.link} onClick={() => void openItem(n)} className="flex items-start gap-3 rounded-lg px-3 py-2.5 hover:bg-neutral-800">
                        <NotificationRow n={n} />
                      </Link>
                    ) : (
                      <button onClick={() => void openItem(n)} className="flex w-full items-start gap-3 rounded-lg px-3 py-2.5 text-left hover:bg-neutral-800">
                        <NotificationRow n={n} />
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {error && <p className="rounded-md bg-red-500/10 px-3 py-2 text-xs text-red-200" role="alert">{error}</p>}
          </div>
        </div>
      )}
    </div>
  );
}

function NotificationRow({ n }: { n: NotificationItem }) {
  return (
    <>
      <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${TONE_DOT[n.tone]}`} aria-hidden="true" />
      <span className="min-w-0">
        <span className={`block text-sm leading-snug ${n.read ? "text-neutral-400" : "font-medium text-neutral-100"}`}>{n.title}</span>
        {n.body && <span className="mt-0.5 block text-xs leading-snug text-neutral-500">{n.body}</span>}
        {n.created_at && <span className="mt-0.5 block text-[11px] text-neutral-600" suppressHydrationWarning>{fmtTime(n.created_at)}</span>}
      </span>
      {!n.read && <span className="ml-auto mt-1.5 h-2 w-2 shrink-0 rounded-full bg-amber-400" aria-label="Unread" />}
    </>
  );
}
