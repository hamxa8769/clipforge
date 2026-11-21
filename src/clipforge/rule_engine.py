"""Rule-based fallback engine for ClipForge Agent (v0.5.0).

Runs with ZERO keys configured. Everything here is deterministic:
transcript keyword extraction, niche keyword lists, template
titles / descriptions / hashtags per platform, and the best-practice
defaults (15–60 s clips, hook in the first 3 s, captions on).

Every function that produces user-facing copy returns an ``engine``
field — ``"rules"`` here, ``"gemini"`` / ``"openai"`` from the LLM
providers in :mod:`clipforge.llm` — so the UI can always say plainly
which engine wrote the text.
"""

from __future__ import annotations

import re
from collections import Counter

from clipforge.niches import (
    CLIP_DEFAULTS,
    niche_description_templates,
    niche_hashtags,
    niche_keywords,
    niche_title_templates,
    normalize_niche,
)
from clipforge.seo import PLATFORMS, generate_seo

ENGINE_NAME = "rules"

_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "so",
    "if",
    "then",
    "when",
    "while",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "with",
    "from",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "i",
    "you",
    "he",
    "she",
    "we",
    "they",
    "my",
    "your",
    "his",
    "her",
    "our",
    "their",
    "me",
    "him",
    "us",
    "them",
    "do",
    "does",
    "did",
    "have",
    "has",
    "had",
    "can",
    "could",
    "will",
    "would",
    "should",
    "just",
    "very",
    "really",
    "not",
    "no",
    "yes",
    "all",
    "any",
    "some",
    "more",
    "most",
    "other",
    "there",
    "here",
    "what",
    "why",
    "how",
    "who",
    "which",
    "about",
    "into",
    "over",
    "under",
    "up",
    "down",
    "out",
    "off",
    "again",
    "once",
}
_WORD_RE = re.compile(r"[a-z][a-z\-']*")


def extract_keywords(text: str, niche: str = "", top_n: int = 5) -> list[str]:
    """Top keywords from a transcript: frequent content words first.

    Niche keywords are boosted to the front (the clip is *about* the
    niche), then the most frequent remaining words. Pure function.
    """
    tokens = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]
    if not tokens:
        return []
    niche_words = [w for w in niche_keywords(niche) if w in set(tokens)]
    counts = Counter(tokens)
    ranked = [w for w, _ in counts.most_common() if w not in niche_words]
    out: list[str] = []
    for word in niche_words + ranked:
        if word not in out:
            out.append(word)
        if len(out) >= top_n:
            break
    return out


def _fill(template: str, keyword: str, hook: str) -> str:
    return template.replace("{keyword}", keyword).replace("{hook}", hook)


def generate_copy(
    keyword: str,
    hook: str,
    niche: str = "",
    platform: str = "youtube",
    duration_s: float = 30.0,
) -> dict:
    """One platform's SEO copy from rules only. ``engine`` is "rules".

    The platform template from :mod:`clipforge.seo` (length caps,
    hashtag caps, keyword placement) is the base; the niche title /
    description templates then shape the wording, so the copy reads
    like the niche instead of a generic clip.
    """
    niche = normalize_niche(niche)
    keyword = (keyword or "clip").strip()
    hook = (hook or keyword).strip()
    title_hint = _fill(niche_title_templates(niche)[0], keyword, hook)
    variant = generate_seo(
        platform,
        keyword,
        hook,
        title_hint=title_hint,
        duration_s=duration_s,
        extra_hashtags=niche_hashtags(niche),
    )
    # Niche description template replaces the generic description when
    # the platform copy would otherwise be a bare "keyword — hook".
    desc_template = niche_description_templates(niche)[0]
    niche_desc = _fill(desc_template, keyword, hook)
    if platform == "youtube":
        tags = " ".join(f"#{t}" for t in variant["hashtags"])
        variant["description"] = f"{niche_desc}\n\n{tags}"
    variant["engine"] = ENGINE_NAME
    variant["niche"] = niche
    variant["defaults"] = dict(CLIP_DEFAULTS)
    return variant


def generate_all_platforms(
    keyword: str,
    hook: str,
    niche: str = "",
    duration_s: float = 30.0,
) -> dict[str, dict]:
    """Rule-based SEO copy for all four platforms."""
    return {
        platform: generate_copy(keyword, hook, niche, platform, duration_s)
        for platform in PLATFORMS
    }


def summarize_transcript(text: str, niche: str = "", max_sentences: int = 2) -> dict:
    """Plain summary of a transcript: keywords + opening hook line.

    Used by the agent to show what a source video is about before any
    clip is made — no model needed.
    """
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    keywords = extract_keywords(text, niche)
    return {
        "keywords": keywords,
        "hook_line": sentences[0][:120] if sentences else "",
        "sentence_count": len(sentences),
        "engine": ENGINE_NAME,
    }


__all__ = [
    "ENGINE_NAME",
    "extract_keywords",
    "generate_all_platforms",
    "generate_copy",
    "summarize_transcript",
]
