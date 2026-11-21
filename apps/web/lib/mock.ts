import type { AnalyticsSnapshot, Candidate, ChannelStatus, Job, Monetization, Post, SocialAccount } from "./api";

// Demo data only — shown with a "Demo data" badge whenever the live API is unreachable.
export const demoJobs: Job[] = [
  { id: "job-001", title: "Weekly podcast ep. 42 — growth tactics", source: "podcast-ep42.mp4", status: "ready", created_at: "2026-11-20T10:00:00Z", candidates: 5, error: null, source_type: "local", own_content: true, stage: "review", progress_pct: 95, eta_s: null, demo: true, blocked_by: null, failed_stage: null, started_at: "2026-11-20T10:00:05Z", finished_at: "2026-11-20T10:00:47Z", duration_s: 42, stages: [], worker_runs: [] },
  { id: "job-002", title: "Product launch walkthrough", source: "launch-walkthrough.mp4", status: "scoring", created_at: "2026-11-20T11:30:00Z", candidates: 0, error: null, source_type: "local", own_content: true, stage: "score", progress_pct: 55, eta_s: 30, demo: true, blocked_by: null, failed_stage: null, started_at: "2026-11-20T11:30:02Z", finished_at: null, duration_s: null, stages: [], worker_runs: [] },
  { id: "job-003", title: "Founder story interview", source: "founder-story.mp4", status: "transcribing", created_at: "2026-11-20T12:05:00Z", candidates: 0, error: null, source_type: "local", own_content: true, stage: "transcribe", progress_pct: 30, eta_s: 45, demo: true, blocked_by: null, failed_stage: null, started_at: "2026-11-20T12:05:01Z", finished_at: null, duration_s: null, stages: [], worker_runs: [] },
  { id: "job-004", title: "Q&A: pricing mistakes", source: "qa-pricing.mp4", status: "failed", created_at: "2026-11-19T16:20:00Z", candidates: 0, error: "Transcription failed: Whisper could not read the audio track (unsupported codec).", source_type: "local", own_content: true, stage: "transcribe", progress_pct: 30, eta_s: null, demo: true, blocked_by: null, failed_stage: "transcribe", started_at: "2026-11-19T16:20:03Z", finished_at: "2026-11-19T16:20:31Z", duration_s: 28, stages: [], worker_runs: [] },
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
  { id: "post-001", clip_id: "cand-001", platform: "Instagram", caption: "Stop posting every day — daily posting is not the growth hack you think it is. Save this for your next content plan.", scheduled_at: "2026-11-21T11:00:00", status: "pending_approval", error: null, attempts: 0 },
  { id: "post-002", clip_id: "cand-001", platform: "Facebook", caption: "Practical takeaway: consistency beats volume. One strong Reel a week outperforms seven rushed ones.", scheduled_at: "2026-11-21T13:00:00", status: "pending_approval", error: null, attempts: 0 },
  { id: "post-003", clip_id: "cand-001", platform: "YouTube", caption: "Stop Posting Every Day (It Hurts Your Reach) — why posting daily can hurt Shorts reach.", scheduled_at: "2026-11-21T15:00:00", status: "pending_approval", error: null, attempts: 0 },
  { id: "post-004", clip_id: "cand-002", platform: "TikTok", caption: "Forget views. Watch this one number instead. #creatortips", scheduled_at: "2026-11-21T19:00:00", status: "pending_approval", error: null, attempts: 0 },
  { id: "post-005", clip_id: "cand-002", platform: "YouTube", caption: "The 1 Metric That Predicts Viral Shorts — viewed-vs-swiped-away beats raw views.", scheduled_at: "2026-11-20T15:00:00", status: "failed", error: "YouTube upload refused: quota exceeded for this project (check Google Cloud Console → YouTube Data API v3 → Quotas).", attempts: 2 },
  { id: "post-006", clip_id: "cand-003", platform: "Instagram", caption: "We Posted at 7pm for 30 Days — Results.", scheduled_at: "2026-11-20T11:00:00", status: "published", error: null, attempts: 1 },
];

export const demoMonetization: Monetization[] = [
  // Mirrors the live GET /monetization rows exactly (src/clipforge/monetization/__init__.py):
  // YouTube returns three rows (Early, Full, Feb-2027), Instagram/Facebook two each.
  { platform: "YouTube", program: "YPP Early (fan funding + Shopping)", current: { subscribers: 642, watch_hours_12mo: 1840, shorts_views_90d_alt: 412000 }, threshold: { subscribers: 500, watch_hours_12mo: 3000, shorts_views_90d_alt: 3000000 }, progress_pct: 61, note: "500 subs + 3 public uploads/90d + 3,000h/12mo OR 3M Shorts views/90d." },
  { platform: "YouTube", program: "YPP Full (ads + Premium)", current: { subscribers: 642, watch_hours_12mo: 1840, shorts_views_90d_alt: 412000 }, threshold: { subscribers: 1000, watch_hours_12mo: 4000, shorts_views_90d_alt: 10000000 }, progress_pct: 46, note: "1,000 subs + 4,000h/12mo OR 10M Shorts views/90d. Shorts watch time does NOT count to the 4,000h. Shorts ad revenue pooled, 45% to creator." },
  { platform: "YouTube", program: "YPP Full — NEW APPLICANTS FROM 1 FEB 2027", current: { subscribers: 642, watch_hours_12mo: 1840, shorts_views_90d_alt: 412000 }, threshold: { subscribers: 1000, watch_hours_12mo: 8000, shorts_views_90d_alt: 20000000 }, progress_pct: 23, note: "FLAG: thresholds DOUBLE for new applicants from 1 Feb 2027 (8,000h or 20M Shorts views). Accept new YPP terms by 31 Jan 2027." },
  { platform: "TikTok", program: "Creator Rewards Program", current: { followers: 3800, views_30d: 42000 }, threshold: { followers: 10000, views_30d: 100000 }, progress_pct: 38, note: "Videos must be original and at least 1 minute long to earn. Personal account only; 8 eligible countries." },
  { platform: "Instagram", program: "Gifts", current: { followers: 1240 }, threshold: { followers: 500 }, progress_pct: 100, note: "Pro account, 18+, payout account. $0.01 per star. Reels Play bonus ENDED 31 Aug 2025 — never promise per-view pay." },
  { platform: "Instagram", program: "Subscriptions", current: { followers: 1240 }, threshold: { followers: 10000 }, progress_pct: 12, note: "Pro account, 18+. Creator-set price." },
  { platform: "Facebook", program: "Content Monetization Program (CMP)", current: { followers: 2100 }, threshold: { followers: 10000 }, progress_pct: 21, note: "Page (not personal profile) or pro-mode profile. 10,000 followers per CMP guide — some guides cite 5,000 (UNVERIFIED). Reel must be >=10s to earn; views <5s excluded. In-stream Ads / Ads on Reels / Performance Bonus stopped earning 31 Aug 2025." },
  { platform: "Facebook", program: "Stars", current: { followers: 2100 }, threshold: { followers: 500 }, progress_pct: 100, note: "500 followers + 1 live in 60d. $0.01 per star, $100 minimum." },
];

export const demoAccounts: SocialAccount[] = [
  { id: "acct-yt", provider: "youtube", label: "@clipforge-demo", account_ref: "UCdemo0001", scopes: "youtube.upload, youtube.readonly", token_env_var: "YOUTUBE_REFRESH_TOKEN", status: "active", daily_cap_override: null, token_configured: true },
  { id: "acct-ig", provider: "instagram", label: "@clipforge.demo", account_ref: "17841400000000001", scopes: "instagram_basic, instagram_content_publish", token_env_var: "META_ACCESS_TOKEN", status: "active", daily_cap_override: null, token_configured: true },
  { id: "acct-fb", provider: "facebook", label: "ClipForge Page", account_ref: "10220000000000001", scopes: "pages_manage_posts, pages_read_engagement", token_env_var: "META_ACCESS_TOKEN", status: "active", daily_cap_override: null, token_configured: false },
  { id: "acct-tt", provider: "tiktok", label: "@clipforge.demo", account_ref: "open-id-demo", scopes: "video.upload", token_env_var: "TIKTOK_ACCESS_TOKEN", status: "pending_audit", daily_cap_override: null, token_configured: false },
];

/** Demo channel cards — same shape as GET /channels, shown only when the
 *  API is unreachable. Labelled demo everywhere they are shown. */
export const demoChannels: ChannelStatus[] = (["youtube", "instagram", "facebook", "tiktok"] as const).map((p) => ({
  platform: p,
  display_name: p === "youtube" ? "YouTube" : p === "instagram" ? "Instagram" : p === "facebook" ? "Facebook" : "TikTok",
  status: p === "youtube" ? "connected" : "not_connected",
  account_name: p === "youtube" ? "@clipforge-demo" : "",
  avatar_url: "",
  account_ref: "",
  scopes: [],
  daily_cap: p === "youtube" ? 6 : p === "instagram" ? 100 : p === "facebook" ? 30 : 5,
  expires_at: null,
  source: p === "youtube" ? "oauth" : "none",
  review_notice: p === "tiktok"
    ? "TikTok Content Posting API audit is pending: unaudited apps can only create private / inbox drafts."
    : "",
  fallback: "Manual-post pack: download the clip + SEO copy and post it yourself.",
  can_refresh: false,
}));

/** Demo analytics snapshots — numbers match demoMonetization so the dashboard stays consistent. Labelled demo everywhere they are shown. */
export const demoAnalytics: AnalyticsSnapshot[] = [
  { account_id: "acct-yt", platform: "youtube", date: "2026-11-20T09:00:00Z", views: 412000, followers: 642, watch_hours: 1840, shorts_views_90d: 412000, completion_pct: 62, viewed_pct: 74, signals: ["Strong viewed% (>=70%): this hook works — reuse its pattern."] },
  { account_id: "acct-ig", platform: "instagram", date: "2026-11-20T09:00:00Z", views: 38000, followers: 1240, watch_hours: 0, shorts_views_90d: 0, completion_pct: 48, viewed_pct: 52, signals: ["Low completion (<50%): viewers drop off — shorten, or move the payoff earlier; check the retention drop-off point."] },
  { account_id: "acct-fb", platform: "facebook", date: "2026-11-20T09:00:00Z", views: 21000, followers: 2100, watch_hours: 0, shorts_views_90d: 0, completion_pct: null, viewed_pct: null, signals: ["Not enough retention data yet — publish, then re-check viewed% / completion before cutting the next clip."] },
  { account_id: "acct-tt", platform: "tiktok", date: "2026-11-20T09:00:00Z", views: 42000, followers: 3800, watch_hours: 0, shorts_views_90d: 0, completion_pct: 55, viewed_pct: 61, signals: ["Not enough retention data yet — publish, then re-check viewed% / completion before cutting the next clip."] },
];
