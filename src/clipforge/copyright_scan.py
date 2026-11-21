"""Copyright scan before publish (v0.8.0, Worker C).

A **metadata check only** — it reads what the job/clip already
recorded (source licence, audio flags, duration, file presence) and
flags risks in plain language. It is NOT a Content ID check, NOT a
match against any rights database, and NOT a guarantee that content
is copyright-safe. Every response carries that disclaimer, and no
function here ever claims Content ID verification.

Flag severities: ``info`` (clean / for the record), ``warning``
(publishable, but confirm first), ``severe`` (do not publish until
resolved — currently unused: nothing in the metadata alone proves
infringement, so the scan never blocks on its own; the UI decides).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clipforge.publishing.limits import PLATFORM_LIMITS
from clipforge.x_platform import X_MAX_VIDEO_S

#: The disclaimer every scan response carries, verbatim.
NO_GUARANTEE_DISCLAIMER = (
    "This scan is a metadata check only — it does NOT guarantee that "
    "this clip is copyright-safe, and it is not a Content ID check. "
    "ClipForge cannot verify ownership against YouTube Content ID or "
    "any rights database. Only publish clips made from videos you own "
    "or hold a licence for, and check the licence terms (attribution, "
    "commercial use) yourself before publishing."
)

#: Licence strings (lowercase, substring-matched) that count as a
#: known, publishable source. Anything else is "unknown".
_KNOWN_LICENCE_MARKERS = (
    "creative commons",
    "cc-by",
    "cc by",
    "cc0",
    "cc-0",
    "public domain",
    "public-domain",
    "licensed",
    "licence",
    "license",
    "royalty-free",
    "royalty free",
    "own",
    "original",
)
#: Licence strings that are explicitly a known CC / public-domain source.
_CC_MARKERS = (
    "creative commons",
    "cc-by",
    "cc by",
    "cc0",
    "cc-0",
    "public domain",
    "public-domain",
)


def _licence_of(job_meta: dict) -> str:
    """The recorded source licence, or "" when nothing was recorded."""
    for key in (
        "licence",
        "license",
        "source_licence",
        "source_license",
        "rights",
        "source_rights",
    ):
        value = job_meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _audio_flag_of(job_meta: dict) -> str:
    """Recorded audio/music state, or "" when nothing was recorded."""
    for key in ("claimed_music", "music_claimed", "audio_claimed", "music", "audio", "audio_track"):
        value = job_meta.get(key)
        if isinstance(value, bool):
            return "claimed" if value else "original"
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def scan_clip(clip: Any, job: Any | None = None) -> dict:
    """Scan one clip candidate (plus its job, when given).

    ``clip`` needs ``id``, ``duration_s`` and ``file_path``;
    ``job`` needs ``source_type``, ``own_content`` and ``meta_json``.
    Returns ``{"clip_id", "flags", "disclaimer"}`` where each flag is
    ``{"type", "message", "severity"}`` with severity in
    ``info`` / ``warning`` / ``severe``.
    """
    flags: list[dict] = []
    meta: dict = dict(getattr(job, "meta_json", None) or {})
    own_content = bool(getattr(job, "own_content", False)) if job is not None else False
    source_type = str(getattr(job, "source_type", "") or "") if job is not None else ""

    # --- source licence ---------------------------------------------
    licence = _licence_of(meta)
    licence_lower = licence.lower()
    if licence and any(marker in licence_lower for marker in _CC_MARKERS):
        flags.append(
            {
                "type": "licence",
                "severity": "info",
                "message": (
                    f"Source licence is recorded as {licence} — check the "
                    "licence terms (attribution, commercial use) before publishing."
                ),
            }
        )
    elif own_content or (licence and any(m in licence_lower for m in _KNOWN_LICENCE_MARKERS)):
        flags.append(
            {
                "type": "licence",
                "severity": "info",
                "message": (
                    "Source is marked as your own content or a licensed source — "
                    "no licence issue recorded."
                    if own_content and not licence
                    else f"Source licence is recorded as {licence} — no licence issue recorded."
                ),
            }
        )
    else:
        flags.append(
            {
                "type": "licence",
                "severity": "warning",
                "message": (
                    "Source licence is unknown — ClipForge has no record that "
                    "you own this video or hold a licence for it. Confirm you "
                    "own it or hold a licence before publishing; do not publish "
                    "another creator's video."
                    + (
                        " This came from YouTube without an own-content declaration."
                        if source_type == "youtube" and not own_content
                        else ""
                    )
                ),
            }
        )

    # --- audio / claimed music ---------------------------------------
    audio = _audio_flag_of(meta)
    if audio in ("claimed", "third-party", "third party", "copyrighted", "claimed_music"):
        flags.append(
            {
                "type": "audio",
                "severity": "warning",
                "message": (
                    "The job recorded claimed or third-party music in this clip. "
                    "Claimed music can get the post muted, blocked, or claimed "
                    "by the rights holder — replace it with original or "
                    "licensed audio before publishing."
                ),
            }
        )
    elif audio in ("original", "none", "royalty-free", "royalty free", "licensed"):
        flags.append(
            {
                "type": "audio",
                "severity": "info",
                "message": f"Audio is recorded as {audio} — no music claim recorded.",
            }
        )
    # No audio metadata at all: nothing to say — silence, not a guess.

    # --- duration vs platform rules ----------------------------------
    duration = float(getattr(clip, "duration_s", 0.0) or 0.0)
    limits: dict[str, float] = {
        platform: float(spec["max_duration_s"])
        for platform, spec in PLATFORM_LIMITS.items()
        if spec.get("max_duration_s")
    }
    limits["x"] = X_MAX_VIDEO_S
    platform_labels = {
        "youtube": "YouTube Shorts",
        "facebook": "Facebook Reels",
        "instagram": "Instagram Reels",
        "tiktok": "TikTok",
        "x": "X video posts",
    }
    for platform, limit in sorted(limits.items()):
        if duration > limit:
            flags.append(
                {
                    "type": "duration",
                    "severity": "warning",
                    "message": (
                        f"Clip is {duration:.0f}s — too long for "
                        f"{platform_labels.get(platform, platform)} "
                        f"({limit:.0f}s max). Trim it or publish it elsewhere."
                    ),
                }
            )

    # --- clip file ----------------------------------------------------
    file_path = getattr(clip, "file_path", None)
    if not file_path:
        flags.append(
            {
                "type": "file",
                "severity": "warning",
                "message": (
                    "No rendered clip file is recorded for this clip — "
                    "publish it only after the clip has rendered."
                ),
            }
        )
    elif not Path(str(file_path)).is_file():
        flags.append(
            {
                "type": "file",
                "severity": "warning",
                "message": (
                    f"Clip video file is missing ({file_path}) — "
                    "re-render the clip before publishing."
                ),
            }
        )

    if not flags:
        flags.append(
            {
                "type": "general",
                "severity": "info",
                "message": "No issues found in the clip metadata.",
            }
        )
    return {
        "clip_id": getattr(clip, "id", ""),
        "flags": flags,
        "disclaimer": NO_GUARANTEE_DISCLAIMER,
    }


__all__ = ["NO_GUARANTEE_DISCLAIMER", "scan_clip"]
