"""Tests for ``clipforge.seo`` + ``clipforge.publishing.limits`` — real API.

  generate_seo(platform, keyword, hook, title_hint="", duration_s=30.0,
               extra_hashtags=None, llm_polish=False) -> dict
      keys: platform, title, description, caption, hashtags, duration_s,
      warnings (from validate(); [] means clean)
  generate_all(keyword, hook, title_hint="", duration_s=30.0)
      -> {platform: variant} for all four platforms
  validate(platform, variant) -> list[str]  # warnings, [] means clean

  PLATFORM_LIMITS[platform]["max_hashtags"] / ["title_max_chars"] /
      ["max_duration_s"] — the single source of truth for limits.

Rules (research report §4): SEO is per-platform, not one caption copied
four ways. YouTube shows only 3 hashtags above the title and caps titles
at 100 chars (aim 55-60); Instagram's in-app hashtag cap is 5 (Dec 2025,
down from 30); TikTok takes 2-5; Facebook 2-3 niche tags.
"""

from __future__ import annotations

import pytest

from tests.conftest import load_or_skip

# --- Spec values pinned from the research report (these tests run now). ---
SPEC_HASHTAG_MAX = {"youtube": 3, "instagram": 5, "tiktok": 5, "facebook": 3}
SPEC_TITLE_MAX_CHARS = {"youtube": 100}


def test_hashtag_caps_match_research():
    limits = load_or_skip("clipforge.publishing.limits")
    for platform, cap in SPEC_HASHTAG_MAX.items():
        assert limits.PLATFORM_LIMITS[platform]["max_hashtags"] == cap
    # Instagram's Dec 2025 cap of 5 (API docs still say 30 — the app cap wins).
    assert SPEC_HASHTAG_MAX["instagram"] == 5


def test_youtube_title_max_is_100_chars():
    limits = load_or_skip("clipforge.publishing.limits")
    assert limits.PLATFORM_LIMITS["youtube"]["title_max_chars"] == 100
    assert SPEC_TITLE_MAX_CHARS["youtube"] == 100


def test_platform_duration_caps():
    limits = load_or_skip("clipforge.publishing.limits")
    assert limits.PLATFORM_LIMITS["youtube"]["max_duration_s"] == 180  # Shorts <=3 min
    assert limits.PLATFORM_LIMITS["facebook"]["max_duration_s"] == 90  # API cap
    assert limits.PLATFORM_LIMITS["instagram"]["max_duration_s"] == 900  # 15 min
    assert limits.PLATFORM_LIMITS["tiktok"]["max_duration_s"] == 600


# --- Implementation tests against the landed module. ----------------------


@pytest.mark.parametrize(
    "platform,low,high",
    [
        ("youtube", 2, 3),
        ("instagram", 1, 5),
        ("tiktok", 2, 5),
        ("facebook", 2, 3),
    ],
)
def test_generated_hashtag_counts_per_platform(platform, low, high):
    seo = load_or_skip("clipforge.seo")
    variant = seo.generate_seo(platform, "morning routine", "Stop doing this first")
    assert low <= len(variant["hashtags"]) <= high
    assert all(tag and " " not in tag for tag in variant["hashtags"])


def test_generated_youtube_title_within_100_chars():
    seo = load_or_skip("clipforge.seo")
    variant = seo.generate_seo(
        "youtube", "morning routine", "Stop doing this first", title_hint="x" * 200
    )
    assert len(variant["title"]) <= 100  # hard cap, never exceeded


def test_generate_all_returns_four_distinct_variants():
    seo = load_or_skip("clipforge.seo")
    variants = seo.generate_all("morning routine", "Stop doing this first")
    assert set(variants) == {"youtube", "instagram", "facebook", "tiktok"}
    for platform, variant in variants.items():
        assert variant["platform"] == platform
        assert variant["hashtags"]
    # Per-platform adaptation lives in titles, hashtags + description/caption:
    # each platform gets its own title shape (YouTube keyword-front,
    # TikTok hook-only, Instagram hook | keyword, Facebook keyword — hook),
    # its own tag set and caption shape.
    titles = {(v.get("title") or "") for v in variants.values()}
    assert len(titles) >= 3, f"titles must diverge per platform, got {titles}"
    hashtag_sets = {tuple(v["hashtags"]) for v in variants.values()}
    assert len(hashtag_sets) > 1
    descriptions = {(v.get("description") or "") for v in variants.values()}
    assert len(descriptions) > 1


def test_generate_seo_rejects_unknown_platform_and_empty_keyword():
    seo = load_or_skip("clipforge.seo")
    with pytest.raises(ValueError):
        seo.generate_seo("myspace", "keyword", "hook")
    with pytest.raises(ValueError):
        seo.generate_seo("youtube", "   ", "hook")


def test_validate_flags_too_many_hashtags():
    seo = load_or_skip("clipforge.seo")
    variant = {"hashtags": ["a"] * 6, "duration_s": 30.0}
    warnings = seo.validate("instagram", variant)
    assert any("hashtag" in w.lower() for w in warnings)


def test_validate_flags_overlong_youtube_title():
    seo = load_or_skip("clipforge.seo")
    variant = {"title": "x" * 101, "description": "keyword here", "hashtags": ["a", "b"]}
    warnings = seo.validate("youtube", variant)
    assert any("100" in w for w in warnings)


def test_validate_flags_short_tiktok_clip_for_rewards():
    seo = load_or_skip("clipforge.seo")
    variant = {"hashtags": ["a", "b"], "duration_s": 30.0}
    warnings = seo.validate("tiktok", variant)
    assert any("Creator Rewards" in w for w in warnings)


def test_validate_flags_facebook_clip_over_90s_api_cap():
    seo = load_or_skip("clipforge.seo")
    variant = {"hashtags": ["a", "b"], "duration_s": 120.0}
    warnings = seo.validate("facebook", variant)
    assert any("90" in w for w in warnings)


def test_generated_variant_validates_clean():
    seo = load_or_skip("clipforge.seo")
    variant = seo.generate_seo("youtube", "morning routine", "Stop doing this first")
    assert variant["warnings"] == []
    assert seo.validate("youtube", variant) == []
