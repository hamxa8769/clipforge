"""Tests for the v0.11.0 SEO pack (``clipforge.seo.pack``).

The pack builds on the existing per-platform variants (``clipforge.seo``):
every clip gets title options, a description, plain search tags and
hashtags for YouTube Shorts / TikTok / Instagram Reels / Facebook Reels /
X, plus one plain-language tip per platform. All deterministic — no LLM,
no key, same output every run.
"""

from __future__ import annotations

import pytest

from tests.conftest import load_or_skip


@pytest.fixture(scope="module")
def pack_mod():
    return load_or_skip("clipforge.seo.pack")


def _pack(pack_mod, **kwargs):
    kwargs.setdefault("keyword", "morning routine")
    kwargs.setdefault("hook", "Stop doing this first thing in the morning")
    return pack_mod.build_seo_pack(**kwargs)


def test_pack_has_five_platforms(pack_mod):
    pack = _pack(pack_mod)
    assert set(pack["platforms"]) == {"youtube", "tiktok", "instagram", "facebook", "x"}
    assert pack["keyword"] == "morning routine"


def test_each_platform_has_complete_fields(pack_mod):
    pack = _pack(pack_mod)
    for platform, variant in pack["platforms"].items():
        assert variant["platform"] == platform
        assert variant["title"], platform
        assert 2 <= len(variant["title_options"]) <= 3, platform
        assert variant["title"] == variant["title_options"][0], platform
        assert len(set(o.lower() for o in variant["title_options"])) == len(
            variant["title_options"]
        ), f"duplicate title options on {platform}"
        assert variant["description"], platform
        assert variant["tags"], platform
        assert variant["hashtags"], platform
        assert variant["tip"], platform
        assert isinstance(variant["warnings"], list)


def test_title_options_within_caps(pack_mod):
    pack = _pack(pack_mod, hook="x" * 300, title_hint="y" * 300)
    caps = {"youtube": 100, "tiktok": 60, "instagram": 68, "facebook": 55, "x": 70}
    for platform, variant in pack["platforms"].items():
        for option in variant["title_options"]:
            assert len(option) <= caps[platform], (platform, option)


def test_youtube_description_front_loads_keyword(pack_mod):
    pack = _pack(pack_mod)
    desc = pack["platforms"]["youtube"]["description"]
    assert "morning routine" in desc[:200].lower()


def test_search_tags_plain_no_hash(pack_mod):
    pack = _pack(
        pack_mod,
        source_channel="Daily Clips",
        transcript="morning routine coffee morning routine stretch",
    )
    tags = pack["platforms"]["youtube"]["tags"]
    assert "morning routine" in tags
    assert all("#" not in t for t in tags)
    assert all(t == t.lower() for t in tags)
    assert len(tags) == len(set(tags))
    assert len(tags) <= 15
    # X gets fewer tags than YouTube.
    assert len(pack["platforms"]["x"]["tags"]) <= 5


def test_search_tags_come_from_transcript_words(pack_mod):
    pack = _pack(
        pack_mod,
        transcript="coffee coffee coffee stretch stretch morning routine",
    )
    tags = pack["platforms"]["youtube"]["tags"]
    assert "coffee" in tags


def test_hashtag_caps_unchanged(pack_mod):
    pack = _pack(pack_mod)
    assert len(pack["platforms"]["youtube"]["hashtags"]) <= 3
    assert len(pack["platforms"]["instagram"]["hashtags"]) <= 5
    assert len(pack["platforms"]["tiktok"]["hashtags"]) <= 5
    assert len(pack["platforms"]["facebook"]["hashtags"]) <= 3


def test_description_adds_what_was_said(pack_mod):
    pack = _pack(pack_mod, transcript="First I drink water, then I stretch for five minutes.")
    assert "What was said" in pack["platforms"]["youtube"]["description"]


def test_copy_all_contains_every_platform(pack_mod):
    pack = _pack(pack_mod)
    text = pack["copy_all"]
    for label in ("YouTube Shorts", "TikTok", "Instagram Reels", "Facebook Reels", "X"):
        assert label in text
    assert "Tags:" in text
    assert "Hashtags:" in text


def test_stored_variant_wins_over_fresh(pack_mod):
    stored = {"youtube": {"title": "Stored title wins", "description": "Stored description."}}
    pack = _pack(pack_mod, stored=stored)
    assert pack["platforms"]["youtube"]["title"] == "Stored title wins"
    assert pack["platforms"]["youtube"]["description"].startswith("Stored description.")
    # ...but the title options still come from the fresh generation.
    assert pack["platforms"]["youtube"]["title_options"][0] == "Stored title wins"


def test_keyword_required(pack_mod):
    with pytest.raises(ValueError, match="keyword is required"):
        pack_mod.build_seo_pack(keyword="  ", hook="something")


def test_empty_hook_still_builds(pack_mod):
    pack = _pack(pack_mod, hook="")
    for variant in pack["platforms"].values():
        assert variant["title"]
        assert variant["title_options"]


def test_extract_search_tags_numbered_names(pack_mod):
    tags = pack_mod.extract_search_tags("gaming", "GTA 6 clutch moment", limit=10)
    assert "gta6" in tags
