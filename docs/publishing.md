# Publishing — per-platform checklists (v0.1.0)

All four platforms publish **only through their official APIs**, only
after human approval, and only inside their rate limits. There is no
auto-post switch and no unofficial uploader anywhere in ClipForge.

Budget weeks for these gates. They are the critical path for publishing,
not the code.

## Common flow (every platform)

1. Create the platform developer app / Cloud project.
2. Configure OAuth redirect URIs (`http://localhost:8000/auth/<platform>/callback`
   for dev; your production domain for prod).
3. Connect an account in ClipForge → tokens stored encrypted, refresh
   handled by the adapter.
4. Pass the platform's review / audit (checklists below).
5. Approve a clip in the review UI → scheduler slots it inside the daily
   cap → adapter publishes (or drafts, for TikTok pre-audit).
6. If a gate is not passed yet, use the **manual-post pack**: the rendered
   MP4 + per-platform caption copy as a download, posted by hand.

## YouTube Shorts — Data API v3

- [ ] Google Cloud project created; YouTube Data API v3 enabled.
- [ ] OAuth 2.0 client (web), scope `youtube.upload`; consent screen
      configured; test users added while in Testing mode (sign-ins expire
      in 7 days in Testing mode).
- [ ] Compliance audit requested for public uploads / quota increase
      (unverified projects created after 28 Jul 2020 upload private-only).
- [ ] **Quota — VERIFY_LIVE in Cloud Console:** sources conflict. Older
      docs: 1,600 units/upload ≈ 6/day from the 10,000-unit pool. Newer
      sources: `videos.insert` = 1 unit with a separate 100-uploads/day
      bucket. ClipForge reads the quota from the console response and
      never assumes one figure. Quota increase is free but audit-gated
      (1–6 weeks).
- [ ] Shorts are defined by the **file** (vertical/square, ≤3 min) — no
      `#Shorts` flag needed. `publishAt` schedules via private-then-public.
- [ ] Custom Shorts thumbnails are YPP-only, in YouTube Studio — the API
      `thumbnails.set` does not cover Shorts; do not promise them.
- [ ] Shorts >1 min with an active Content ID claim are blocked globally —
      warn before publishing a >60 s Short with third-party audio.

## Instagram Reels — Graph API (v26)

- [ ] Professional account (Business or Creator). Personal accounts cannot
      use the API at all.
- [ ] Meta app with Instagram Login (`instagram_business_basic`,
      `instagram_business_content_publish`) or Facebook Login
      (`instagram_basic`, `instagram_content_publish`, `pages_*`).
- [ ] **App Review** (screencasts per permission, privacy policy + data-
      deletion URL) + **Business Verification** for Advanced Access.
      Standard Access covers app roles only.
- [ ] Limits (read `content_publishing_limit` live, do not hard-code):
      100 API posts / rolling 24 h (Reels + feed + Stories combined),
      200 calls/hr/user. File: 3 s–15 min, ≤300 MB, ≤1920 px wide,
      MP4/MOV H.264 + AAC, 23–60 fps.
- [ ] Hashtag cap is **5** in-app (Dec 2025), even though API docs still
      say 30. ClipForge validates to 5.
- [ ] Two-step publish: `POST /{ig-user}/media` (`media_type=REELS`,
      public `video_url`) → poll `status_code` → `POST /{ig-user}/media_publish`.

## Facebook Reels — Graph API

- [ ] Facebook Page (not a personal profile) + Meta app with
      `pages_manage_posts` (+ `pages_show_list`, `pages_read_engagement`).
- [ ] App Review (+ Business Verification for other people's Pages).
- [ ] Limits: **30 Reels / rolling 24 h per Page**; **3–90 s via API**
      (in-app Reels have no length cap — the API still caps at 90 s),
      ≥540×960, MP4 recommended, 24–60 fps.
- [ ] Endpoint: `/{page}/video_reels`.

## TikTok — Content Posting API (the hard gate)

- [ ] TikTok developer app + Login Kit OAuth 2.0 (PKCE for mobile/desktop;
      access token 24 h, refresh token 365 d).
- [ ] Scopes: `video.upload` (Upload / inbox draft) and `video.publish`
      (Direct Post).
- [ ] Domain / URL-prefix verification if using `PULL_FROM_URL`.
- [ ] **Audit is the real gate.** App review with ≥1 end-to-end demo
      video; changes are re-reviewed. Practitioners report ~6 weeks and
      12+ submissions. The app must be for a **wide audience** — an
      in-house tool for your own accounts fails the audit, and an app that
      copies arbitrary content from other platforms fails it too.
- [ ] **Until the audit passes, ClipForge uses Upload mode only** (inbox
      draft — the creator opens TikTok to finish/publish). No audit is
      needed for drafts.
- [ ] Unaudited Direct Posts are forced `SELF_ONLY` (private), the posting
      account must itself be private, ≤5 users/24 h. ClipForge never
      enables Direct Post pre-audit.
- [ ] Posting screen rules (Content Sharing Guidelines): show creator
      nickname, check max duration, privacy dropdown with **no default**
      (creator must pick), Duet/Stitch/Comment unchecked by default,
      commercial-content + music-usage declaration, preview + consent,
      no watermarks, `is_aigc` flag for AI content.
- [ ] Limits: Direct Post 6 req/min per user token; Upload ≤5 pending
      drafts/user/24 h; video ≤4 GB, ≤10 min (per-account cap 3/5/10 min —
      check `creator_info.max_video_post_duration_sec`).

## Fallback: manual-post pack

When a platform gate is not passed, ClipForge exports a ZIP per clip:
rendered 1080×1920 MP4 + a text file per platform with its title,
description, hashtags, and best posting time. The creator posts by hand.
The pack is also the audit trail: what was approved, when, by whom.
