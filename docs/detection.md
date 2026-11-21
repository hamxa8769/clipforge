# Clip detection — how the viral score is computed (v0.8.0)

ClipForge Pro detects clip candidates from a Whisper-style transcript
(word-level timestamps). The engine is deterministic and rule-based:
no network calls, no model downloads. Gemini (when connected) only
rewrites titles — it never changes the score.

Source: `src/clipforge/detection.py`. Tests: `tests/unit/test_detection.py`.

## Pipeline

1. **Word-level transcript** — segments `{start, end, text, energy?,
   speaker?}` are flattened into timed words (words spread evenly
   across each segment's span).
2. **Sentence segmentation** — words are split into sentences on
   `[.!?]` boundaries. A trailing run without terminal punctuation is
   still a sentence, so no words are dropped.
3. **Topic grouping** — sentences are grouped into topics by
   content-word Jaccard overlap, so a change of subject starts a new
   topic.
4. **Sliding windows** — from each sentence start, extend sentence by
   sentence while the window stays within 15–60 s. Every end that
   lands in range is a candidate (several lengths per anchor, so the
   best cut wins).
5. **Scoring** — each candidate is scored on six sub-scores (below).
6. **Dedup + diversity** — candidates with time IoU > 0.5 keep only
   the higher score; a candidate whose topic keywords nearly duplicate
   an already-kept, higher-scoring candidate is penalised. The top
   8–12 survive, so they are not near-duplicates.

## The viral score (0–100)

```
viral = 0.25*hook + 0.25*peak + 0.15*payoff
      + 0.10*emotion + 0.10*visual + 0.15*completeness
```

Each sub-score is 0–100. The weights are pinned in code as
`detection.WEIGHTS` and by test.

| Signal | Weight | What it measures |
| --- | --- | --- |
| **hook** | 0.25 | First 3 s: a question (+25), a bold claim (+25), an emotion word (+20), a number (+15), a direct "you" address (+15), a niche keyword (+10, max 100) |
| **peak** | 0.25 | Audio energy / loudness / pace inside the window. Uses the caller-supplied per-segment `energy` (mean × 50) when present; without it, degrades gracefully to pace + emphasis words + laughter markers — it never invents energy |
| **payoff** | 0.15 | Concluding / insight language in the last 20% of the window ("so", "that's why", "the key", summary phrases — 40 pts per hit) |
| **emotion** | 0.10 | Sentiment-lexicon density (positive/negative arousal words) × 800, capped at 100 |
| **visual** | 0.10 | Scene changes + face presence from optional visual cues; 50 (neutral) when no visual data is given |
| **completeness** | 0.15 | Self-contained: starts/ends on a sentence boundary, no dangling pronoun at the start, duration inside 15–60 s (−25 per non-boundary, −20 dangling pronoun, −30 out-of-range duration) |

## Hook titles

Rule-based titles are built from the window's opening sentence
(question → the question; bold claim → the claim; otherwise the
first 8 words). Gemini, when connected, can rewrite them; the
rule-based title is always the fallback.

## Rights guardrail

Detection runs ONLY on the user's own videos or Creative Commons /
licensed / public-domain videos. This module never downloads or
fetches a video — it scores transcript segments the caller already
has. It must not be wired to any source that is another creator's
standard-licensed video.
