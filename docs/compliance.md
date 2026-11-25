# Compliance — AI labelling, copyright, privacy (v0.1.0)

ClipForge is built to survive platform review. The rules below are
product requirements, not suggestions — a PR that breaks them does not
merge.

## Own content only

> v0.11.0 note: the licence hard block described below was removed —
> see `docs/licensing.md`. The licence is now recorded as metadata
> only, never enforced, and no rights / licence notice is shown
> anywhere in the app any more. The rights acknowledgement checkboxes
> (agent `rights_confirmed`, batch `own_content_declaration`) are
> gone from the UI — the declaration the server-side ingest requires
> for YouTube URLs is now sent with every job/run instead. The user
> is responsible for the rights to what they clip (stated in the
> release notes, not in the UI).

- Ingest requires a **source-ownership declaration** (`ownership_declared`)
  on every job. No declaration, no job.
- ClipForge never downloads other creators' videos (no "channel
  downloader"), never re-uploads third-party content, and never processes
  content the user does not own or hold rights to.
- Cross-posting **your own** video to all four platforms is not penalised.
  What is penalised: another app's watermark/logo in frame (TikTok
  watermark on a Reel, etc.), re-uploading other people's content without
  material creative edit, and channels that primarily aggregate/re-upload
  (YouTube, Oct 2026 Shorts update). Fix: always publish from the clean
  master file produced by ClipForge — cropping a watermark out is not a
  fix, and ClipForge never does it.
- Duplicate-risk warning: if the same file is queued to more than one
  account unchanged, the UI warns before approval.

## Reused / inauthentic content

YouTube (16 Jul 2026) treats three "inauthentic" types as non-monetizable,
and a whole channel can lose YPP: (1) generic / repetitive / template-based
content, including AI/CGI mass-produced and copied tutorials; (2)
off-putting / distressing / emotionally manipulative content; (3) AI
personas presented as experts on health, finance, legal, or politics.
TikTok / Instagram / Meta have equivalent originality rules (see the
research report §5).

Mitigations in ClipForge:

- Templates are varied per clip; human review is mandatory.
- Clips that are >X% identical in structure to the channel's recent output
  are flagged in review.
- AI-persona expert content in health / finance / legal / politics is
  blocked by default; AI-persona testimonials are blocked entirely
  (fake testimonials are banned, including AI personas as happy customers).

## AI-generated content labelling

| Platform | Rule |
|---|---|
| YouTube | Disclose at upload if AI meaningfully alters/generates photorealistic content — a real person seeming to say/do something they did not, an altered real event/place, a realistic scene that never happened, AI music as the main focus. Non-disclosure → label added, removal, YPP suspension. Auto-labelling from C2PA + internal detection since May 2026. |
| TikTok | Label realistic AI-generated / significantly edited media (creator toggle + `is_aigc` in the API + auto-detection). Misleading unlabeled AI is removed even if labeled, in banned categories (impersonation / fake authority). |
| Meta (FB/IG) | "AI info" from C2PA/IPTC signals or self-disclosure; penalties possible for required-but-missing labels on photorealistic video / realistic audio. |

ClipForge behaviour:

- Every AI-clipped / AI-voiced post carries the platform's AI label via
  the API (`is_aigc`, YouTube disclosure field, Meta signal preservation).
  **Do not strip C2PA metadata.**
- Per-clip "AI-assisted edit" toggle, **default on** when captions,
  reframing, or voiceover were AI-generated.
- Simple cleanup (captions, reframing) generally does not need a label;
  realistic generation does — the review UI explains the distinction.
- NY law (from 9 Jun 2026) requires conspicuous disclosure of AI
  performers in ads ($1,000 / $5,000 fines) — branded-content /
  paid-partnership toggles exist per platform.

## Copyrighted music / clips

- Use each platform's licensed library or royalty-free music per platform.
  Music licences are per-platform and time-limited — do not carry a TikTok
  sound into YouTube.
- A Short >1 min with an active Content ID claim is blocked globally.
  ClipForge warns before publishing a >60 s Short with third-party audio.
- B-roll comes only from licensed stock (e.g. Pexels) or the source video
  itself — never scraped from other creators' clips.

## Automation / spam

- Official APIs allow scheduled publishing **with owner consent per post**.
  ClipForge's approval queue is that consent, recorded per clip × platform.
- No auto-like / comment / DM / follow, no engagement pods, no bots to
  inflate metrics. Rate limits are respected with backoff (§3 caps in
  `ARCHITECTURE.md`).
- Never ask for passwords or cookies. OAuth only, least-privilege scopes.

## Data privacy

- User videos, transcripts, and tokens are personal data. Tokens are
  encrypted at rest; transcript retention is user-controlled; delete-on-
  request is supported; data is never sold.
- Meta App Review requires a public privacy policy + data-deletion URL;
  TikTok's audit checks data handling. Both are repo requirements before
  either review is submitted.
- Video/audio is **not** sent to an LLM provider unless the user opts in;
  transcript-text-only is the safer default.

## What ClipForge never does (summary)

No Selenium/cookie uploaders, no unofficial TikTok uploader, no Postiz
AGPL code, no third-party reposts, no watermark stripping, no stealth
publishing, no promised earnings, no Reels Play bonus claims.
