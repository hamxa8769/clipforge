"""Per-platform SEO generator (research report §4).

Rule: NEVER one caption copied four ways. Each platform gets its own
title / description / caption / hashtag set, built by deterministic
templates from (keyword, hook, title hint). An optional LLM polish step
runs only when ``CLIPFORGE_LLM_API_KEY`` is set — and only rewrites copy,
never the platform constraints (hashtag counts, length caps).

Platform rules encoded here (report §4):
- YouTube Shorts: title <=100 chars, aim 55-60, keyword front-loaded;
  keyword in first 200 chars of description; 2-3 hashtags; <=3 min.
- TikTok: keyword in first line of caption; 2-5 hashtags; >=60s variant
  flagged for Creator Rewards (short clips earn $0 there).
- Instagram Reels: keyword-rich caption, hook in first 55-70 chars,
  MAX 5 hashtags (Dec 2025 cap, down from 30).
- Facebook Reels: short plain caption, 2-3 niche hashtags, <=90s (API cap).

Every generator returns ``warnings`` from :func:`validate` — callers
should surface them in the review UI, not silently publish past them.
"""

from __future__ import annotations

import os
import re

from clipforge.publishing.limits import PLATFORM_LIMITS

YOUTUBE = "youtube"
INSTAGRAM = "instagram"
FACEBOOK = "facebook"
TIKTOK = "tiktok"
PLATFORMS = (YOUTUBE, INSTAGRAM, FACEBOOK, TIKTOK)

# Deterministic hashtag pools per platform, keyed loosely by keyword.
_GENERIC_HASHTAGS = {
    YOUTUBE: ["shorts", "viralshorts"],
    TIKTOK: ["fyp", "viral"],
    INSTAGRAM: ["reels", "reelsinstagram"],
    FACEBOOK: ["reels", "fbreels"],
}


def _slug_hashtag(word: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "", word.lower())
    return clean or "clip"


def _hashtags(platform: str, keyword: str, extra: list[str] | None = None) -> list[str]:
    tags = [_slug_hashtag(keyword)]
    for tag in extra or []:
        tag = _slug_hashtag(tag)
        if tag and tag not in tags:
            tags.append(tag)
    for tag in _GENERIC_HASHTAGS.get(platform, []):
        if tag not in tags:
            tags.append(tag)
    caps = {"youtube": 3, "tiktok": 5, "instagram": 5, "facebook": 3}
    return tags[: caps[platform]]


def _truncate(text: str, max_len: int) -> str:
    """Truncate at a word boundary; return the text unchanged if it fits."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" :,-")


def _front_load(keyword: str, text: str, max_len: int) -> str:
    """Put the keyword first, then the hook text, truncated at a word boundary.

    If the text already starts with the keyword (e.g. a niche template
    produced "gameplay: the clutch moment..."), do not prepend it again —
    "gameplay: gameplay: ..." is a real bug seen in v0.6.0 trending runs.
    """
    text = text.strip()
    keyword = keyword.strip()
    if not text:
        return keyword
    lowered = text.lower()
    key_lower = keyword.lower()
    if (
        lowered == key_lower
        or lowered.startswith(key_lower + ":")
        or lowered.startswith(key_lower + " —")
        or lowered.startswith(key_lower + " -")
    ):
        base = text
    else:
        base = f"{keyword}: {text}"
    return _truncate(base, max_len)


def _title_tiktok(hook: str, title_hint: str) -> str:
    """TikTok title = the hook itself, caption-style (no keyword prefix)."""
    return _truncate(title_hint.strip() or hook.strip(), 60)


def _title_instagram(keyword: str, hook: str) -> str:
    """Instagram title = hook first, keyword as a trailing tag."""
    return _truncate(f"{hook.strip()} | {keyword.strip()}", 68)


def _title_facebook(keyword: str, hook: str) -> str:
    """Facebook title = short plain 'keyword — hook' (no colon prefix)."""
    return _truncate(f"{keyword.strip()} — {hook.strip()}", 55)


def generate_youtube(
    keyword: str, hook: str, title_hint: str = "", duration_s: float = 30.0
) -> dict:
    title = _front_load(keyword, title_hint or hook, 60)
    if len(title) > 100:  # hard cap, never exceed
        title = title[:97].rsplit(" ", 1)[0] + "..."
    hashtags = _hashtags(YOUTUBE, keyword)
    desc_lines = [
        f"{keyword.strip()} — {hook.strip()}",
        "",
        title_hint.strip() if title_hint.strip() else hook.strip(),
        "",
        " ".join(f"#{t}" for t in hashtags),
    ]
    return {
        "platform": YOUTUBE,
        "title": title,
        "description": "\n".join(desc_lines).strip(),
        "caption": title,
        "hashtags": hashtags,
        "duration_s": duration_s,
    }


def generate_tiktok(
    keyword: str, hook: str, title_hint: str = "", duration_s: float = 30.0
) -> dict:
    caption = f"{keyword.strip()}\n{hook.strip()}"
    hashtags = _hashtags(TIKTOK, keyword)
    caption = caption + "\n" + " ".join(f"#{t}" for t in hashtags)
    return {
        "platform": TIKTOK,
        "title": _title_tiktok(hook, title_hint),
        "description": caption,
        "caption": caption,
        "hashtags": hashtags,
        "duration_s": duration_s,
        "rewards_eligible_variant": duration_s >= 60,
        # >=60s required for TikTok Creator Rewards; a shorter clip earns $0 there.
    }


def generate_instagram(
    keyword: str, hook: str, title_hint: str = "", duration_s: float = 30.0
) -> dict:
    # Hook must land in the first 55-70 chars of the caption.
    hook_text = _front_load(keyword, hook, 68)
    hashtags = _hashtags(INSTAGRAM, keyword)
    caption = (
        hook_text + "\n\n" + (title_hint.strip() or "") + "\n" + " ".join(f"#{t}" for t in hashtags)
    )
    return {
        "platform": INSTAGRAM,
        "title": _title_instagram(keyword, hook),
        "description": caption.strip(),
        "caption": caption.strip(),
        "hashtags": hashtags,
        "duration_s": duration_s,
    }


def generate_facebook(
    keyword: str, hook: str, title_hint: str = "", duration_s: float = 30.0
) -> dict:
    caption = f"{keyword.strip()} — {hook.strip()}"
    hashtags = _hashtags(FACEBOOK, keyword)
    caption = caption + " " + " ".join(f"#{t}" for t in hashtags)
    return {
        "platform": FACEBOOK,
        "title": _title_facebook(keyword, hook),
        "description": caption,
        "caption": caption,
        "hashtags": hashtags,
        "duration_s": duration_s,
    }


_GENERATORS = {
    YOUTUBE: generate_youtube,
    TIKTOK: generate_tiktok,
    INSTAGRAM: generate_instagram,
    FACEBOOK: generate_facebook,
}


def generate_seo(
    platform: str,
    keyword: str,
    hook: str,
    title_hint: str = "",
    duration_s: float = 30.0,
    extra_hashtags: list[str] | None = None,
    llm_polish: bool = False,
) -> dict:
    """Generate one platform's SEO variant + validation warnings.

    ``llm_polish`` only takes effect when ``CLIPFORGE_LLM_API_KEY`` is set;
    without it the deterministic template is returned unchanged. The LLM
    path is a stub in v0.1.0 — it must never be wired to send video/audio,
    only the keyword/hook text, and only on explicit opt-in.
    """
    platform = platform.lower()
    if platform not in _GENERATORS:
        raise ValueError(f"unknown platform {platform!r}; expected one of {PLATFORMS}")
    if not keyword.strip():
        raise ValueError("keyword is required for SEO generation")
    variant = _GENERATORS[platform](keyword, hook, title_hint, duration_s)
    if extra_hashtags:
        merged = _hashtags(platform, keyword, extra_hashtags)
        variant["hashtags"] = merged
        for key in ("description", "caption"):
            if key in variant and variant[key]:
                variant[key] = re.sub(r"( #[a-z0-9]+)+$", "", variant[key]).rstrip()
                variant[key] = variant[key] + "\n" + " ".join(f"#{t}" for t in merged)
    if llm_polish and os.environ.get("CLIPFORGE_LLM_API_KEY"):
        variant["warnings_llm"] = (
            "LLM polish requested but not wired in v0.1.0 — deterministic "
            "template returned unchanged. Wire an LLM call here only with "
            "text (keyword/hook), never video/audio."
        )
    variant["warnings"] = validate(platform, variant)
    return variant


def generate_all(
    keyword: str, hook: str, title_hint: str = "", duration_s: float = 30.0
) -> dict[str, dict]:
    """All four platform variants from one master clip (each different)."""
    return {p: generate_seo(p, keyword, hook, title_hint, duration_s) for p in PLATFORMS}


def validate(platform: str, variant: dict) -> list[str]:
    """Validate one SEO variant against the platform spec. Returns warnings."""
    warnings: list[str] = []
    limits = PLATFORM_LIMITS[platform]
    duration = float(variant.get("duration_s") or 0)
    if duration > limits["max_duration_s"]:
        warnings.append(
            f"{platform}: clip is {duration:.0f}s but the API max is "
            f"{limits['max_duration_s']}s — trim or use a shorter variant."
        )
    if duration < limits.get("min_duration_s", 0):
        warnings.append(f"{platform}: clip is under the {limits['min_duration_s']}s minimum.")
    hashtags = variant.get("hashtags") or []
    if len(hashtags) > limits["max_hashtags"]:
        warnings.append(
            f"{platform}: {len(hashtags)} hashtags exceeds the cap of {limits['max_hashtags']}."
        )
    if platform == YOUTUBE:
        title = variant.get("title") or ""
        if len(title) > 100:
            warnings.append(f"youtube: title is {len(title)} chars, max 100.")
        elif len(title) > 60:
            warnings.append("youtube: title over the 55-60 char aim — keyword may be buried.")
        desc = variant.get("description") or ""
        keyword = (variant.get("title") or "").split(":")[0].strip().lower()
        if keyword and keyword not in desc[:200].lower():
            warnings.append("youtube: keyword missing from the first 200 chars of the description.")
    if platform == TIKTOK and duration < 60:
        warnings.append(
            "tiktok: clip under 60s is NOT eligible for Creator Rewards "
            "(>=60s required) — reach variant only; consider a >=60s cut for pay."
        )
    if platform == INSTAGRAM:
        caption = variant.get("caption") or ""
        if len(caption) > 70 and ":" not in caption[:70]:
            warnings.append("instagram: hook may not land in the first 55-70 chars.")
    if platform == FACEBOOK and duration > 90:
        warnings.append("facebook: Reels Publishing API caps at 90s (in-app Reels do not).")
    return warnings


__all__ = [
    "generate_seo",
    "generate_all",
    "generate_youtube",
    "generate_tiktok",
    "generate_instagram",
    "generate_facebook",
    "validate",
    "PLATFORMS",
]
