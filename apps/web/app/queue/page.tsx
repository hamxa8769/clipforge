// Client component: posts + liveness are fetched in the browser so this
// page can be statically exported. Same UI and demo fallback as before;
// QueueClient is unchanged apart from receiving its data from here.
"use client";
import { useEffect, useState } from "react";
import { apiGet, getPosts, type Post } from "@/lib/api";
import { demoPosts } from "@/lib/mock";
import QueueClient from "./QueueClient";

export default function QueuePage() {
  const [posts, setPosts] = useState<Post[]>(demoPosts);
  const [live, setLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [fetched, health] = await Promise.all([getPosts(), apiGet<unknown>("/health")]);
      if (cancelled) return;
      if (fetched !== null) setPosts(fetched);
      setLive(health !== null);
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return <QueueClient posts={posts} demo={!live} />;
}
