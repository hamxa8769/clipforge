"use client";
import { useEffect, useState } from "react";
import { API_URL } from "@/lib/api";
import type { Post } from "@/lib/api";
import { Card, StatusPill } from "@/components/ui";

export default function QueueClient({ posts, demo }: { posts: Post[]; demo: boolean }) {
  const [items, setItems] = useState(posts);
  // `posts` arrives twice: demo seed data first, then the live list once
  // the parent's fetch lands. useState only reads the initial prop, so
  // without this the queue showed the demo posts forever (and Approve /
  // Publish posted to demo ids that do not exist) even with the API live.
  useEffect(() => {
    setItems(posts);
  }, [posts]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  async function act(id: string, action: "approve" | "publish") {
    // Never update optimistically: only a real 2xx from the server moves
    // the post. (The old code set the status even when the request
    // failed or the API was unreachable, so demo posts looked approved.)
    if (demo) {
      setMsg("Demo data — the server is not answering, so nothing can be approved or published from here.");
      return;
    }
    setBusy(`${action}:${id}`);
    setMsg("");
    let ok = false;
    let error = "";
    try {
      const res = await fetch(`${API_URL}/posts/${id}/${action}`, { method: "POST" });
      ok = res.ok;
      if (!ok) {
        try {
          const data = (await res.json()) as { detail?: unknown };
          error = typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`;
        } catch {
          error = `Request failed (${res.status})`;
        }
      }
    } catch {
      error = "The API did not answer.";
    }
    setBusy(null);
    if (!ok) {
      setMsg(`Could not ${action} this post: ${error}`);
      return;
    }
    // Optimistic update uses real PostStatus values: a dry-run publish lands
    // the post in "dry_run", an approval in "approved" (docs/api.md).
    setItems((xs) => xs.map((p) => (p.id === id ? { ...p, status: action === "approve" ? "approved" : "dry_run" } : p)));
    setMsg(action === "publish" ? "Dry-run publish: no post was sent to any platform." : "Approved — ready to schedule.");
  }
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3"><h1 className="text-2xl font-bold">Approval queue</h1>{demo && <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — server not answering</span>}</div>
      <p className="text-sm text-neutral-400">Nothing publishes without approval. TikTok posts go to the creator inbox as a draft until the Content Posting API audit is passed.</p>
      {msg && <p className="rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">{msg}</p>}
      {items.length === 0 ? <Card><p className="text-neutral-400">Queue is empty — approve clips on a job review page first.</p></Card> : (
        <ul className="space-y-4">{items.map((p) => (
          <li key={p.id}><Card>
            <div className="flex flex-wrap items-center gap-3">
              <span className="rounded-full bg-neutral-800 px-2 py-0.5 text-xs font-medium">{p.platform}</span>
              <span className="text-sm text-neutral-400">{p.scheduled_at ? `Scheduled ${new Date(p.scheduled_at).toLocaleString()}` : "Not scheduled yet"}</span>
              <span className="ml-auto"><StatusPill status={p.status} /></span>
            </div>
            <p className="mt-3 text-sm">{p.caption}</p>
            <div className="mt-4 flex gap-2">
              <button onClick={() => act(p.id, "approve")} disabled={demo || busy !== null} className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-60" title={demo ? "Demo data — the server is not answering" : undefined}>{busy === `approve:${p.id}` ? "Working…" : "Approve"}</button>
              <button onClick={() => act(p.id, "publish")} disabled={demo || busy !== null} className="rounded-md bg-neutral-800 px-4 py-2 text-sm font-medium hover:bg-neutral-700 disabled:opacity-60" title={demo ? "Demo data — the server is not answering" : undefined}>{busy === `publish:${p.id}` ? "Working…" : "Publish (dry-run)"}</button>
            </div>
            {demo && <p className="mt-2 text-xs text-neutral-500">Demo post — approve / publish are disabled until the API answers.</p>}
          </Card></li>
        ))}</ul>
      )}
    </div>
  );
}
