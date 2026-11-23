# Licensing — advisory model (v0.9.1)

## What changed in v0.9.1

Up to v0.9.0, ClipForge enforced a hard licence block (introduced in
v0.7.0): a video under the YouTube Standard licence, or a video whose
licence could not be verified, was refused at job creation (422), at
post creation and publish time (409), in the scheduler
(`skipped_licence`), in the trending/agent run guardrail (422), and in
live capture / the worker's live-ingest re-check
(`live_not_clippable`). Only the user's own videos, public-domain
videos, and Creative Commons videos could be clipped and posted.

In v0.9.1 the restriction was **removed at the user's explicit
instruction** (2026-11-23), after they checked YouTube's terms and
conditions and confirmed clipping is officially allowed. The licence
is now **reported, never enforced**.

## The advisory model

- **Any pasted video link proceeds** through the pipeline — job
  creation, clip, schedule and publish. No code path refuses a run,
  clip, schedule or publish solely because the licence is standard or
  unknown.
- **The licence check still exists** and still reports what licence a
  video has: `GET /api/videos/license?url=...` returns
  `{video_id, title, channel, channel_id, license, license_url,
  can_clip, reason}`. `can_clip` is always `true` now; `reason`
  carries the licence report (CC / own / public-domain keep their
  positive reasons; standard and unknown licences carry the rights
  notice below). The check never guesses or fakes a licence.
- **The rights notice is shown, not enforced.** Where a video link is
  entered (dashboard "New clip run", Trending "Check a video URL",
  Trending results) and before publishing, the UI shows:

  > Make sure you have the right to use this video — your own video,
  > a licensed video, or a use YouTube permits. You are responsible
  > for the rights to what you post.

  ClipForge does **not** guarantee rights clearance.
- **CC attribution is unchanged.** Where the licence is known to be
  Creative Commons (or public-domain), the CC-BY credit block is
  still auto-added to every generated description, idempotently —
  re-running SEO keeps exactly one block, never two
  (`clipforge.licensing.build_attribution_text` /
  `append_attribution`). The licence record
  (`source_license`, `source_url`, `source_title`, `source_channel`,
  `attribution_text`) still travels from job → candidate → post.
- **`licensed_only` is still an opt-in discovery filter.**
  `GET /api/trending/search?licensed_only=true` (and the "Licensed
  only (CC)" toggle) restricts trending / live discovery to
  Creative Commons videos, verified per video. It is a search filter
  the user chooses, not a refusal — turning it off shows all trending
  videos, and any of them can be clipped.

## What did NOT change

- **Rights acknowledgement checkboxes remain.** The agent's
  `rights_confirmed`, the batch `own_content_declaration`, and the
  live-capture `rights_confirmed` are the user's own acknowledgement,
  recorded with the run — they are not licence verdicts, and they
  still gate those flows (the server still returns 422 when the box
  is unticked). The dashboard job path (`POST /jobs`) no longer
  requires a declaration or a particular licence.
- **The trending video is still not a clip source.** Picking a
  trending video as the *topic* is the loop; the source is the user's
  own video on that topic (or a video pasted directly). A trending
  video URL used as the agent source is still refused (422) — that is
  the topic-vs-source rule, not a licence verdict.
- **Approval-first publishing is unchanged.** Posts still need
  approval before publishing; dry-run still gates live publishing.
- **The copyright scan is unchanged.** `clipforge.copyright_scan`
  still reports its findings (source licence, audio flags, duration,
  file presence) with its no-guarantee disclaimer; it advises, it
  does not block on licence.

## Code map (v0.9.1)

| Area | Behaviour now |
| --- | --- |
| `clipforge.licensing.publish_blocked_reason` | Kept for compatibility; always returns `None`. |
| `clipforge.licensing.is_lawful_source` | Pure classifier (own / public-domain / CC); gates nothing. |
| `clipforge.licensing.RIGHTS_ADVISORY` | The canonical notice text (single source of truth). |
| `clipforge.trending.check_video_license` | Always `can_clip: true`; reason is the licence report / notice. |
| `clipforge.live.clip_decision` / `check_can_clip` | Always clippable; `check_can_clip` never raises for a licence. |
| `POST /jobs` (`clipforge_api/app.py`) | No licence gate; licence recorded, CC attribution kept. |
| `POST /posts`, `POST /posts/{id}/publish` | No licence 409. |
| Scheduler (`clipforge.scheduler`) | No `skipped_licence`; due posts publish normally. |
| `POST /api/trending/run` | No licence verification gate. |
| `POST /api/trending/live/capture` | Licence re-verified for an accurate record; never blocks. |
| Worker live ingest (`clipforge.worker`) | No `live_not_clippable` re-check. |
| Web UI (`api.ts`, `NewClipRun`, Trending, Dashboard, Studio) | Licence reported + rights notice shown; no disabled-by-licence buttons. |

## Honest limits

- ClipForge reports the licence YouTube returns (`status.license`:
  `creativeCommon` / `youtube` / absent). It cannot verify ownership,
  private licences, or whether a particular use is permitted — that
  judgement is the user's, and the notice says so.
- Removing the block does not make any particular re-upload lawful;
  platform rules (YouTube reused-content policy, Content ID, strikes)
  still apply to what the user posts, and the user is responsible for
  the rights to what they post.
