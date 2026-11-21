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
  async function act(id: string, action: "approve" | "publish") {
    try { await fetch(`${API_URL}/posts/${id}/${action}`, { method: "POST" }); } catch { /* demo */ }
    // Optimistic update uses real PostStatus values: a dry-run publish lands
    // the post in "dry_run", an approval in "approved" (docs/api.md).
    setItems((xs) => xs.map((p) => (p.id === id ? { ...p, status: action === "approve" ? "approved" : "dry_run" } : p)));
    setMsg(action === "publish" ? "Dry-run publish: no post was sent to any platform." : "Approved — ready to schedule.");
  }
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3"><h1 className="text-2xl font-bold">Approval queue</h1>{demo && <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">Demo data — API unreachable</span>}</div>
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
              <button onClick={() => act(p.id, "approve")} className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500">Approve</button>
              <button onClick={() => act(p.id, "publish")} className="rounded-md bg-neutral-800 px-4 py-2 text-sm font-medium hover:bg-neutral-700">Publish (dry-run)</button>
            </div>
          </Card></li>
        ))}</ul>
      )}
    </div>
  );
}
