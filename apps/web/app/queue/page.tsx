import { apiGet, getPosts } from "@/lib/api";
import { demoPosts } from "@/lib/mock";
import QueueClient from "./QueueClient";

export default async function QueuePage() {
  const posts = (await getPosts()) ?? demoPosts;
  const live = (await apiGet<unknown>("/health")) !== null;
  return <QueueClient posts={posts} demo={!live} />;
}
