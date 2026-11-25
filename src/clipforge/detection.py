"""Deeper AI clip detection for ClipForge Pro (v0.8.0, Worker A).

Deterministic, rule-based detection over word-level transcript
segments — no network calls, no model downloads, fully unit-testable.
It goes deeper than :mod:`clipforge.highlights` (which scores whole
spans on one lexicon pass) by segmenting the transcript into
sentences/topics first, generating sentence-anchored sliding windows,
and scoring every candidate on six named sub-scores.

This module never downloads or fetches a video — it scores
transcript segments the caller already has.

Scoring formula (all sub-scores 0-100, final score 0-100)::

    viral = 0.25*hook + 0.25*peak + 0.15*payoff + 0.10*emotion
          + 0.10*visual + 0.15*completeness

* ``hook`` — the first 3 s: a question, a bold claim, strong emotion
  words, a number, or a direct "you" address.
* ``peak`` — audio energy / loudness / pace plus emphasis words and
  laughter markers inside the window. Uses the caller-supplied
  ``energy`` values when present; when absent it degrades gracefully
  to pace + emphasis + laughter only (never invents energy).
* ``payoff`` — concluding / insight language in the last 20% of the
  window ("so", "that's why", "the key", summary phrases).
* ``emotion`` — sentiment-lexicon intensity (positive/negative arousal
  words) across the window.
* ``visual`` — scene changes + face presence in the window from the
  optional visual cues; 50 (neutral) when no visual data is given.
* ``completeness`` — self-contained: starts and ends on a sentence
  boundary, no dangling pronoun/reference at the start, duration
  inside 15-60 s.

After scoring, candidates are deduped (time IoU > 0.5 keeps the
higher score) and topic-diversified (a candidate whose topic keywords
nearly duplicate an already-kept, higher-scoring candidate is
penalised, so the top 8-12 clips are not near-duplicates). Titles are
rule-based hook titles built from the window's opening sentence —
Gemini enhancement happens elsewhere; this module stays deterministic.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Clip shape (same bounds as highlights.py / CLIP_DEFAULTS).
MIN_CLIP_S = 15.0
MAX_CLIP_S = 60.0
TARGET_CLIP_S = 30.0
MAX_CANDIDATES = 12
DEDUPE_IOU = 0.5  # time IoU > 0.5 -> keep the higher score

# Documented weights — the formula above, as data, so tests can pin it.
WEIGHTS = {
    "hook": 0.25,
    "peak": 0.25,
    "payoff": 0.15,
    "emotion": 0.10,
    "visual": 0.10,
    "completeness": 0.15,
}

# --- lexicons -----------------------------------------------------------
QUESTION_WORDS = {"what", "why", "how", "when", "where", "who", "which", "did", "do"}
BOLD_CLAIM_WORDS = {
    "secret",
    "never",
    "nobody",
    "everyone",
    "mistake",
    "truth",
    "warning",
    "shocking",
    "insane",
    "crazy",
    "stop",
    "free",
    "imagine",
    "lie",
    "myth",
    "scam",
    "fake",
    "overrated",
    "underrated",
    "best",
    "worst",
    "guaranteed",
    "proven",
    "nobody tells",
    "hidden",
}
EMOTION_WORDS = {
    "love",
    "hate",
    "angry",
    "amazing",
    "terrible",
    "incredible",
    "unbelievable",
    "wow",
    "scared",
    "excited",
    "frustrated",
    "devastated",
    "thrilled",
    "heartbreaking",
    "obsessed",
    "furious",
    "beautiful",
    "horrible",
    "fantastic",
    "disaster",
    "genius",
    "stupid",
    "brilliant",
    "insane",
    "shocking",
    "crazy",
}
POSITIVE_WORDS = {
    "love",
    "amazing",
    "incredible",
    "fantastic",
    "brilliant",
    "genius",
    "beautiful",
    "excited",
    "thrilled",
    "great",
    "awesome",
    "perfect",
    "best",
    "win",
    "won",
    "success",
    "happy",
    "proud",
}
NEGATIVE_WORDS = {
    "hate",
    "angry",
    "terrible",
    "horrible",
    "devastated",
    "furious",
    "frustrated",
    "scared",
    "disaster",
    "worst",
    "fail",
    "failed",
    "mistake",
    "wrong",
    "pain",
    "loss",
    "lost",
    "stupid",
}
EMPHASIS_WORDS = {
    "very",
    "really",
    "absolutely",
    "totally",
    "literally",
    "exactly",
    "definitely",
    "seriously",
    "honestly",
    "actually",
    "incredibly",
    "extremely",
    "super",
    "so",
}
PAYOFF_PHRASES = {
    "so",
    "that's why",
    "that is why",
    "the key",
    "the secret is",
    "in the end",
    "bottom line",
    "the point is",
    "what matters",
    "remember",
    "takeaway",
    "lesson",
    "turns out",
    "in summary",
    "to sum up",
    "finally",
    "ultimately",
    "the result",
    "it works",
    "that's how",
    "that is how",
}
LAUGHTER_MARKERS = {"haha", "lol", "laugh", "laughs", "laughing", "[laughter]"}
DANGLING_STARTERS = {
    "it",
    "this",
    "that",
    "they",
    "he",
    "she",
    "them",
    "those",
    "these",
    "and",
    "but",
    "so",
    "then",
    "also",
    "because",
}
NUMBER_RE = re.compile(r"\b\d+([.,]\d+)?\b")
YOU_RE = re.compile(r"\byou\b", re.IGNORECASE)
QUESTION_RE = re.compile(r"\b(what|why|how|when|where|who|which)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z][a-z\-']*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

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


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _content_words(text: str) -> set[str]:
    return {w for w in _tokens(text) if w not in _STOPWORDS}


# --- data shapes --------------------------------------------------------
@dataclass
class Word:
    """One word with its time span, spread from its segment."""

    text: str
    start: float
    end: float


@dataclass
class Sentence:
    """One sentence with word indices and its time span."""

    text: str
    start_word: int
    end_word: int  # exclusive
    start_s: float
    end_s: float
    topic: int = 0


@dataclass
class DetectionCandidate:
    """One scored clip candidate (all sub-scores 0-100)."""

    start: float
    end: float
    score: float
    hook_score: float
    peak_score: float
    payoff_score: float
    emotion_score: float
    visual_score: float
    completeness_score: float
    title: str
    reason: str
    topic: int = 0
    topic_words: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("topic_words", None)
        return data


# --- normalisation ------------------------------------------------------
def segments_to_words(segments: list[dict]) -> list[Word]:
    """Flatten transcript segments into timed words.

    Each segment's words are spread evenly across its [start, end]
    span. Segments without text, or with end <= start, are skipped.
    Pure function.
    """
    words: list[Word] = []
    for seg in segments or []:
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", 0.0))
            text = str(seg.get("text") or "").strip()
        except (AttributeError, TypeError, ValueError):
            continue
        if not text or end <= start:
            continue
        parts = text.split()
        span = (end - start) / len(parts)
        for i, part in enumerate(parts):
            words.append(Word(part, start + i * span, start + (i + 1) * span))
    return words


def segment_sentences(words: list[Word]) -> list[Sentence]:
    """Split words into sentences on [.!?] boundaries.

    A trailing run without terminal punctuation is still a sentence,
    so no words are dropped. Pure function.
    """
    sentences: list[Sentence] = []
    if not words:
        return sentences
    start_idx = 0
    for i, word in enumerate(words):
        if word.text.endswith((".", "!", "?")):
            sentences.append(
                Sentence(
                    text=" ".join(w.text for w in words[start_idx : i + 1]),
                    start_word=start_idx,
                    end_word=i + 1,
                    start_s=words[start_idx].start,
                    end_s=word.end,
                )
            )
            start_idx = i + 1
    if start_idx < len(words):
        sentences.append(
            Sentence(
                text=" ".join(w.text for w in words[start_idx:]),
                start_word=start_idx,
                end_word=len(words),
                start_s=words[start_idx].start,
                end_s=words[-1].end,
            )
        )
    return sentences


def assign_topics(sentences: list[Sentence], overlap_threshold: float = 0.25) -> list[Sentence]:
    """Group sentences into topics by content-word overlap.

    A sentence joins the current topic while its content words
    overlap (Jaccard) with the topic so far at or above the
    threshold; otherwise it starts a new topic. Pure function.
    """
    topic = 0
    topic_words: set[str] = set()
    for sent in sentences:
        words = _content_words(sent.text)
        if topic_words:
            union = words | topic_words
            jaccard = len(words & topic_words) / len(union) if union else 0.0
            if jaccard < overlap_threshold:
                topic += 1
                topic_words = set()
        sent.topic = topic
        topic_words |= words
    return sentences


# --- sub-scores (each 0-100, pure functions) ----------------------------
def score_hook(first_3s_text: str, niche_keywords: list[str] | None = None) -> float:
    """Hook strength of the first 3 s.

    Question +25, bold claim +25, strong emotion word +20, a number
    +15, direct "you" address +15; a niche keyword in the hook +10
    (capped at 100). Empty text scores 0.
    """
    text = (first_3s_text or "").strip()
    if not text:
        return 0.0
    score = 0.0
    tokens = set(_tokens(text))
    if QUESTION_RE.search(text):
        score += 25.0
    if tokens & BOLD_CLAIM_WORDS:
        score += 25.0
    if tokens & EMOTION_WORDS:
        score += 20.0
    if NUMBER_RE.search(text):
        score += 15.0
    if YOU_RE.search(text):
        score += 15.0
    if niche_keywords:
        niche = {k.lower() for k in niche_keywords}
        if tokens & niche:
            score += 10.0
    return min(100.0, score)


def score_peak(
    window_text: str,
    energies: list[float] | None = None,
    duration_s: float = 30.0,
    word_count: int = 0,
) -> float:
    """Peak intensity: energy + pace + emphasis + laughter.

    With energy values (0-1 loudness per segment/word in the window)
    the mean energy contributes up to 50 points. Without energy the
    score degrades gracefully to pace (words/second vs a ~2.5 w/s
    baseline, up to 30), emphasis words (up to 30) and laughter
    markers (up to 40) — it never invents energy it was not given.
    """
    score = 0.0
    tokens = _tokens(window_text or "")
    if energies:
        valid = [min(1.0, max(0.0, float(e))) for e in energies]
        if valid:
            score += 50.0 * (sum(valid) / len(valid))
    # Pace: brisk delivery reads as a peak; ~2.5 words/s is baseline.
    if duration_s > 0 and word_count:
        pace = word_count / duration_s
        score += min(30.0, max(0.0, (pace - 1.5) / 2.0) * 30.0)
    emphasis = len([t for t in tokens if t in EMPHASIS_WORDS])
    score += min(30.0, emphasis * 10.0)
    if set(tokens) & LAUGHTER_MARKERS:
        score += 40.0
    return min(100.0, score)


def score_payoff(last_20pct_text: str) -> float:
    """Payoff: concluding / insight language in the last 20%."""
    text = (last_20pct_text or "").strip().lower()
    if not text:
        return 0.0
    hits = sum(1 for phrase in PAYOFF_PHRASES if phrase in text)
    score = min(100.0, hits * 40.0)
    # A question-free declarative close with a payoff phrase is the
    # classic "and that's why" ending; a bare trailing fragment scores
    # little even with one marker.
    if hits and len(text.split()) >= 4:
        score = max(score, 40.0)
    return score


def score_emotion(window_text: str) -> float:
    """Sentiment-lexicon intensity (arousal, either valence)."""
    tokens = _tokens(window_text or "")
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in POSITIVE_WORDS or t in NEGATIVE_WORDS)
    # Density-based: a window where ~1 word in 8 carries arousal is
    # saturated; scale so that density maps onto 0-100.
    return min(100.0, (hits / len(tokens)) * 8 * 100.0)


def score_visual(
    scene_changes: int,
    faces: int,
    has_visual_data: bool,
) -> float:
    """Visual interest from cues; 50 (neutral) with no visual data."""
    if not has_visual_data:
        return 50.0
    score = 40.0  # a window with visual data but no events is middling
    score += min(30.0, scene_changes * 10.0)
    score += min(30.0, faces * 10.0)
    return min(100.0, score)


def score_completeness(
    starts_on_boundary: bool,
    ends_on_boundary: bool,
    first_word: str,
    duration_s: float,
) -> float:
    """Self-containedness: boundaries + no dangling start + duration."""
    score = 100.0
    if not starts_on_boundary:
        score -= 25.0
    if not ends_on_boundary:
        score -= 25.0
    if (first_word or "").lower().strip(".,!?") in DANGLING_STARTERS:
        score -= 20.0
    if duration_s < MIN_CLIP_S or duration_s > MAX_CLIP_S:
        score -= 30.0
    elif abs(duration_s - TARGET_CLIP_S) > 15:
        score -= 10.0
    return max(0.0, min(100.0, score))


def viral_score(sub: dict[str, float]) -> float:
    """The documented weighted formula, from the six sub-scores."""
    total = sum(sub[name] * WEIGHTS[name] for name in WEIGHTS)
    return round(min(100.0, max(0.0, total)), 1)


# --- candidate generation + scoring -------------------------------------
def _window_text(words: list[Word], start_idx: int, end_idx: int) -> str:
    return " ".join(w.text for w in words[start_idx:end_idx])


def _hook_title(opening_sentence: str) -> str:
    """Rule-based hook title from the window's opening sentence."""
    text = (opening_sentence or "").strip().rstrip(".!?")
    if not text:
        return "Clip"
    words = text.split()
    title = " ".join(words[:9])
    if len(words) > 9:
        title += "…"
    return title[:80].strip() or "Clip"


def _score_window(
    words: list[Word],
    sentences: list[Sentence],
    start_idx: int,
    end_idx: int,
    energies: list[float] | None,
    visual_cues: list[dict] | None,
    niche_keywords: list[str] | None,
) -> DetectionCandidate:
    start_s = words[start_idx].start
    end_s = words[end_idx - 1].end
    duration = end_s - start_s
    text = _window_text(words, start_idx, end_idx)

    # First 3 s of the window.
    hook_words = [w for w in words[start_idx:end_idx] if w.start - start_s <= 3.0]
    hook_text = " ".join(w.text for w in hook_words)
    hook = score_hook(hook_text, niche_keywords)

    # Last 20% of the window (by time).
    tail_start = end_s - 0.2 * duration
    tail_words = [w for w in words[start_idx:end_idx] if w.start >= tail_start]
    payoff = score_payoff(" ".join(w.text for w in tail_words))

    window_energies = None
    if energies is not None:
        window_energies = [
            energies[i] for i in range(start_idx, end_idx) if i < len(energies)
        ] or None
    peak = score_peak(text, window_energies, duration, end_idx - start_idx)
    emotion = score_emotion(text)

    # Visual cues inside the window.
    visual = 50.0
    if visual_cues:
        in_window = [c for c in visual_cues if start_s <= float(c.get("t", -1)) <= end_s]
        if in_window:
            scene_changes = sum(1 for c in in_window if c.get("scene_change"))
            faces = max((int(c.get("faces") or 0) for c in in_window), default=0)
            visual = score_visual(scene_changes, faces, True)
        else:
            visual = score_visual(0, 0, True)
    elif visual_cues is not None:
        visual = 50.0  # visual list given but empty -> neutral

    starts_boundary = any(s.start_word == start_idx for s in sentences)
    ends_boundary = any(s.end_word == end_idx for s in sentences)
    completeness = score_completeness(
        starts_boundary, ends_boundary, words[start_idx].text, duration
    )

    sub = {
        "hook": hook,
        "peak": peak,
        "payoff": payoff,
        "emotion": emotion,
        "visual": visual,
        "completeness": completeness,
    }
    total = viral_score(sub)
    opening = next(
        (s.text for s in sentences if s.start_word <= start_idx < s.end_word),
        text,
    )
    topic = next(
        (s.topic for s in sentences if s.start_word <= start_idx < s.end_word),
        0,
    )
    topic_words = sorted(_content_words(text))[:8]
    reason = (
        f"Hook {hook:.0f}/100, peak {peak:.0f}/100, payoff {payoff:.0f}/100; "
        f"self-contained {duration:.0f}s span starting “{opening[:60]}”."
    )
    return DetectionCandidate(
        start=round(start_s, 2),
        end=round(end_s, 2),
        score=total,
        hook_score=round(hook, 1),
        peak_score=round(peak, 1),
        payoff_score=round(payoff, 1),
        emotion_score=round(emotion, 1),
        visual_score=round(visual, 1),
        completeness_score=round(completeness, 1),
        title=_hook_title(opening),
        reason=reason,
        topic=topic,
        topic_words=topic_words,
    )


def _time_iou(a: DetectionCandidate, b: DetectionCandidate) -> float:
    overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    union = max(a.end, b.end) - min(a.start, b.start)
    return overlap / union if union > 0 else 0.0


def _topic_jaccard(a: DetectionCandidate, b: DetectionCandidate) -> float:
    set_a, set_b = set(a.topic_words), set(b.topic_words)
    union = set_a | set_b
    return len(set_a & set_b) / len(union) if union else 0.0


def dedupe_and_diversify(
    candidates: list[DetectionCandidate],
    top_n: int = MAX_CANDIDATES,
) -> list[DetectionCandidate]:
    """Dedupe (IoU > 0.5 keeps the higher score) + topic diversity.

    Candidates are taken in score order. A candidate whose topic
    keywords nearly duplicate an already-kept candidate (Jaccard
    > 0.7) is skipped; one that partly duplicates (Jaccard > 0.5)
    loses 20% of its score before the final ranking, so the top
    8-12 are not near-duplicates of each other. Deterministic.
    """
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    kept: list[DetectionCandidate] = []
    for cand in ranked:
        if any(_time_iou(cand, other) > DEDUPE_IOU for other in kept):
            continue
        max_jaccard = max((_topic_jaccard(cand, other) for other in kept), default=0.0)
        if max_jaccard > 0.7:
            continue
        if max_jaccard > 0.5:
            cand = DetectionCandidate(**{**asdict(cand), "score": round(cand.score * 0.8, 1)})
        kept.append(cand)
        if len(kept) >= top_n:
            break
    kept.sort(key=lambda c: c.score, reverse=True)
    return kept


def analyze(
    segments: list[dict],
    duration: float | None = None,
    niche_keywords: list[str] | None = None,
    visual_cues: list[dict] | None = None,
    top_n: int = MAX_CANDIDATES,
) -> list[dict]:
    """Detect clip candidates from word-level transcript segments.

    ``segments``: ``[{start, end, text, energy?, speaker?}]`` — the
    optional per-segment ``energy`` (0-1) feeds the peak score.
    ``duration`` defaults to the last segment's end. ``niche_keywords``
    (optional) lift the hook when present in the first 3 s.
    ``visual_cues`` (optional): ``[{t, scene_change, faces}]``.

    Returns up to ``top_n`` candidate dicts with ``start``, ``end``,
    ``score`` and the six sub-scores, a rule-based ``title`` and a
    ``reason``. Returns [] when there is too little transcript to
    fill even one 15 s clip. Pure function — no I/O, no network.
    """
    words = segments_to_words(segments)
    # A 15 s clip can be cut from as few as ~5 words (slow speech,
    # pauses); the window builder below is the real gate — it only
    # emits windows of 15-60 s. The old <10 gate rejected short but
    # valid transcripts (e.g. the Studio's 9-word, 40 s sample).
    if len(words) < 5:
        return []
    total = float(duration) if duration else words[-1].end
    if total <= MIN_CLIP_S:
        return []

    sentences = assign_topics(segment_sentences(words))
    if not sentences:
        return []

    # Per-word energies, aligned to the flattened word list.
    energies: list[float] | None = None
    per_segment_energy: list[float] = []
    has_energy = False
    for seg in segments or []:
        try:
            energy = seg.get("energy")
        except AttributeError:
            energy = None
        if energy is None:
            per_segment_energy.append(0.0)
        else:
            has_energy = True
            per_segment_energy.append(float(energy))
    if has_energy:
        # Walk segments in order, repeating each segment's energy
        # across its words, aligned to the flattened word list.
        energies = []
        for seg, energy in zip(segments, per_segment_energy):
            try:
                count = len(str(seg.get("text") or "").split())
            except AttributeError:
                count = 0
            energies.extend([energy] * count)
        energies = energies[: len(words)]

    # Sliding windows anchored on sentence boundaries: from each
    # sentence start, extend sentence by sentence while the window
    # stays within 15-60 s; every end that lands in range is a
    # candidate (several lengths per anchor, so the best cut wins).
    candidates: list[DetectionCandidate] = []
    for anchor in sentences:
        start_idx = anchor.start_word
        end_idx = start_idx
        for sent in sentences:
            if sent.start_word < start_idx:
                continue
            span = sent.end_s - anchor.start_s
            if span > MAX_CLIP_S:
                break
            end_idx = sent.end_word
            if span >= MIN_CLIP_S:
                candidates.append(
                    _score_window(
                        words,
                        sentences,
                        start_idx,
                        end_idx,
                        energies,
                        visual_cues,
                        niche_keywords,
                    )
                )
    if not candidates:
        return []
    kept = dedupe_and_diversify(candidates, top_n=max(1, min(top_n, MAX_CANDIDATES)))
    return [c.to_dict() for c in kept]


__all__ = [
    "DEDUPE_IOU",
    "MAX_CANDIDATES",
    "MAX_CLIP_S",
    "MIN_CLIP_S",
    "WEIGHTS",
    "DetectionCandidate",
    "analyze",
    "assign_topics",
    "dedupe_and_diversify",
    "score_completeness",
    "score_emotion",
    "score_hook",
    "score_payoff",
    "score_peak",
    "score_visual",
    "segment_sentences",
    "segments_to_words",
    "viral_score",
]
