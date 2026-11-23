// Client component: posts + liveness are fetched in the browser so this
// page can be statically exported. Same UI and demo fallback as before;
// QueueClient is unchanged apart from receiving its data from here.
"use client";
import { useEffect, useState } from "react";
import { getPosts, type Post } from "@/lib/api";
import { useServerStatus } from "@/components/ServerStatusProvider";
import { demoPosts } from "@/lib/mock";
import QueueClient from "./QueueClient";

export default function QueuePage() {
  const [posts, setPosts] = useState<Post[]>(demoPosts);
  // Liveness is the shared server status (v0.9.3). Demo data is
  // labelled only once a probe has actually failed, not while the
  // first probe is still checking.
  const server = useServerStatus();
  const live = server.live;
  // Re-runs when liveness flips, so the page recovers on its own
  // when the server comes back.
  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    (async () => {
      const fetched = await getPosts();
      if (cancelled) return;
      if (fetched !== null) setPosts(fetched);
    })();
    return () => {
      cancelled = true;
    };
  }, [live]);
  return <QueueClient posts={posts} demo={!live && server.status === "reconnecting"} />;
}
