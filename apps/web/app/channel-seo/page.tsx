// Channel SEO (v0.11.0, Worker B) — what works on a channel, in plain words.
//
// Paste a channel link (or use your connected YouTube channel) and see:
// the channel's top videos by views, the words and title shapes that win
// there, and suggested titles + tags for your next clip.
//
// Honesty rule: the numbers come from the public channel page (yt-dlp
// flat playlist, no download) or, when a YouTube Data API key is saved,
// from the API — the page says which. Views YouTube did not show are
// listed last and the page says so; nothing is ever invented. When the
// server is unreachable the page says so instead of showing demo
// numbers, because fake view counts would be worse than no data.
"use client";
import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  analyzeChannelSeo, getChannelSeo, getConnectedChannelSeo,
  type ChannelSeo,
} from "@/lib/api";
import { Card, CardHeader, EmptyState, ErrorBanner, ExternalLink, PageHeader, Skeleton, fmtCompact } from "@/components/ui";
import { useServerStatus } from "@/components/ServerStatusProvider";
import { CopyButton } from "@/components/SeoPack";

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtViews(n: number | null): string {
  return n === null ? "views not shown" : `${fmtCompact(n)} views`;
}

export default function ChannelSeoPage() {
  return (
    <Suspense fallback={<Skeleton className="h-48" />}>
      <ChannelSeoPageInner />
    </Suspense>
  );
}

function ChannelSeoPageInner() {
  const searchParams = useSearchParams();
  // Shareable link: /channel-seo?channel_url=…&topic=… opens the view
  // for that channel straight away (cached when fresh, else a live read).
  const initialUrl = searchParams.get("channel_url") ?? "";
  const initialTopic = searchParams.get("topic") ?? "";
  const server = useServerStatus();
  const live = server.live;
  const [channelUrl, setChannelUrl] = useState(initialUrl);
  const [topic, setTopic] = useState(initialTopic);
  const [data, setData] = useState<ChannelSeo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connectedName, setConnectedName] = useState<string | null>(null);

  async function run(promise: Promise<{ data: ChannelSeo | null; error: string | null }>) {
    setLoading(true);
    setError(null);
    const { data: result, error: err } = await promise;
    setLoading(false);
    if (err) {
      setError(err);
      setData(null);
      return;
    }
    setData(result);
    if (result?.connected_account_name) setConnectedName(result.connected_account_name);
  }

  // Auto-run once for a shared link (?channel_url=…&topic=…).
  const autoRan = useRef(false);
  useEffect(() => {
    if (autoRan.current || !initialUrl.trim()) return;
    autoRan.current = true;
    void run(getChannelSeo(initialUrl.trim(), initialTopic.trim()));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onAnalyze() {
    if (!channelUrl.trim() && !topic.trim()) {
      setError("Paste a YouTube channel link first — or use your connected channel below.");
      return;
    }
    void run(analyzeChannelSeo(channelUrl.trim(), topic.trim()));
  }

  function onUseConnected() {
    void run(getConnectedChannelSeo(topic.trim()));
  }

  const patterns = data?.what_works.title_patterns ?? {};
  const counted = typeof patterns.videos_counted === "number" ? patterns.videos_counted : 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Channel SEO"
        intro="Paste a channel link and see what works there — top videos, winning titles and tags, and ideas for your next clip. No key needed."
      />

      {!live && (
        <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-100">
          The server is not answering right now, so this page cannot read a channel. Start the server and try again — nothing here is demo data.
        </p>
      )}

      <Card>
        <div className="space-y-3">
          <label className="block text-sm">
            <span className="text-neutral-400">YouTube channel link</span>
            <input
              className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
              value={channelUrl}
              onChange={(e) => setChannelUrl(e.target.value)}
              placeholder="https://www.youtube.com/@channelname"
              aria-label="YouTube channel link"
            />
          </label>
          <label className="block text-sm">
            <span className="text-neutral-400">Your next clip is about (optional — for title ideas)</span>
            <input
              className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="e.g. evening routine"
              aria-label="Topic for your next clip"
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onAnalyze}
              disabled={loading || !live}
              className="rounded-md bg-amber-400 px-4 py-2 text-sm font-medium text-neutral-950 hover:bg-amber-300 disabled:opacity-60"
            >
              {loading ? "Reading the channel…" : "Look at this channel"}
            </button>
            <button
              type="button"
              onClick={onUseConnected}
              disabled={loading || !live}
              className="rounded-md bg-neutral-800 px-4 py-2 text-sm font-medium text-neutral-100 hover:bg-neutral-700 disabled:opacity-60"
            >
              Use my connected channel
            </button>
          </div>
          <p className="text-xs text-neutral-500">
            Reading a channel takes up to a couple of minutes the first time — it lists the channel&apos;s videos from the public page. The next visit loads instantly from a one-day cache.
          </p>
        </div>
      </Card>

      {error && <ErrorBanner message={error} onRetry={() => { if (channelUrl.trim()) void run(analyzeChannelSeo(channelUrl.trim(), topic.trim())); }} />}
      {loading && <Skeleton className="h-48" />}

      {!loading && !error && data === null && (
        <EmptyState
          title="No channel looked at yet"
          body="Paste a channel link above — yours, or a channel you want to learn from — and this page shows its top videos, what titles work there, and title + tag ideas for your next clip."
        />
      )}

      {data !== null && (
        <>
          <Card>
            <CardHeader
              title={data.channel_name || "Channel"}
              hint={`${data.subscriber_count !== null ? `${fmtCompact(data.subscriber_count)} subscribers · ` : ""}${data.videos_fetched} videos read${data.cached ? " · from cache" : ""}`}
            />
            <p className="text-sm text-neutral-400">{data.views_note}</p>
            {data.api_enrichment.note && <p className="mt-1 text-xs text-neutral-500">{data.api_enrichment.note}</p>}
            {connectedName && <p className="mt-1 text-xs text-neutral-500">Connected channel: {connectedName}.</p>}
            {data.fetched_at && <p className="mt-1 text-xs text-neutral-500">Read {fmtDate(data.fetched_at)}.</p>}
          </Card>

          <Card>
            <CardHeader title="Top videos" hint="Most views first" />
            {data.top_videos.length === 0 ? (
              <p className="text-sm text-neutral-400">No videos to show.</p>
            ) : (
              <ol className="space-y-2">
                {data.top_videos.map((v, i) => (
                  <li key={v.video_id || i} className="flex flex-wrap items-baseline gap-2 rounded-lg border border-neutral-800 p-3 text-sm">
                    <span className="w-6 shrink-0 font-bold text-neutral-400">{i + 1}.</span>
                    <ExternalLink href={v.url} className="min-w-0 flex-1 font-medium text-sky-300 underline">{v.title || v.video_id}</ExternalLink>
                    <span className="shrink-0 text-neutral-400">{fmtViews(v.views)}</span>
                    <span className="shrink-0 text-xs text-neutral-500">{fmtDate(v.published_at)}</span>
                  </li>
                ))}
              </ol>
            )}
          </Card>

          <Card>
            <CardHeader title="What works here" hint={data.what_works.basis} />
            <p className="text-sm font-medium text-neutral-200">Words in the top titles</p>
            {data.what_works.common_words.length === 0 ? (
              <p className="mt-1 text-sm text-neutral-400">Not enough titles to learn from yet.</p>
            ) : (
              <ul className="mt-2 flex flex-wrap gap-2">
                {data.what_works.common_words.map((w) => (
                  <li key={w.word} className="rounded-full border border-neutral-700 bg-neutral-800 px-3 py-1 text-sm">
                    {w.word} <span className="text-neutral-500">×{w.count}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-4 text-sm font-medium text-neutral-200">Title shapes</p>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-neutral-300">
              <li>{patterns.starts_with_how_to ?? 0} of the top {counted} start with “How to”.</li>
              <li>{patterns.starts_with_why ?? 0} of the top {counted} start with “Why”.</li>
              <li>{patterns.contains_number ?? 0} of the top {counted} put a number in the title.</li>
              <li>{patterns.contains_vs ?? 0} of the top {counted} use a “vs” comparison.</li>
              <li>{patterns.contains_question ?? 0} of the top {counted} ask a question.</li>
              <li>Average title length: {patterns.avg_title_chars ?? 0} characters.</li>
            </ul>
          </Card>

          <Card>
            <CardHeader title="Ideas for your next clip" hint="Built from this channel's winners" />
            {data.suggested_titles.length === 0 ? (
              <p className="text-sm text-neutral-400">No title ideas yet.</p>
            ) : (
              <ul className="space-y-2">
                {data.suggested_titles.map((s) => (
                  <li key={s.title} className="rounded-lg border border-neutral-800 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="flex-1 text-sm font-medium text-neutral-100">{s.title}</p>
                      <CopyButton text={s.title} label="Copy title" small />
                    </div>
                    <p className="mt-1 text-xs text-neutral-500">{s.why}</p>
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <p className="text-sm font-medium text-neutral-200">Suggested tags</p>
              <CopyButton text={data.suggested_tags.join(", ")} label="Copy tags" small />
            </div>
            <p className="mt-1 text-sm text-neutral-300">{data.suggested_tags.join(", ") || "—"}</p>
            <p className="mt-1 text-xs text-neutral-500">{data.suggested_tags_basis}</p>
          </Card>
        </>
      )}
    </div>
  );
}
