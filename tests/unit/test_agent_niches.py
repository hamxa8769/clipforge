"""Unit tests — niches, rule engine, niche scoring (ClipForge Agent v0.5.0).

Pure functions only: no network, no keys, no FFmpeg.
"""

from __future__ import annotations

from clipforge.highlights import find_heuristic_candidates, score_span
from clipforge.niches import (
    NICHES,
    PRESET_NAMES,
    niche_hashtags,
    niche_keyword_density,
    niche_keywords,
    normalize_niche,
)
from clipforge.rule_engine import (
    extract_keywords,
    generate_all_platforms,
    generate_copy,
    summarize_transcript,
)
from clipforge.transcribe import fallback_transcript_from_text

GAMING_TEXT = (
    "Stop making this mistake in ranked games. This clutch win was insane, "
    "nobody expected the squad to survive the final boss fight. The secret "
    "is the loot build: grab the best weapon, level up fast, and never "
    "fight alone. Everyone says this strategy is overrated, but watch this "
    "gameplay and tell me I am wrong. How do you win a one versus five? "
    "You stay calm, you aim, you clutch. This is the greatest gaming moment "
    "of the season, and the crowd went crazy when the final kill landed."
)


def _transcript(text: str = GAMING_TEXT, duration_s: float = 75.0):
    return fallback_transcript_from_text(text, duration_s=duration_s)


# ---------------------------------------------------------------- niches
def test_preset_list_has_eight_niches():
    assert set(PRESET_NAMES) == {
        "gaming",
        "tech",
        "vlogging",
        "education",
        "finance",
        "fitness",
        "cooking",
        "music",
    }
    for name in PRESET_NAMES:
        preset = NICHES[name]
        assert preset["keywords"] and preset["hashtags"]
        assert preset["title_templates"] and preset["description_templates"]


def test_normalize_niche():
    assert normalize_niche("gaming") == "gaming"
    assert normalize_niche("Street Food") == "street-food"
    assert normalize_niche("") == "general"
    assert normalize_niche(None) == "general"
    assert normalize_niche("  TECH  ") == "tech"


def test_niche_keywords_preset_and_custom():
    assert "clutch" in niche_keywords("gaming")
    assert "game" in niche_keywords("gaming")
    assert niche_keywords("street-food") == ["street", "food"]
    assert niche_keywords("general") == []
    assert niche_keywords("") == []


def test_niche_keyword_density():
    assert niche_keyword_density(GAMING_TEXT, "gaming") > 0.1
    assert niche_keyword_density(GAMING_TEXT, "cooking") == 0.0
    assert niche_keyword_density("", "gaming") == 0.0
    assert niche_keyword_density(GAMING_TEXT, "general") == 0.0


def test_niche_hashtags():
    tags = niche_hashtags("gaming")
    assert "gaming" in tags
    assert niche_hashtags("street-food") == ["street", "food"]


# ---------------------------------------------------------------- rule engine
def test_extract_keywords_puts_niche_first():
    keywords = extract_keywords(GAMING_TEXT, "gaming", top_n=5)
    assert keywords
    assert keywords[0] in niche_keywords("gaming")
    assert len(keywords) <= 5


def test_extract_keywords_empty_text():
    assert extract_keywords("", "gaming") == []


def test_generate_copy_rules_engine_and_niche():
    copy = generate_copy("ranked games", "Stop making this mistake", "gaming", "youtube", 30.0)
    assert copy["engine"] == "rules"
    assert copy["niche"] == "gaming"
    assert "gaming" in copy["hashtags"]
    assert len(copy["title"]) <= 100
    assert copy["description"]
    assert copy["defaults"]["hook_window_s"] == 3.0


def test_generate_all_platforms_differ_per_platform():
    all_copy = generate_all_platforms("ranked games", "Stop making this mistake", "gaming", 30.0)
    assert set(all_copy) == {"youtube", "tiktok", "instagram", "facebook"}
    assert all(c["engine"] == "rules" for c in all_copy.values())
    titles = {c["title"] for c in all_copy.values()}
    assert len(titles) > 1  # never one caption copied four ways


def test_summarize_transcript():
    summary = summarize_transcript(GAMING_TEXT, "gaming")
    assert summary["engine"] == "rules"
    assert summary["keywords"]
    assert summary["hook_line"]


# ---------------------------------------------------------------- niche scoring
def test_score_span_niche_lifts_matching_span():
    transcript = _transcript()
    words = transcript.words
    span_end = next(i for i, w in enumerate(words) if w.end >= 30.0)
    plain_score, _, plain_signals = score_span(words, 0, span_end)
    niche_score, _, niche_signals = score_span(words, 0, span_end, niche="gaming")
    assert niche_signals["niche"] > 0
    assert plain_signals["niche"] == 0.0
    assert niche_score >= plain_score


def test_score_span_unrelated_niche_no_lift():
    transcript = _transcript()
    words = transcript.words
    span_end = next(i for i, w in enumerate(words) if w.end >= 30.0)
    plain_score, _, _ = score_span(words, 0, span_end)
    cooking_score, _, cooking_signals = score_span(words, 0, span_end, niche="cooking")
    assert cooking_signals["niche"] == 0.0
    assert cooking_score == plain_score


def test_score_span_niche_word_in_hook_lifts_hook():
    # First 3 seconds are all gaming words -> hook should be strong.
    text = "clutch win ranked game clutch win ranked game " + ("filler word here. " * 40)
    transcript = _transcript(text, duration_s=90.0)
    words = transcript.words
    span_end = next(i for i, w in enumerate(words) if w.end >= 30.0)
    _, hook_plain, _ = score_span(words, 0, span_end)
    _, hook_niche, _ = score_span(words, 0, span_end, niche="gaming")
    assert hook_niche >= hook_plain
    assert hook_niche >= 5


def test_find_heuristic_candidates_carry_niche_signal():
    transcript = _transcript(GAMING_TEXT * 3, duration_s=180.0)
    cands = find_heuristic_candidates(transcript, top_n=3, niche="gaming")
    assert cands
    assert all("niche" in c.signals for c in cands)
    assert all(c.signals.get("niche", 0) >= 0 for c in cands)
