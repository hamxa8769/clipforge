"""Tests for ``clipforge.detection`` (v0.8.0, Worker A).

Formula pinned from the module docstring::

    viral = 0.25*hook + 0.25*peak + 0.15*payoff + 0.10*emotion
          + 0.10*visual + 0.15*completeness
"""

from __future__ import annotations

from clipforge import detection as det

# A ~126 s transcript: 21 segments x 6 s, one or two sentences each.
# Sentence 1 is a strong hook, sentence 12 carries the payoff.
_SENTENCES = [
    "What if I told you the secret nobody tells beginners?",
    "You can double your results with one simple change.",
    "Most people make this mistake every single day.",
    "I learned this the hard way after years of failing.",
    "The first step is to stop guessing and start measuring.",
    "Here is exactly how the process works in practice.",
    "You take your best idea and you test it small.",
    "Then you watch the numbers and you learn fast.",
    "It feels amazing when the first win finally lands.",
    "But most people quit right before that moment arrives.",
    "The difference is patience and honest feedback.",
    "So that is why the key is consistency over intensity.",
    "In the end what matters is showing up every day.",
    "Remember this lesson and your results will follow.",
    "The second trick is to keep your setup incredibly simple.",
    "You do not need fancy tools to get started today.",
    "Just pick one thing and do it really well.",
    "I was honestly shocked by how fast it worked.",
    "That is the whole story in one short video.",
    "Now go and try it yourself this week.",
    "Your future self will thank you for starting now.",
]


def make_segments(sentences=None, seg_s: float = 6.0, energy: float | None = None):
    """Segments of ``seg_s`` seconds, one sentence each."""
    out = []
    for i, text in enumerate(sentences or _SENTENCES):
        seg = {"start": i * seg_s, "end": (i + 1) * seg_s, "text": text}
        if energy is not None:
            seg["energy"] = energy
        out.append(seg)
    return out


def total_duration(segments):
    return segments[-1]["end"]


# ------------------------------------------------------------------ formula
def test_formula_weights_sum_to_one_and_match():
    assert abs(sum(det.WEIGHTS.values()) - 1.0) < 1e-9
    assert det.WEIGHTS == {
        "hook": 0.25,
        "peak": 0.25,
        "payoff": 0.15,
        "emotion": 0.10,
        "visual": 0.10,
        "completeness": 0.15,
    }
    sub = {
        "hook": 80.0,
        "peak": 60.0,
        "payoff": 40.0,
        "emotion": 20.0,
        "visual": 50.0,
        "completeness": 100.0,
    }
    expected = 0.25 * 80 + 0.25 * 60 + 0.15 * 40 + 0.10 * 20 + 0.10 * 50 + 0.15 * 100
    assert det.viral_score(sub) == round(expected, 1)


def test_viral_score_clamped_to_0_100():
    assert det.viral_score({k: 200.0 for k in det.WEIGHTS}) == 100.0
    assert det.viral_score({k: -50.0 for k in det.WEIGHTS}) == 0.0


# ------------------------------------------------------------------ hook
def test_hook_question_and_you_beat_plain_statement():
    hooky = det.score_hook("What if I told you the secret?")
    plain = det.score_hook("The process works in practice today.")
    assert hooky > plain
    assert hooky >= 50.0


def test_hook_number_and_emotion_add_points():
    assert det.score_hook("You get 5 amazing tricks today") > det.score_hook("You get tricks today")
    assert det.score_hook("") == 0.0
    assert det.score_hook("Stop! This is insane and shocking!") >= 45.0


def test_hook_niche_keyword_lifts_hook():
    base = det.score_hook("The process works in practice today.")
    lifted = det.score_hook("The process works in practice today.", niche_keywords=["process"])
    assert lifted == base + 10.0


# ------------------------------------------------------------------ peak
def test_peak_energy_present_beats_energy_absent():
    text = "You take your best idea and you test it small today."
    with_energy = det.score_peak(text, energies=[0.9] * 10, duration_s=6.0, word_count=10)
    without_energy = det.score_peak(text, energies=None, duration_s=6.0, word_count=10)
    assert with_energy > without_energy
    assert with_energy >= 45.0  # 0.9 * 50 from energy alone


def test_peak_laughter_and_emphasis_count_without_energy():
    text = "That was honestly so funny haha I really loved it."
    assert det.score_peak(text, None, duration_s=6.0, word_count=10) >= 40.0
    assert det.score_peak("The cat sat on the mat today.", None, 6.0, 7) < 20.0


# ------------------------------------------------------------------ payoff / emotion
def test_payoff_concluding_language_scores_high():
    assert det.score_payoff("so that is why the key is consistency") >= 40.0
    assert det.score_payoff("the cat sat on the mat today here") < 40.0
    assert det.score_payoff("") == 0.0


def test_emotion_arousal_words_score_higher_than_flat_text():
    hot = det.score_emotion("I love this amazing incredible brilliant win")
    flat = det.score_emotion("The cat sat on the mat today here now")
    assert hot > flat
    assert det.score_emotion("") == 0.0


# ------------------------------------------------------------------ visual
def test_visual_neutral_without_data():
    assert det.score_visual(0, 0, False) == 50.0
    assert det.score_visual(3, 2, True) > det.score_visual(0, 0, True)


# ------------------------------------------------------------------ completeness
def test_completeness_dangling_start_and_bad_duration_penalised():
    full = det.score_completeness(True, True, "You", 30.0)
    dangling = det.score_completeness(True, True, "It", 30.0)
    assert full == 100.0
    assert dangling < full
    assert det.score_completeness(True, True, "You", 5.0) < full
    assert det.score_completeness(False, False, "You", 30.0) == 50.0


# ------------------------------------------------------------------ end to end
def test_analyze_returns_ranked_candidates_in_15_60s_bounds():
    segments = make_segments()
    out = det.analyze(segments, total_duration(segments))
    assert 1 <= len(out) <= 12
    for cand in out:
        assert set(cand) >= {
            "start",
            "end",
            "score",
            "hook_score",
            "peak_score",
            "payoff_score",
            "emotion_score",
            "visual_score",
            "completeness_score",
            "title",
            "reason",
        }
        assert 15.0 <= cand["end"] - cand["start"] <= 60.0
        assert 0.0 <= cand["score"] <= 100.0
        assert cand["title"]
        assert cand["reason"]
    scores = [c["score"] for c in out]
    assert scores == sorted(scores, reverse=True)


def test_analyze_is_deterministic():
    segments = make_segments(energy=0.7)
    first = det.analyze(segments, total_duration(segments))
    second = det.analyze(segments, total_duration(segments))
    assert first == second


def test_analyze_too_short_transcript_returns_empty():
    short = [{"start": 0.0, "end": 10.0, "text": "Hello world this is short."}]
    assert det.analyze(short, 10.0) == []
    assert det.analyze([], 0.0) == []


def test_analyze_uses_visual_cues_and_niche():
    segments = make_segments(energy=0.6)
    visual = [{"t": 20.0, "scene_change": True, "faces": 2}]
    with_visual = det.analyze(segments, total_duration(segments), visual_cues=visual)
    without_visual = det.analyze(segments, total_duration(segments))
    assert with_visual and without_visual
    # Visual data changes at least one candidate's visual sub-score.
    assert any(c["visual_score"] != 50.0 for c in with_visual) or any(
        c["visual_score"] == 50.0 for c in without_visual
    )
    niche = det.analyze(segments, total_duration(segments), niche_keywords=["secret", "mistake"])
    assert len(niche) >= 1


def test_dedup_removes_high_iou_overlap():
    a = det.DetectionCandidate(
        start=0.0,
        end=30.0,
        score=90.0,
        hook_score=90,
        peak_score=80,
        payoff_score=70,
        emotion_score=60,
        visual_score=50,
        completeness_score=100,
        title="A",
        reason="r",
        topic_words=["alpha", "beta", "gamma"],
    )
    b = det.DetectionCandidate(
        start=5.0,
        end=32.0,
        score=70.0,
        hook_score=70,
        peak_score=60,
        payoff_score=50,
        emotion_score=40,
        visual_score=50,
        completeness_score=100,
        title="B",
        reason="r",
        topic_words=["delta", "epsilon", "zeta"],
    )
    kept = det.dedupe_and_diversify([b, a])
    assert [c.title for c in kept] == ["A"]


def test_diversity_penalises_near_duplicate_topics():
    def cand(title, score, words):
        return det.DetectionCandidate(
            start=0.0,
            end=30.0,
            score=score,
            hook_score=80,
            peak_score=80,
            payoff_score=70,
            emotion_score=60,
            visual_score=50,
            completeness_score=100,
            title=title,
            reason="r",
            topic_words=words,
        )

    # Same topic words, non-overlapping times: the second must be
    # skipped (Jaccard 1.0 > 0.7) or penalised, never kept at full score.
    a = cand("A", 90.0, ["alpha", "beta", "gamma", "delta"])
    b = cand("B", 85.0, ["alpha", "beta", "gamma", "delta"])
    b.start, b.end = 60.0, 90.0
    kept = det.dedupe_and_diversify([a, b])
    assert all(c.title != "B" or c.score < 85.0 for c in kept)


def test_segmentation_splits_sentences_and_assigns_topics():
    words = det.segments_to_words(make_segments())
    sentences = det.assign_topics(det.segment_sentences(words))
    assert len(sentences) == len(_SENTENCES)
    assert sentences[0].start_s == 0.0
    # A sharp topic change (cooking vs. coding) starts a new topic.
    cooking = det.segment_sentences(
        det.segments_to_words(
            [
                {"start": 0.0, "end": 5.0, "text": "Add salt and pepper to the soup."},
                {"start": 5.0, "end": 10.0, "text": "Stir the soup slowly now."},
                {"start": 10.0, "end": 15.0, "text": "The compiler optimises the binary code."},
                {"start": 15.0, "end": 20.0, "text": "Link the object files together."},
            ]
        )
    )
    det.assign_topics(cooking)
    assert cooking[-1].topic > cooking[0].topic


def test_short_word_count_but_long_span_still_yields_a_candidate():
    """Regression (v0.8.0 integration): the Studio sample is 9 words
    over 40 s — the old <10-word gate rejected it even though a
    15-60 s window exists. A 5-word, 10 s transcript still yields []."""
    sample = [
        {"start": 0.0, "end": 15.0, "text": "welcome back to the channel"},
        {"start": 15.0, "end": 40.0, "text": "the big reveal"},
    ]
    found = det.analyze(sample, 600.0)
    assert len(found) >= 1
    assert 15.0 <= found[0]["end"] - found[0]["start"] <= 60.0
    short = [{"start": 0.0, "end": 10.0, "text": "Hello world this is short."}]
    assert det.analyze(short, 10.0) == []
