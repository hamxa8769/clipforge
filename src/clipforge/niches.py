"""Niche presets for ClipForge Agent (v0.5.0).

One niche per agent / channel. A niche is a preset (gaming, tech,
vlogging, education, finance, fitness, cooking, music) or free text —
anything else is treated as a custom niche whose keywords are its own
words. The scorer, the rule engine and the LLM prompts all read the
same lists here, so a niche means the same thing everywhere.

Best-practice defaults the agent applies to every niche (report §4):
clips 15–60 s (30 s target), the hook lands in the first 3 seconds,
captions are on, and SEO copy is written for the niche, not generic.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- presets
# Each preset: keywords the scorer listens for, hashtags the SEO writer
# uses, and title/description templates the rule engine fills in.
# Templates use {keyword} (the clip's topic) and {hook} (its opening line).
NICHES: dict[str, dict] = {
    "gaming": {
        "label": "Gaming",
        "keywords": [
            "game",
            "gameplay",
            "gaming",
            "win",
            "ranked",
            "clutch",
            "boss",
            "level",
            "pro",
            "squad",
            "kill",
            "headshot",
            "loot",
            "build",
            "strategy",
            "tournament",
            "speedrun",
            "noob",
            "op",
            "gg",
        ],
        "hashtags": ["gaming", "gamer", "gameplay", "clips"],
        "title_templates": [
            "{keyword}: the clutch moment nobody expected",
            "This {keyword} play broke the game",
            "{keyword} — watch this insane clutch",
        ],
        "description_templates": [
            "{hook} Full {keyword} gameplay moment, clipped for you.",
            "If you play {keyword}, you need to see this: {hook}",
        ],
    },
    "tech": {
        "label": "Tech",
        "keywords": [
            "tech",
            "ai",
            "app",
            "software",
            "code",
            "coding",
            "phone",
            "review",
            "gadget",
            "setup",
            "tutorial",
            "how",
            "update",
            "feature",
            "tool",
            "hack",
            "device",
            "laptop",
            "smartphone",
        ],
        "hashtags": ["tech", "techtok", "ai", "gadgets"],
        "title_templates": [
            "{keyword}: the feature nobody talks about",
            "Stop using {keyword} wrong — do this instead",
            "{keyword} explained in 30 seconds",
        ],
        "description_templates": [
            "{hook} A quick {keyword} breakdown — no fluff, just what matters.",
            "Everything you need to know about {keyword}: {hook}",
        ],
    },
    "vlogging": {
        "label": "Vlogging",
        "keywords": [
            "day",
            "life",
            "vlog",
            "morning",
            "routine",
            "travel",
            "trip",
            "story",
            "today",
            "weekend",
            "family",
            "friends",
            "behind",
            "scenes",
            "real",
            "honest",
            "journey",
            "adventure",
            "city",
        ],
        "hashtags": ["vlog", "dailylife", "lifestyle", "reallife"],
        "title_templates": [
            "{keyword}: the part I almost didn't share",
            "A day in my life — {keyword}",
            "{keyword} got real today",
        ],
        "description_templates": [
            "{hook} Come along for this {keyword} moment from my day.",
            "Real life, unfiltered: {hook} ({keyword})",
        ],
    },
    "education": {
        "label": "Education",
        "keywords": [
            "learn",
            "lesson",
            "explain",
            "explained",
            "how",
            "why",
            "what",
            "study",
            "tip",
            "tips",
            "mistake",
            "fact",
            "facts",
            "science",
            "history",
            "math",
            "language",
            "school",
            "exam",
            "beginner",
        ],
        "hashtags": ["learn", "education", "edutok", "facts"],
        "title_templates": [
            "{keyword} explained so simply anyone gets it",
            "The #1 {keyword} mistake beginners make",
            "Learn {keyword} in 30 seconds",
        ],
        "description_templates": [
            "{hook} A quick {keyword} lesson — save this for later.",
            "Did you know this about {keyword}? {hook}",
        ],
    },
    "finance": {
        "label": "Finance",
        "keywords": [
            "money",
            "invest",
            "investing",
            "stock",
            "stocks",
            "crypto",
            "budget",
            "save",
            "saving",
            "income",
            "passive",
            "wealth",
            "finance",
            "financial",
            "bank",
            "tax",
            "profit",
            "market",
            "trading",
            "gold",
        ],
        "hashtags": ["finance", "money", "investing", "moneytips"],
        "title_templates": [
            "{keyword}: the money mistake costing you every month",
            "How {keyword} actually works (no jargon)",
            "Stop losing money on {keyword} — do this",
        ],
        "description_templates": [
            "{hook} A straight {keyword} tip — not financial advice, just what I learned.",
            "Your money, explained: {hook} ({keyword})",
        ],
    },
    "fitness": {
        "label": "Fitness",
        "keywords": [
            "workout",
            "gym",
            "fitness",
            "exercise",
            "muscle",
            "weight",
            "diet",
            "protein",
            "cardio",
            "run",
            "running",
            "health",
            "strong",
            "training",
            "routine",
            "form",
            "reps",
            "sets",
            "fat",
            "loss",
        ],
        "hashtags": ["fitness", "gym", "workout", "fitlife"],
        "title_templates": [
            "{keyword}: do it like this, not like that",
            "The {keyword} form check you needed",
            "30 seconds that will fix your {keyword}",
        ],
        "description_templates": [
            "{hook} A quick {keyword} tip for your next workout.",
            "Train smarter: {hook} ({keyword})",
        ],
    },
    "cooking": {
        "label": "Cooking",
        "keywords": [
            "recipe",
            "cook",
            "cooking",
            "food",
            "taste",
            "delicious",
            "kitchen",
            "easy",
            "quick",
            "ingredient",
            "bake",
            "baking",
            "chef",
            "meal",
            "dinner",
            "snack",
            "spice",
            "flavour",
            "flavor",
            "homemade",
        ],
        "hashtags": ["cooking", "recipe", "foodtok", "homemade"],
        "title_templates": [
            "{keyword} — the 30-second recipe you will actually make",
            "This {keyword} trick changes everything",
            "Stop cooking {keyword} wrong",
        ],
        "description_templates": [
            "{hook} A quick {keyword} recipe — full steps in the caption.",
            "Made this {keyword} in minutes: {hook}",
        ],
    },
    "music": {
        "label": "Music",
        "keywords": [
            "music",
            "song",
            "beat",
            "melody",
            "sing",
            "singing",
            "guitar",
            "piano",
            "cover",
            "remix",
            "lyrics",
            "album",
            "artist",
            "concert",
            "sound",
            "vocal",
            "chorus",
            "hook",
            "track",
        ],
        "hashtags": ["music", "musictok", "cover", "singer"],
        "title_templates": [
            "{keyword}: listen to this chorus",
            "This {keyword} cover gave me goosebumps",
            "{keyword} — the hook is unreal",
        ],
        "description_templates": [
            "{hook} A {keyword} moment worth your 30 seconds.",
            "Turn the sound up for this {keyword}: {hook}",
        ],
    },
}

# Fallback niche when none is set: plain clips, no keyword weighting.
GENERAL_NICHE = "general"
PRESET_NAMES = tuple(NICHES)
_WORD_RE = re.compile(r"[a-z0-9]+")

# Best-practice defaults applied to every niche (report §4).
CLIP_DEFAULTS = {
    "target_duration_s": 30.0,
    "min_duration_s": 15.0,
    "max_duration_s": 60.0,
    "hook_window_s": 3.0,  # the hook must land in the first 3 seconds
    "captions": True,
}


def normalize_niche(value: str | None) -> str:
    """Canonical niche name: a preset name, a free-text slug, or "general".

    Free text is lowercased and slugified ("Street Food" -> "street-food");
    anything empty becomes "general". A custom niche keeps its own words
    as keywords (see :func:`niche_keywords`).
    """
    if not value or not str(value).strip():
        return GENERAL_NICHE
    slug = "-".join(_WORD_RE.findall(str(value).lower()))
    return slug or GENERAL_NICHE


def is_preset(niche: str) -> bool:
    return normalize_niche(niche) in NICHES


def niche_label(niche: str) -> str:
    """Display label: the preset label, or the free-text niche itself."""
    name = normalize_niche(niche)
    preset = NICHES.get(name)
    return preset["label"] if preset else name.replace("-", " ").title()


def niche_keywords(niche: str) -> list[str]:
    """Keywords the scorer weights for this niche.

    Presets use their curated list; a custom niche uses its own words
    (split on "-"); "general" has no keywords (no weighting).
    """
    name = normalize_niche(niche)
    preset = NICHES.get(name)
    if preset:
        return list(preset["keywords"])
    if name == GENERAL_NICHE:
        return []
    return [w for w in name.split("-") if w]


def niche_hashtags(niche: str) -> list[str]:
    """Hashtag pool for this niche (preset list, else the niche words)."""
    name = normalize_niche(niche)
    preset = NICHES.get(name)
    if preset:
        return list(preset["hashtags"])
    if name == GENERAL_NICHE:
        return []
    return [w for w in name.split("-") if w][:3]


def niche_title_templates(niche: str) -> list[str]:
    name = normalize_niche(niche)
    preset = NICHES.get(name)
    if preset:
        return list(preset["title_templates"])
    return ["{keyword}: {hook}"]


def niche_description_templates(niche: str) -> list[str]:
    name = normalize_niche(niche)
    preset = NICHES.get(name)
    if preset:
        return list(preset["description_templates"])
    return ["{hook}"]


def niche_keyword_density(text: str, niche: str) -> float:
    """Fraction of the text's words that are niche keywords (0–1).

    Pure function — the scorer's niche signal. Case-insensitive,
    whole-word matches; a short span dense with niche words scores
    near 1, a span with none scores 0.
    """
    keywords = set(niche_keywords(niche))
    if not keywords:
        return 0.0
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return 0.0
    hits = sum(1 for tok in tokens if tok in keywords)
    return min(1.0, hits / max(4, len(tokens) * 0.2))


__all__ = [
    "CLIP_DEFAULTS",
    "GENERAL_NICHE",
    "NICHES",
    "PRESET_NAMES",
    "is_preset",
    "niche_description_templates",
    "niche_hashtags",
    "niche_keyword_density",
    "niche_keywords",
    "niche_label",
    "niche_title_templates",
    "normalize_niche",
]
