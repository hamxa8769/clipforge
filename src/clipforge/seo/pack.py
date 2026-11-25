"""SEO pack per clip (v0.11.0, Worker B) — ClipNova-style, simple.

ClipNova's "AI Viral SEO Engine" (``studio/brain.py`` + ``studio/hashtags.py``
in https://github.com/ahsanK225/ClipNova-AI, MIT) gives every clip one
ready-to-post pack: a hook title, a description, hashtags, and — the part
ClipForge was missing — plain YouTube search tags (``seo_tags``: 12–20
keyword phrases without ``#``). ClipNova writes those with an LLM and
falls back to a deterministic generator (``fallback_seo``) when the LLM
is offline. ClipForge does the same job with its own deterministic
templates only — no LLM call, no key needed — so the pack is identical
every run and works offline.

What a pack is (one per clip, all five platforms):

* ``title_options`` — 2–3 titles to pick from, never one take-it-or-leave-it
  title. Option 1 is the existing per-platform title; the others are the
  hook on its own and the keyword + hook in the platform's own shape.
* ``description`` — the existing per-platform description, plus the first
  line of what was actually said in the clip when a transcript is given.
* ``tags`` — plain search keywords (no ``#``), from the keyword, the
  channel name, and the words said most in the clip — ClipNova's
  ``hashtags.keywords`` idea, applied to search tags instead of hashtags.
* ``hashtags`` — the existing per-platform hashtags (caps unchanged).
* ``warnings`` / ``tip`` — the existing validation warnings plus one
  plain-language tip per platform.

This module only *builds* packs. It never touches the database; the API
router (``clipforge_api.seo_pack``) loads the clip and overlays the stored
SEO variants where the worker already wrote them.
"""

from __future__ import annotations

import re

from clipforge.seo import generate_all as _generate_all

PACK_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook", "x")

PLATFORM_LABELS = {
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "facebook": "Facebook Reels",
    "x": "X",
}

# Aim caps for title *options* (same caps the per-platform generators use;
# YouTube's hard cap of 100 is enforced by the generator itself).
_TITLE_CAPS = {"youtube": 60, "tiktok": 60, "instagram": 68, "facebook": 55, "x": 70}

# How many plain search tags each platform gets. YouTube is the only one
# with a real tags field (up to 500 chars); the others use the list as
# extra keywords to paste into the caption / hashtags.
_TAG_LIMITS = {"youtube": 15, "tiktok": 10, "instagram": 10, "facebook": 8, "x": 5}

# Plain-language tip per platform, shown next to the pack (no jargon).
_TIPS = {
    "youtube": "Put the main word first in the title — YouTube reads the first words most.",
    "tiktok": "Say the main word in the first line — TikTok search reads the caption.",
    "instagram": "The first line of the caption is what people see before they tap “more”.",
    "facebook": "Short and plain wins here — one idea, two or three hashtags.",
    "x": "Keep the post under 280 characters — the hashtags count towards it.",
}

# Words that carry no meaning as a search tag. A short own list (common
# English filler + the clip/stream words ClipNova also filters); the
# transcript-keyword scorer below does the real work.
_STOPWORDS = set(
    """about above after again against all also always among and another any
    anyone anything are around because been before being between both but came
    can cannot come could did does doing done down each else even ever every
    everyone everything find first for from gave get gets getting give going
    gone gonna got great had has have having here him his how into its just
    keep kind know last left let like little look looking made make makes many
    may maybe mean more most much must myself need never next nobody not
    nothing now off okay once one only onto other our out over own people put
    really right said same say says see she should since some someone
    something still such sure take than that the their them then there these
    they thing things think this those though through time too took try under
    until upon use very want was way we well went were what when where whether
    which while who whole why will with without would yeah yes you your
    video clip stream chat watch watching guys literally actually basically
    honestly really just""".split()
)

_CHANNEL_NOISE = {
    "live",
    "official",
    "tv",
    "gaming",
    "clips",
    "channel",
    "highlights",
    "youtube",
    "twitch",
    "kick",
    "tiktok",
    "stream",
    "shorts",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9']{2,19}")
_NUMBERED_RE = re.compile(r"\b([A-Za-z]{2,12})\s+(\d{1,2})\b")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _truncate(text: str, max_len: int) -> str:
    text = _clean(text)
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" :,-")


def _tag_word(word: str) -> str:
    """One single-word tag: lowercase letters/digits, 3–30 chars, no filler."""
    tag = re.sub(r"[^a-z0-9]", "", word.lower())[:30]
    if len(tag) < 3 or tag.isdigit() or tag in _STOPWORDS:
        return ""
    return tag


def _numbered_tags(*texts: str) -> list[str]:
    """Names with a number in them (“GTA 6”) as one tag each."""
    out: list[str] = []
    for text in texts:
        for name, num in _NUMBERED_RE.findall(text or ""):
            tag = _tag_word(name + num)
            if tag and tag not in out:
                out.append(tag)
    return out


def _transcript_keywords(text: str, title: str, count: int) -> list[str]:
    """Words said most in the clip — ClipNova's ``hashtags.keywords`` idea.

    A word scores for being said more than once, for being a name
    (capitalised mid-sentence), and for also appearing in the title.
    """
    raw = (text or "").split()
    in_title = {w.lower() for w in _WORD_RE.findall(title or "")}
    score: dict[str, int] = {}
    for i, token in enumerate(raw):
        match = _WORD_RE.search(token)
        if not match:
            continue
        word = match.group(0).strip("'").lower()
        if len(word) < 4 or not _tag_word(word):
            continue
        is_name = token[:1].isupper() and i > 0 and raw[i - 1][-1:] not in ".?!"
        score[word] = (
            score.get(word, 0) + 1 + (2 if is_name else 0) + (2 if word in in_title else 0)
        )
    best = sorted(
        (w for w in score if score[w] >= 2),
        key=lambda w: (-score[w], -len(w), w),
    )
    return best[:count]


def extract_search_tags(
    keyword: str,
    hook: str,
    transcript: str = "",
    source_title: str = "",
    source_channel: str = "",
    limit: int = 15,
) -> list[str]:
    """Plain search tags for one clip (no ``#`` — these are YouTube tags).

    Order is most useful first: the keyword itself, the channel name,
    names-with-numbers from the titles, then the words said most in the
    clip. Generic filler (“shorts”, “clips”) only fills the list when
    fewer than 3 real tags were found.
    """
    tags: list[str] = []

    def add(tag: str) -> None:
        tag = _clean(tag).lower()
        if tag and tag not in tags and len(tags) < limit:
            tags.append(tag)

    keyword = _clean(keyword)
    if keyword:
        add(keyword)
    channel_words = [
        w
        for w in re.findall(r"[A-Za-z0-9]+", source_channel or "")
        if w.lower() not in _CHANNEL_NOISE
    ]
    if channel_words:
        add(" ".join(channel_words).lower())
    for tag in _numbered_tags(hook, source_title, transcript):
        add(tag)
    # Single-word tags from the hook / titles / transcript, most said first.
    combined = " ".join(t for t in (hook, source_title, transcript) if t)
    for word in _transcript_keywords(combined, source_title or hook, limit):
        add(word)
    # The keyword's own words are also useful as single tags (“morning
    # routine” -> “morning”, “routine”) when there is room.
    for word in _WORD_RE.findall(keyword):
        add(_tag_word(word))
    for filler in ("shorts", "clips"):
        if len(tags) < 3:
            add(filler)
    return tags


def _title_options(
    platform: str,
    keyword: str,
    hook: str,
    title_hint: str,
    base_title: str,
    transcript: str,
) -> list[str]:
    """2–3 distinct title options for one platform, within its cap.

    Option 1 is always the generator's title (the safe default). The
    others come from the hook on its own, the keyword + hook in the
    platform's shape, and — when the transcript offers one — the first
    thing actually said in the clip. Duplicates are dropped, so a clip
    with a one-word hook honestly returns 2 options instead of 3 padded
    copies.
    """
    cap = _TITLE_CAPS[platform]
    keyword = _clean(keyword)
    hook = _clean(hook)
    hint = _clean(title_hint) or hook
    candidates: list[str] = [base_title]
    if platform == "youtube":
        candidates += [hook, f"{keyword} — {hook}", hint]
    elif platform == "tiktok":
        candidates += [hint, hook, f"{keyword}: {hook}"]
    elif platform == "instagram":
        candidates += [hook, f"{hook} | {keyword}", hint]
    elif platform == "facebook":
        candidates += [hook, f"{keyword} — {hook}", hint]
    else:  # x: post-text options
        candidates += [hint, f"{keyword}: {hook}", hook]
    first_sentence = _clean((transcript or "").split(".")[0])
    if first_sentence and len(first_sentence) > 12:
        candidates.append(first_sentence)
    options: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        title = _truncate(candidate, cap)
        key = title.lower()
        if title and key not in seen:
            seen.add(key)
            options.append(title)
        if len(options) == 3:
            break
    return options


def _generate_x(
    keyword: str, hook: str, title_hint: str, duration_s: float, hashtags: list[str]
) -> dict:
    """X (Twitter) variant — the SEO module covers four platforms, X is new.

    A post is one short text: hook first, keyword in it, 2–3 hashtags,
    280 characters total. No separate title/description on X, so the
    caption *is* the post. ``hashtags`` comes from the Facebook variant
    (same 2–3 niche-tag cap X needs) so the tag rules stay in one place.
    """
    post = _truncate(f"{_clean(hook) or _clean(title_hint) or keyword}", 200)
    caption = post + " " + " ".join(f"#{t}" for t in hashtags)
    caption = _truncate(caption, 280)
    return {
        "platform": "x",
        "title": _truncate(hint_title(keyword, hook, title_hint), _TITLE_CAPS["x"]),
        "description": caption,
        "caption": caption,
        "hashtags": hashtags,
        "duration_s": duration_s,
    }


def hint_title(keyword: str, hook: str, title_hint: str) -> str:
    """Best short post opener for X: the hint, else the hook, else keyword."""
    return _clean(title_hint) or _clean(hook) or _clean(keyword)


def _copy_all_text(keyword: str, platforms: dict[str, dict]) -> str:
    """The whole pack as plain text — one paste for the clipboard."""
    lines = [f"SEO pack — {keyword}", ""]
    for platform in PACK_PLATFORMS:
        variant = platforms[platform]
        lines.append(f"{PLATFORM_LABELS[platform]}")
        lines.append(f"Title: {variant['title']}")
        if variant["title_options"][1:]:
            for i, option in enumerate(variant["title_options"][1:], start=2):
                lines.append(f"Title option {i}: {option}")
        lines.append(f"Description: {variant['description']}")
        if variant["tags"]:
            lines.append(f"Tags: {', '.join(variant['tags'])}")
        if variant["hashtags"]:
            lines.append(f"Hashtags: {' '.join('#' + t for t in variant['hashtags'])}")
        lines.append("")
    return "\n".join(lines).strip()


def build_seo_pack(
    keyword: str,
    hook: str,
    title_hint: str = "",
    duration_s: float = 30.0,
    transcript: str = "",
    source_title: str = "",
    source_channel: str = "",
    attribution_text: str | None = None,
    stored: dict[str, dict] | None = None,
) -> dict:
    """Build the full SEO pack for one clip (all five platforms).

    ``keyword`` is required (the worker uses the job title's first part).
    ``stored`` overlays already-generated variants (the worker's SEO stage)
    — their title/description win over the freshly generated ones, so the
    pack never contradicts what the Review page already showed.
    """
    keyword = _clean(keyword)
    if not keyword:
        raise ValueError("keyword is required for an SEO pack")
    # No hook at all (a clip with no transcript line): the keyword is the
    # hook — an empty title helps nobody.
    hook = _clean(hook) or keyword
    title_hint = _clean(title_hint) or hook
    transcript = _clean(transcript)
    variants = _generate_all(
        keyword, hook, title_hint, duration_s, attribution_text=attribution_text
    )
    variants["x"] = _generate_x(
        keyword, hook, title_hint, duration_s, list(variants["facebook"]["hashtags"])
    )
    stored = stored or {}

    platforms: dict[str, dict] = {}
    for platform in PACK_PLATFORMS:
        variant = dict(variants[platform])
        overlay = stored.get(platform) or {}
        if overlay.get("title"):
            variant["title"] = overlay["title"]
        if overlay.get("description"):
            variant["description"] = overlay["description"]
        if overlay.get("hashtags"):
            variant["hashtags"] = list(overlay["hashtags"])
        # Description: add what was actually said, when we know it and it
        # is not already in there — a real quote beats a template line.
        if transcript and transcript[:80].lower() not in variant["description"].lower():
            quote = _truncate(transcript, 200)
            variant["description"] = (
                variant["description"].rstrip() + f"\n\nWhat was said: “{quote}”"
            ).strip()
        tags = extract_search_tags(
            keyword,
            hook,
            transcript=transcript,
            source_title=source_title,
            source_channel=source_channel,
            limit=_TAG_LIMITS[platform],
        )
        # X has no tags field — its tags are the hashtags; keep the list
        # anyway so the UI can offer them as extra words.
        platforms[platform] = {
            "platform": platform,
            "label": PLATFORM_LABELS[platform],
            "title": variant["title"],
            "title_options": _title_options(
                platform, keyword, hook, title_hint, variant["title"], transcript
            ),
            "description": variant["description"],
            "caption": variant.get("caption") or variant["description"],
            "tags": tags,
            "hashtags": list(variant["hashtags"]),
            "warnings": list(variant.get("warnings") or []),
            "tip": _TIPS[platform],
        }
    return {
        "keyword": keyword,
        "platforms": platforms,
        "copy_all": _copy_all_text(keyword, platforms),
    }


__all__ = [
    "PACK_PLATFORMS",
    "PLATFORM_LABELS",
    "build_seo_pack",
    "extract_search_tags",
]
