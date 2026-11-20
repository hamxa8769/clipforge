"""Tests for ``clipforge.highlights`` — the real landed API.

  Candidate(start_word_index, end_word_index, start_s, end_s,
            score, hook, hook_line, reason, signals={}, source="heuristic")
  overlap_fraction(a, b) -> float   # TIME overlap / shorter duration, 0..1
  dedupe_and_rank(candidates, top_n=10) -> list[Candidate]
  DEDUPE_OVERLAP = 0.5
  score_span(words, start, end) -> (score 0-100, hook 1-10, signals)
  chunk_transcript(transcript, window_s=540, overlap_s=15) -> list[Chunk]
  find_heuristic_candidates(transcript, top_n=10) -> list[Candidate]

Rules (research report §2): highlights are found by word INDEX (models copy
numbers reliably, do arithmetic badly); candidates are scored 0-100 with a
hook rating 1-10 on the first 3 s; candidates overlapping >50% are deduped,
keeping the higher score. Overlap here is measured in TIME, as a fraction
of the shorter candidate — the landed implementation's definition.
"""

from __future__ import annotations

import pytest

from tests.conftest import load_or_skip

# --- Spec values pinned from the research report (these tests run now). ---
SPEC_DEDUPE_THRESHOLD = 0.5
SPEC_SCORE_MIN, SPEC_SCORE_MAX = 0, 100
SPEC_HOOK_MIN, SPEC_HOOK_MAX = 1, 10

# Words rich in the report's virality signals, so the heuristic scorer has
# something to find. 240 words spread over 120 s by the fallback transcript.
SAMPLE_TEXT = " ".join(
    [
        "why",
        "stop",
        "doing",
        "this",
        "amazing",
        "secret",
        "revealed",
        "question",
        "what",
        "happens",
        "next",
        "will",
        "shock",
        "you",
        "incredible",
        "story",
        "finally",
        "truth",
        "mistake",
        "everyone",
        "makes",
        "learn",
        "how",
        "to",
        "win",
        "today",
        "real",
        "change",
        "life",
        "powerful",
    ]
    * 8
)


def _cand(start_s, end_s, score, hook=8, start_word=0, end_word=10):
    hl = load_or_skip("clipforge.highlights")
    return hl.Candidate(
        start_word_index=start_word,
        end_word_index=end_word,
        start_s=start_s,
        end_s=end_s,
        score=score,
        hook=hook,
        hook_line="hook line",
        reason="test candidate",
    )


def _transcript():
    tr = load_or_skip("clipforge.transcribe")
    return tr.fallback_transcript_from_text(SAMPLE_TEXT, duration_s=120.0)


# --- Spec-pinning tests: the contract values themselves. ------------------


def test_dedupe_threshold_is_half():
    """Dedupe rule is strictly greater than 50% overlap (report §2)."""
    assert SPEC_DEDUPE_THRESHOLD == 0.5
    hl = load_or_skip("clipforge.highlights")
    assert hl.DEDUPE_OVERLAP == 0.5


def test_score_bounds_are_0_to_100():
    assert (SPEC_SCORE_MIN, SPEC_SCORE_MAX) == (0, 100)


def test_hook_rating_bounds_are_1_to_10():
    assert (SPEC_HOOK_MIN, SPEC_HOOK_MAX) == (1, 10)


def test_word_index_integrity_rule():
    """A valid candidate must satisfy 0 <= start < end <= word_count."""
    word_count = 100
    for start, end in [(0, 10), (5, 6), (99, 100)]:
        assert 0 <= start < end <= word_count
    for start, end in [(10, 10), (20, 5), (-1, 10), (0, 101)]:
        assert not (0 <= start < end <= word_count)


# --- Implementation tests against the landed module. ----------------------


def test_dedupe_drops_over_50pct_overlap_keeping_higher_score():
    hl = load_or_skip("clipforge.highlights")
    low = _cand(0, 100, score=60)
    high = _cand(10, 90, score=90)  # time overlap 80/80 = 1.0
    assert hl.dedupe_and_rank([low, high]) == [high]


def test_dedupe_keeps_at_most_50pct_overlap():
    hl = load_or_skip("clipforge.highlights")
    # Time overlap 50 s / shorter 100 s = 0.5 — NOT > threshold, both survive.
    a = _cand(0, 100, score=80)
    b = _cand(50, 150, score=70)
    assert len(hl.dedupe_and_rank([a, b])) == 2


def test_dedupe_keeps_disjoint_candidates_and_sorts_by_score_desc():
    hl = load_or_skip("clipforge.highlights")
    a = _cand(0, 50, score=40)
    b = _cand(100, 150, score=95)
    c = _cand(200, 250, score=70)
    assert hl.dedupe_and_rank([a, b, c]) == [b, c, a]


def test_dedupe_empty_in_empty_out():
    hl = load_or_skip("clipforge.highlights")
    assert hl.dedupe_and_rank([]) == []


def test_overlap_fraction_values():
    hl = load_or_skip("clipforge.highlights")
    assert hl.overlap_fraction(_cand(0, 10, 50), _cand(0, 10, 50)) == pytest.approx(1.0)
    assert hl.overlap_fraction(_cand(0, 10, 50), _cand(20, 30, 50)) == pytest.approx(0.0)
    assert hl.overlap_fraction(_cand(0, 10, 50), _cand(5, 15, 50)) == pytest.approx(0.5)


def test_score_span_bounds():
    hl = load_or_skip("clipforge.highlights")
    transcript = _transcript()
    score, hook, signals = hl.score_span(transcript.words, 0, 60)
    assert SPEC_SCORE_MIN <= score <= SPEC_SCORE_MAX
    assert SPEC_HOOK_MIN <= hook <= SPEC_HOOK_MAX
    assert signals  # the sample text carries hook/opinion/revelation signals


def test_score_span_empty_is_zero():
    hl = load_or_skip("clipforge.highlights")
    transcript = _transcript()
    score, hook, _ = hl.score_span(transcript.words, 10, 10)
    assert score == 0.0
    assert hook == 1


def test_chunk_transcript_windows_cover_with_overlap():
    hl = load_or_skip("clipforge.highlights")
    tr = load_or_skip("clipforge.transcribe")
    long_text = " ".join(["word"] * 1200)
    transcript = tr.fallback_transcript_from_text(long_text, duration_s=600.0)
    chunks = hl.chunk_transcript(transcript)
    assert len(chunks) == 2  # 9-min window + 15 s overlap over 600 s
    assert chunks[0].start_s == pytest.approx(0.0)
    assert chunks[-1].end_s == pytest.approx(600.0)
    overlap = chunks[0].end_s - chunks[1].start_s
    assert overlap == pytest.approx(15.0, abs=1.0)


def test_find_heuristic_candidates_are_valid_and_ranked():
    hl = load_or_skip("clipforge.highlights")
    transcript = _transcript()
    candidates = hl.find_heuristic_candidates(transcript, top_n=5)
    assert 1 <= len(candidates) <= 5
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)
    word_count = len(transcript.words)
    for cand in candidates:
        assert 0 <= cand.start_word_index < cand.end_word_index <= word_count
        assert cand.start_s < cand.end_s
        assert 0 <= cand.score <= 100
        assert 1 <= cand.hook <= 10
        assert cand.hook_line and cand.reason
        assert cand.duration_s == pytest.approx(cand.end_s - cand.start_s)
    # No kept pair overlaps by more than 50% (time).
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            assert hl.overlap_fraction(a, b) <= 0.5
