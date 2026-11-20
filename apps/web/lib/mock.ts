import type { Candidate, Job, Monetization, Post } from "./api";

// Demo data only — shown with a "Demo data" badge whenever the live API is unreachable.
export const demoJobs: Job[] = [
  { id: "job-001", title: "Weekly podcast ep. 42 — growth tactics", source: "podcast-ep42.mp4", status: "ready", created_at: "2026-11-20T10:00:00Z", candidates: 5 },
  { id: "job-002", title: "Product launch walkthrough", source: "launch-walkthrough.mp4", status: "scoring", created_at: "2026-11-20T11:30:00Z", candidates: 0 },
  { id: "job-003", title: "Founder story interview", source: "founder-story.mp4", status: "transcribing", created_at: "2026-11-20T12:05:00Z", candidates: 0 },
  { id: "job-004", title: "Q&A: pricing mistakes", source: "qa-pricing.mp4", status: "failed", created_at: "2026-11-19T16:20:00Z", candidates: 0 },
];

const seo = (title: string, desc: string, tags: string[]): { title: string; description: string; hashtags: string[] } => ({ title, description: desc, hashtags: tags });

export const demoCandidates: Candidate[] = [
  {
    id: "cand-001", job_id: "job-001", score: 87, hook_rating: 9,
    hook_line: "Stop posting every day — it is hurting your reach.",
    reason: "Contrarian opinion bomb in the first 3 seconds, clear practical value, quotable line.",
    transcript_excerpt: "…most creators think volume wins. It does not. The algorithm rewards completion, not frequency — one strong clip a week beats seven forgettable ones…",
    start_s: 342, end_s: 378, status: "pending", n: 1, file_path: null,
    seo: {
      youtube: seo("Stop Posting Every Day (It Hurts Your Reach)", "Why posting daily can hurt Shorts reach — and what to do instead. Full episode linked.", ["#YouTubeShorts", "#CreatorTips", "#Growth"]),
      tiktok: seo("Stop posting every day", "Posting daily is hurting your reach. Here is why — and the fix. #creatortips", ["#creatortips", "#growth", "#contentcreator", "#fyp", "#learnontiktok"]),
      instagram: seo("Stop posting every day", "Daily posting is not the growth hack you think it is. Save this for your next content plan.", ["#Reels", "#CreatorTips", "#ContentStrategy"]),
      facebook: seo("Stop posting every day", "Practical takeaway: consistency beats volume. One strong Reel a week outperforms seven rushed ones.", ["#CreatorTips", "#SocialMediaTips"]),
    },
  },
  {
    id: "cand-002", job_id: "job-001", score: 74, hook_rating: 7,
    hook_line: "The one metric that actually predicts a viral clip.",
    reason: "Revelation + practical value; hook is strong but payoff arrives late (around 8s).",
    transcript_excerpt: "…views lie. The metric that matters is viewed-versus-swiped-away — if people stay past the first three seconds, everything else follows…",
    start_s: 512, end_s: 545, status: "pending", n: 2, file_path: null,
    seo: {
      youtube: seo("The 1 Metric That Predicts Viral Shorts", "Viewed-vs-swiped-away beats raw views. How to read it and fix a weak hook.", ["#YouTubeShorts", "#Analytics", "#Shorts"]),
      tiktok: seo("This metric predicts viral clips", "Forget views. Watch this one number instead.", ["#creatortips", "#analytics", "#viral", "#growth"]),
      instagram: seo("The metric that predicts viral Reels", "Views lie — this number does not. Check it before your next Reel.", ["#Reels", "#Analytics", "#CreatorTips"]),
      facebook: seo("The metric that predicts viral Reels", "Short version: track viewed-vs-swiped-away, not views.", ["#SocialMediaTips", "#Analytics"]),
    },
  },
  {
    id: "cand-003", job_id: "job-001", score: 61, hook_rating: 6,
    hook_line: "We tried posting at 7pm for a month — here is what happened.",
    reason: "Story peak with a real result, but the hook is slow and the clip needs a tighter trim.",
    transcript_excerpt: "…for thirty days we posted at seven in the evening, same format, same length. Reach went up on TikTok, flat everywhere else…",
    start_s: 891, end_s: 930, status: "pending", n: 3, file_path: null,
    seo: {
      youtube: seo("We Posted at 7pm for 30 Days — Results", "A month-long posting-time experiment across all four platforms.", ["#YouTubeShorts", "#Experiment", "#Growth"]),
      tiktok: seo("Posting at 7pm for 30 days", "The results surprised us. Full breakdown in the comments.", ["#experiment", "#growth", "#tiktokgrowth"]),
      instagram: seo("7pm posting experiment: 30 days", "One month, one posting time. Here is what changed.", ["#Reels", "#Experiment", "#Growth"]),
      facebook: seo("7pm posting experiment: 30 days", "Practical result: timing matters most on TikTok, least on Facebook.", ["#Experiment", "#Growth"]),
    },
  },
];

export const demoPosts: Post[] = [
  { id: "post-001", clip_id: "cand-001", platform: "Instagram", caption: "Stop posting every day — daily posting is not the growth hack you think it is. Save this for your next content plan.", scheduled_at: "2026-11-21T11:00:00", status: "pending_approval" },
  { id: "post-002", clip_id: "cand-001", platform: "Facebook", caption: "Practical takeaway: consistency beats volume. One strong Reel a week outperforms seven rushed ones.", scheduled_at: "2026-11-21T13:00:00", status: "pending_approval" },
  { id: "post-003", clip_id: "cand-001", platform: "YouTube", caption: "Stop Posting Every Day (It Hurts Your Reach) — why posting daily can hurt Shorts reach.", scheduled_at: "2026-11-21T15:00:00", status: "pending_approval" },
  { id: "post-004", clip_id: "cand-002", platform: "TikTok", caption: "Forget views. Watch this one number instead. #creatortips", scheduled_at: "2026-11-21T19:00:00", status: "pending_approval" },
];

export const demoMonetization: Monetization[] = [
  { platform: "YouTube", program: "YouTube Partner Program (full)", current: { subscribers: 642, watch_hours_12mo: 1840, shorts_views_90d: 412000 }, threshold: { subscribers: 1000, watch_hours_12mo: 4000, shorts_views_90d: 10000000 }, progress_pct: 46, note: "From 1 Feb 2027, new applicants need 8,000 watch hours OR 20M Shorts views (subs unchanged at 1,000). Shorts watch time does not count toward the 4,000 hours." },
  { platform: "TikTok", program: "Creator Rewards Program", current: { followers: 3800, views_30d: 42000 }, threshold: { followers: 10000, views_30d: 100000 }, progress_pct: 38, note: "Videos must be original and at least 1 minute long to earn. Personal account only; 8 eligible countries." },
  { platform: "Instagram", program: "Gifts / Subscriptions", current: { followers: 1240 }, threshold: { followers_gifts: 500, followers_subscriptions: 10000 }, progress_pct: 12, note: "Gifts unlock at 500 followers ($0.01/star). Subscriptions at 10,000. Reels Play bonus ended 31 Aug 2025 — no per-view Reels pay." },
  { platform: "Facebook", program: "Content Monetization Program", current: { followers: 2100 }, threshold: { followers: 10000 }, progress_pct: 21, note: "Page (not personal profile) with 10,000 followers. Reels must be at least 10 seconds to earn; views under 5s do not count." },
];
