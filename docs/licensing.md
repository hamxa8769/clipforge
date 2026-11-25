# Licensing — metadata only (v0.11.0)

## What changed

Up to v0.9.0, ClipForge enforced a hard licence block (introduced in
v0.7.0): a video under the YouTube Standard licence, or a video whose
licence could not be verified, was refused at job creation (422), at
post creation and publish time (409), in the scheduler
(`skipped_licence`), in the trending/agent run guardrail (422), and in
live capture / the worker's live-ingest re-check
(`live_not_clippable`). Only the user's own videos, public-domain
videos, and Creative Commons videos could be clipped and posted.

In v0.9.1 the hard block was **removed at the user's explicit
instruction** (2026-11-23), after they checked YouTube's terms and
conditions and confirmed clipping is officially allowed. The licence
became **reported, never enforced**, and a non-blocking rights notice
was shown instead.

In v0.11.0 the remaining notices were **removed at the user's explicit
instruction** (2026-11-25) — "remove all warnings/restrictions, the
licensed ones", same instruction as v0.9.1. There is now **no rights /
licence notice anywhere in the app**: no banner on the Dashboard, no
notice where a video link is entered, no licence check step, no
"Licensed only (CC)" toggle, no licence badges, no rights /
licence acknowledgement checkboxes, and no attribution nagging in
the UI. The user is responsible for the rights to what they clip —
that is stated **once in the release notes, not in the app UI**.

## The model now

- **Any pasted video link proceeds** through the pipeline — job
  creation, clip, schedule and publish. No code path refuses a run,
  clip, schedule or publish solely because of a licence.
- **The licence is recorded as metadata only.** The licence record
  (`source_license`, `source_url`, `source_title`, `source_channel`,
  `attribution_text`) still travels from job → candidate → post, and
  `GET /api/videos/license?url=...` still reports what licence a
  video has (`{video_id, title, channel, channel_id, license,
  license_url, can_clip, reason}`; `can_clip` is always `true`).
  Nothing in the UI displays it any more.
- **No licence check in any flow.** The dashboard one-box / New clip
  run, Trending ("Clip this video", trending-to-agent run, live
  capture), Batch mode, the Agent page, the setup wizard, and Clip
  Studio all start without a licence check, a licence badge, or a
  rights checkbox.
- **No rights gates in the agent or trending runs.**
  `AgentConfig.validate()` checks structure only; `POST
  /api/trending/run` and `POST /api/trending/live/capture` no longer
  require `rights_confirmed`, and a trending video URL is no longer
  refused as a clip source. The `rights_confirmed` /
  `own_content_declaration` fields are still sent (always `true`)
  for wire compatibility — the server-side ingest still requires the
  declaration for YouTube URLs (`clipforge.ingest`), which was out
  of scope for v0.11.0 and is unchanged.
- **CC attribution still exists, but only in the SEO pack.** Where
  the licence is known to be Creative Commons (or public-domain),
  `clipforge.licensing.build_attribution_text` /
  `append_attribution` can still build the CC-BY credit block and
  the SEO step appends it to generated descriptions, idempotently.
  The review UI no longer shows an attribution block or says the
  credit "cannot be removed".
- **`licensed_only` still exists server-side** as an opt-in search
  filter (`GET /api/trending/search?licensed_only=true` restricts
  discovery to Creative Commons videos, verified per video). The UI
  no longer passes it and no toggle is shown.
- **The copyright scan is unchanged as a feature** — it still runs
  before publishing and still reports its findings — but its
  disclaimer no longer carries the "only publish clips made from
  videos you own or hold a licence for" sentence.

## What did NOT change

- **Approval-first publishing is unchanged.** Posts still need
  approval before publishing; dry-run still gates live publishing.
- **Keys stay local-only.** API keys and secrets are still stored
  encrypted on the server (OS keyring / encrypted file), masked to
  the last 4 characters in the UI, never in the repo, never in a log.
  That rule was explicitly NOT part of the licence removal.
- **Genuine operational errors stay.** Tool missing / broken,
  download failed, server down, model not downloaded — those are
  functional errors, not licence restrictions, and the UI still
  reports them plainly.
- **B-roll keeps its declaration check.** `clipforge.broll` still
  requires a source/rights declaration for uploaded B-roll assets —
  a separate subsystem, out of scope for v0.11.0.

## Code map (v0.11.0)

| Area | Behaviour now |
| --- | --- |
| `clipforge.licensing.publish_blocked_reason` | Kept for compatibility; always returns `None`. |
| `clipforge.licensing.is_lawful_source` | Pure classifier (own / public-domain / CC); gates nothing. |
| `clipforge.licensing.RIGHTS_ADVISORY` | **Deleted.** No notice text exists any more. |
| `clipforge.trending.RIGHTS_NOTICE` / `TRENDING_NOTICE` | **Deleted.** API responses no longer carry a `notice` field. |
| `clipforge.trending.refuse_trending_source` | **Deleted.** `is_trending_source` stays as a pure helper. |
| `clipforge.trending.check_video_license` | Always `can_clip: true`; reason is a plain licence label. |
| `clipforge.live.LIVE_NOTICE` | **Deleted.** `clip_decision` reasons are plain labels. |
| `clipforge.agent.RIGHTS_NOTICE` | **Deleted.** `validate()` checks structure only. |
| `clipforge.agent` config output | No `rights_notice` field any more. |
| `POST /jobs` (`clipforge_api/app.py`) | No licence gate; licence recorded as metadata. |
| `POST /posts`, `POST /posts/{id}/publish` | No licence 409. |
| Scheduler (`clipforge.scheduler`) | No `skipped_licence`; due posts publish normally. |
| `POST /api/trending/run` | No rights gate, no trending-source refusal. |
| `POST /api/trending/live/capture` | No rights gate; licence re-verified for an accurate record. |
| Web UI (`api.ts`, `NewClipRun`, `OneBox`, Trending, Dashboard, Studio, Batch, Agent, Review, Live, Export) | No licence text, badge, check or checkbox anywhere. |
| Web UI `api.ts` `RIGHTS_ADVISORY` / `licenseBadgeLabel` / `attributionForClip` | **Deleted.** `deriveCanClip` reasons are plain labels. |

## Honest limits

- ClipForge reports the licence YouTube returns (`status.license`:
  `creativeCommon` / `youtube` / absent). It cannot verify ownership,
  private licences, or whether a particular use is permitted — that
  judgement is the user's.
- Removing the block and the notices does not make any particular
  re-upload lawful; platform rules (YouTube reused-content policy,
  Content ID, strikes) still apply to what the user posts, and the
  user is responsible for the rights to what they post.
