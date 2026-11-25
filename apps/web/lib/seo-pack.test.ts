// Unit tests for the v0.11.0 SEO client (lib/api.ts: SeoPack +
// ChannelSeo normalisers). Run with `npm run test:unit` — compiles this
// file + api.ts with tsc and runs it under Node's built-in runner.
import test from "node:test";
import assert from "node:assert/strict";
import {
  normalizeChannelSeo,
  normalizeSeoPack,
} from "./api";

const RAW_PACK = {
  keyword: "morning routine",
  copy_all: "SEO pack — morning routine",
  clip_candidate_id: "cand1",
  platforms: {
    youtube: {
      platform: "youtube", label: "YouTube Shorts", title: "Morning routine: stop doing this",
      title_options: ["Morning routine: stop doing this", "Stop doing this first"],
      description: "morning routine — stop doing this\n\n#morningroutine #shorts",
      caption: "Morning routine: stop doing this",
      tags: ["morning routine", "habits"], hashtags: ["morningroutine", "shorts"],
      warnings: [], tip: "Put the main word first.",
    },
    tiktok: {
      platform: "tiktok", label: "TikTok", title: "Stop doing this first",
      title_options: ["Stop doing this first"], description: "morning routine\nStop doing this first",
      caption: "morning routine\nStop doing this first", tags: ["morning routine"],
      hashtags: ["morningroutine", "fyp"], warnings: ["tiktok: clip under 60s is NOT eligible"], tip: "Say the main word first.",
    },
    instagram: { platform: "instagram", label: "Instagram Reels", title: "x", title_options: ["x"], description: "x", caption: "x", tags: [], hashtags: [], warnings: [], tip: "" },
    facebook: { platform: "facebook", label: "Facebook Reels", title: "x", title_options: ["x"], description: "x", caption: "x", tags: [], hashtags: [], warnings: [], tip: "" },
    x: { platform: "x", label: "X", title: "x", title_options: ["x"], description: "x", caption: "x", tags: [], hashtags: [], warnings: [], tip: "" },
  },
};

test("normalizeSeoPack keeps all five platforms and the title options", () => {
  const pack = normalizeSeoPack(RAW_PACK);
  assert.ok(pack);
  assert.equal(pack.keyword, "morning routine");
  assert.equal(Object.keys(pack.platforms).length, 5);
  assert.deepEqual(pack.platforms.youtube.title_options, [
    "Morning routine: stop doing this", "Stop doing this first",
  ]);
  assert.deepEqual(pack.platforms.youtube.tags, ["morning routine", "habits"]);
  assert.equal(pack.clip_candidate_id, "cand1");
  assert.equal(pack.copy_all, "SEO pack — morning routine");
});

test("normalizeSeoPack returns null when no platform has a title", () => {
  assert.equal(normalizeSeoPack({ platforms: {} }), null);
  assert.equal(normalizeSeoPack(null), null);
});

test("normalizeSeoPack tolerates missing fields", () => {
  const pack = normalizeSeoPack({ platforms: { youtube: { title: "Only a title" } } });
  assert.ok(pack);
  assert.deepEqual(pack.platforms.youtube.title_options, []);
  assert.deepEqual(pack.platforms.youtube.tags, []);
  assert.deepEqual(pack.platforms.tiktok.title_options, []);
});

const RAW_CHANNEL_SEO = {
  channel_id: "UC123",
  channel_name: "Daily Clips",
  channel_url: "https://www.youtube.com/@dailyclips",
  subscriber_count: 12000,
  videos_fetched: 2,
  views_source: "yt-dlp",
  views_note: "Views from the public channel page.",
  top_videos: [
    { video_id: "b", title: "Morning routine mistakes", url: "https://www.youtube.com/watch?v=b", views: 120000, duration_s: 48, published_at: "2026-10-28" },
    { video_id: "d", title: "No views shown", url: "https://www.youtube.com/watch?v=d", views: null, duration_s: 50, published_at: null },
  ],
  what_works: {
    common_words: [{ word: "morning", count: 4 }],
    title_patterns: { starts_with_how_to: 1, videos_counted: 2 },
    basis: "From the channel's top 2 videos by views.",
  },
  suggested_titles: [{ title: "How to evening routine", why: "1 of the top 2 videos start with “How to”." }],
  suggested_tags: ["morning", "routine", "evening"],
  suggested_tags_basis: "Words from the channel's top titles.",
  api_enrichment: { used: false, note: "No YouTube API key saved." },
  cached: false,
  fetched_at: "2026-11-25T00:00:00+00:00",
};

test("normalizeChannelSeo keeps top videos, words and suggestions", () => {
  const data = normalizeChannelSeo(RAW_CHANNEL_SEO);
  assert.ok(data);
  assert.equal(data.channel_name, "Daily Clips");
  assert.equal(data.subscriber_count, 12000);
  assert.equal(data.top_videos[0].views, 120000);
  assert.equal(data.top_videos[1].views, null);
  assert.equal(data.what_works.common_words[0].word, "morning");
  assert.equal(data.what_works.title_patterns.starts_with_how_to, 1);
  assert.equal(data.suggested_titles[0].title, "How to evening routine");
  assert.deepEqual(data.suggested_tags, ["morning", "routine", "evening"]);
  assert.equal(data.api_enrichment.used, false);
  assert.equal(data.cached, false);
});

test("normalizeChannelSeo returns null for garbage", () => {
  assert.equal(normalizeChannelSeo(null), null);
  assert.equal(normalizeChannelSeo({}), null);
  assert.equal(normalizeChannelSeo({ detail: "No YouTube channel is connected yet." }), null);
});
