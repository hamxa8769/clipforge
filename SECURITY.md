# Security Policy — ClipForge

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (current alpha) | ✅ Best-effort fixes; report anything you find |

## Reporting a vulnerability

**Do not open a public GitHub issue for a vulnerability.** Email the
maintainer via the contact listed on the GitHub repository profile, or open
a private security advisory on GitHub. Include:

- what is affected (component, version/commit),
- steps to reproduce (without live credentials),
- impact you believe it has.

You will get an acknowledgement, and a fix or mitigation plan, before any
public disclosure. Please allow reasonable time for a fix before disclosing.

## What ClipForge does with secrets and tokens

- **No secrets in the repo, ever.** No API keys, OAuth tokens, keystores,
  service-account files, or filled-in `.env`. `.env`, `*.local.json`,
  `*.keystore`, `*.pem`, and `*.key` are gitignored, and pre-commit runs
  `detect-private-key` as a second net.
- **OAuth tokens are encrypted at rest** (per-account token vault) and
  refreshed per platform rules. ClipForge never asks for a platform
  password or browser cookies — OAuth only, least-privilege scopes.
- **Least privilege:** each platform adapter requests only the scopes it
  needs (`youtube.upload`, `instagram_business_content_publish`,
  `pages_manage_posts`, `video.publish` / `video.upload`). Tokens are never
  logged; publish attempts store request/response metadata, not tokens.
- **Local dev:** `SECRET_KEY` in `.env` must be a random value (see
  `.env.example`); the placeholder `CHANGE_ME_...` must never be used in a
  shared deployment.
- **User content:** videos and transcripts are personal data — encrypted
  transport, retention controls, delete-on-request, no sale of data. Video
  or audio is sent to an LLM provider only when the user opts in
  (transcript-text-only is the default).

## Dependency hygiene

- Dependencies are pinned to minimum versions in `pyproject.toml`; review
  new dependencies for licence (MIT/Apache-2.0/BSD preferred; **AGPL code
  is never merged** — see the DO-NOT list in `README.md`) and for supply-
  chain risk before adding.
- Heavy/optional capabilities (local Whisper, OpenCV, YouTube client) are
  optional extras, not default installs, so a minimal install has a
  minimal attack surface.
