"""Viral-moment selection for ClipForge.

Borrowed *ideas* (own implementation, no copied code) from the two MIT
reuse references in the research report (§2, §9b):

* Anil-matcha/AI-Youtube-Shorts-Generator — the virality framework:
  hook / emotional peak / opinion bomb / revelation / conflict /
  quotable line / story peak / practical value; score 0–100, hook 1–10,
  hook line + one-sentence reason; merge, re-rank, dedupe >50% overlap.
* zhouxiaoka/autoclip — chunking the transcript into ~9-minute windows
  with 15 s overlap so boundary moments are never cut, and scoring up
  to 10 clips per source.

Key correctness rule (from artbyjazi/autoclip, cited in report §9b):
clip boundaries are **word indices**, never timestamp arithmetic by
the LLM — the model copies indices reliably and does arithmetic badly.
All start/end *seconds* are derived here from the transcript words.

Two paths:

* LLM — strict-JSON call to an OpenAI-compatible endpoint when
  ``OPENAI_API_KEY`` (and optionally ``OPENAI_BASE_URL`` /
  ``CLIPFORGE_LLM_MODEL``) is configured. The prompt asks for word
  indices only; this module converts them to times and validates them.
  The system prompt is externalised in
  ``prompts/highlight_system.md`` (``load_highlight_prompt``) so it can
  be tuned without touching code; override it with
  ``CLIPFORGE_HIGHLIGHT_PROMPT``.
* Heuristic fallback — deterministic keyword / position / signal
  scoring, no model and no key. Pure functions, unit-testable without
  FFmpeg or an LLM.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipforge.transcribe import Transcript, Word

# The LLM highlight prompt lives in a file (prompts/highlight_system.md)
# so it can be tuned without touching code — the "prompts as files"
# pattern borrowed from zhouxiaoka/autoclip (MIT, see docs/REUSE.md).
# Set CLIPFORGE_HIGHLIGHT_PROMPT to your own prompt file to override it.
_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "highlight_system.md"
_FALLBACK_PROMPT = (
    "You select viral short-form clips from a word-level transcript. "
    'Return STRICT JSON only: {"candidates": [{"start_word_index": int, '
    '"end_word_index": int, "score": number, "hook": int, '
    '"hook_line": string, "reason": string}]}. '
    "Rules: indices refer to the numbered word list, start < end, each clip "
    "15-60 seconds long by the word timestamps, self-contained, hook rated "
    "1-10 on the first 3 seconds, score 0-100. Score on: hook, emotional "
    "peak, opinion bomb / contrarian take, revelation, conflict, quotable "
    "line, story peak, practical value. Never do timestamp arithmetic — "
    "use word indices only. At most 10 candidates."
)


def load_highlight_prompt(path: str | Path | None = None) -> str:
    """Load the LLM highlight system prompt from a file.

    ``path`` overrides; otherwise ``CLIPFORGE_HIGHLIGHT_PROMPT`` wins,
    then the shipped ``prompts/highlight_system.md``. The prompt file
    is a Markdown doc whose ``## Instructions`` section is the prompt;
    if that section is absent the whole file is used. Any read error
    falls back to the built-in prompt — a missing prompt file must
    never fail a job.
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    if os.environ.get("CLIPFORGE_HIGHLIGHT_PROMPT"):
        candidates.append(Path(os.environ["CLIPFORGE_HIGHLIGHT_PROMPT"]))
    candidates.append(_PROMPT_FILE)
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        marker = "## Instructions"
        if marker in text:
            text = text.split(marker, 1)[1]
        text = text.strip()
        if text:
            return text
    return _FALLBACK_PROMPT


# Chunking (report §2 stage 3).
CHUNK_WINDOW_S = 9 * 60  # 9 minutes
CHUNK_OVERLAP_S = 15.0
# Clip shape targets (report §4: 15–35 s optimal, 35–60 s tutorials).
MIN_CLIP_S = 15.0
MAX_CLIP_S = 60.0
TARGET_CLIP_S = 30.0
MAX_CANDIDATES = 10
DEDUPE_OVERLAP = 0.5  # >50% overlap -> keep the higher score

# Heuristic signal lexicons (own lists, inspired by the framework above).
HOOK_WORDS = {
    "stop",
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
    "free",
    "imagine",
    "look",
    "watch",
    "listen",
    "here's",
    "this",
    "why",
    "how",
    "what",
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
}
OPINION_WORDS = {
    "wrong",
    "overrated",
    "underrated",
    "best",
    "worst",
    "should",
    "shouldn't",
    "never",
    "always",
    "everyone",
    "nobody",
    "lie",
    "myth",
    "scam",
    "fake",
    "honestly",
    "controversial",
    "unpopular",
}
REVELATION_WORDS = {
    "discovered",
    "found",
    "realised",
    "realized",
    "revealed",
    "secret",
    "hidden",
    "actually",
    "turns",
    "truth",
    "nobody",
    "tells",
}
CONFLICT_WORDS = {
    "but",
    "however",
    "fight",
    "argued",
    "disagree",
    "versus",
    "vs",
    "battle",
    "problem",
    "failed",
    "mistake",
    "wrong",
}
PRACTICAL_WORDS = {
    "how",
    "step",
    "tips",
    "trick",
    "guide",
    "exactly",
    "first",
    "second",
    "third",
    "do",
    "use",
    "try",
    "learn",
    "fix",
    "save",
    "make",
    "build",
}
SIGNAL_LEXICONS = {
    "hook": HOOK_WORDS,
    "emotional_peak": EMOTION_WORDS,
    "opinion_bomb": OPINION_WORDS,
    "revelation": REVELATION_WORDS,
    "conflict": CONFLICT_WORDS,
    "practical_value": PRACTICAL_WORDS,
}
# Quotable / story signals are structural, not lexical (see scoring).
QUESTION_RE = re.compile(r"\b(what|why|how|when|where|who)\b", re.IGNORECASE)


@dataclass
class Candidate:
    """One scored clip candidate. Boundaries are word indices first."""

    start_word_index: int
    end_word_index: int
    start_s: float
    end_s: float
    score: float  # 0–100
    hook: int  # 1–10, first ~3 seconds
    hook_line: str
    reason: str
    signals: dict[str, float] = field(default_factory=dict)
    source: str = "heuristic"  # "heuristic" | "llm"

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Chunk:
    """One transcript window, expressed in word-index space."""

    start_word_index: int
    end_word_index: int
    start_s: float
    end_s: float


def chunk_transcript(
    transcript: Transcript,
    window_s: float = CHUNK_WINDOW_S,
    overlap_s: float = CHUNK_OVERLAP_S,
) -> list[Chunk]:
    """Split a transcript into ~9-min windows with 15 s overlap.

    Pure function. Windows are cut on word boundaries; the overlap
    guarantees a moment straddling a boundary appears whole in at
    least one window.
    """
    words = transcript.words
    if not words:
        return []
    total = words[-1].end
    chunks: list[Chunk] = []
    start_t = 0.0
    while start_t < total:
        end_t = min(start_t + window_s, total)
        start_idx = next((i for i, w in enumerate(words) if w.end > start_t), 0)
        end_idx = next((i for i, w in enumerate(words) if w.start >= end_t), len(words))
        end_idx = max(end_idx, start_idx + 1)
        chunks.append(Chunk(start_idx, end_idx, words[start_idx].start, words[end_idx - 1].end))
        if end_t >= total:
            break
        start_t = end_t - overlap_s
    return chunks


def _signal_scores(words: list[Word]) -> dict[str, float]:
    """Per-signal density (0–1) for a word span. Pure function."""
    text = " ".join(w.text for w in words).lower()
    tokens = re.findall(r"[a-z']+", text) or [""]
    scores: dict[str, float] = {}
    for name, lexicon in SIGNAL_LEXICONS.items():
        hits = sum(1 for tok in tokens if tok in lexicon)
        scores[name] = min(1.0, hits / max(4, len(tokens) * 0.15))
    # Quotable: short, punchy, self-contained sentence(s).
    joined = " ".join(w.text for w in words)
    sentences = [s for s in re.split(r"[.!?]+", joined) if s.strip()]
    short = [s for s in sentences if 4 <= len(s.split()) <= 18]
    scores["quotable"] = min(1.0, len(short) / 2.0)
    # Story peak: narrative markers + a length that can hold an arc.
    story_hits = len(
        re.findall(r"\b(then|finally|so|after|when|until|suddenly|because)\b", text, re.IGNORECASE)
    )
    scores["story"] = min(1.0, story_hits / 4.0)
    return scores


def score_span(
    words: list[Word], span_start: int, span_end: int
) -> tuple[float, int, dict[str, float]]:
    """Score words[span_start:span_end] -> (score 0–100, hook 1–10, signals).

    Pure function. Weights follow the framework: the opening hook and
    emotional/opinion peaks dominate; practical value and quotability
    lift a solid span; length near the 30 s target is preferred.
    """
    span = words[span_start:span_end]
    if not span:
        return 0.0, 1, {}
    signals = _signal_scores(span)
    duration = span[-1].end - span[0].start
    # Hook: first ~3 s of the span.
    hook_words = [w for w in span if w.start - span[0].start <= 3.0]
    hook_signals = _signal_scores(hook_words) if hook_words else {}
    hook_strength = max(
        hook_signals.get("hook", 0.0),
        hook_signals.get("opinion_bomb", 0.0),
        hook_signals.get("revelation", 0.0),
        0.35 if QUESTION_RE.search(" ".join(w.text for w in hook_words)) else 0.0,
    )
    hook = max(1, min(10, int(round(1 + hook_strength * 9))))

    weights = {
        "hook": 0.18,
        "emotional_peak": 0.16,
        "opinion_bomb": 0.14,
        "revelation": 0.12,
        "conflict": 0.10,
        "quotable": 0.10,
        "story": 0.08,
        "practical_value": 0.12,
    }
    raw = sum(signals.get(k, 0.0) * w for k, w in weights.items())
    # Length shaping: full credit near 30 s, tapering to the 15–60 s bounds.
    if duration < MIN_CLIP_S:
        raw *= 0.55 + 0.45 * (duration / MIN_CLIP_S)
    elif duration > MAX_CLIP_S:
        raw *= max(0.5, 1.0 - (duration - MAX_CLIP_S) / MAX_CLIP_S)
    else:
        raw *= 0.9 + 0.1 * (1.0 - abs(duration - TARGET_CLIP_S) / TARGET_CLIP_S)
    score = round(min(100.0, max(0.0, raw * 100 * 1.35)), 1)
    return score, hook, signals


def _words_to_candidate(words: list[Word], start_idx: int, end_idx: int, source: str) -> Candidate:
    score, hook, signals = score_span(words, start_idx, end_idx)
    span_text = " ".join(w.text for w in words[start_idx:end_idx])
    hook_line = span_text[:120].strip()
    top_signal = max(signals, key=signals.get) if signals else "practical_value"
    reason = (
        f"Scores on {top_signal.replace('_', ' ')} "
        f"({signals.get(top_signal, 0):.2f}) with a {hook}/10 opening hook; "
        f"self-contained {words[end_idx - 1].end - words[start_idx].start:.0f}s span."
    )
    return Candidate(
        start_word_index=start_idx,
        end_word_index=end_idx,
        start_s=words[start_idx].start,
        end_s=words[end_idx - 1].end,
        score=score,
        hook=hook,
        hook_line=hook_line,
        reason=reason,
        signals={k: round(v, 3) for k, v in signals.items()},
        source=source,
    )


def find_heuristic_candidates(
    transcript: Transcript,
    top_n: int = MAX_CANDIDATES,
    target_s: float = TARGET_CLIP_S,
    step_words: int = 20,
) -> list[Candidate]:
    """Sliding-window heuristic candidates over the whole transcript.

    Pure function (no I/O). Windows start every ``step_words`` words and
    run to ~``target_s`` seconds, snapping the end to a sentence boundary
    when one is nearby; results are deduped and ranked.
    """
    words = transcript.words
    if len(words) < 10:
        return []
    candidates: list[Candidate] = []
    for start_idx in range(0, len(words), step_words):
        # Grow the window to the target duration.
        end_idx = start_idx + 1
        while end_idx < len(words) and words[end_idx].end - words[start_idx].start < target_s:
            end_idx += 1
        # Snap the end to a sentence boundary within ~8 words, if any.
        for j in range(end_idx, min(end_idx + 8, len(words))):
            if words[j].text.endswith((".", "!", "?")):
                end_idx = j + 1
                break
        if words[end_idx - 1].end - words[start_idx].start < MIN_CLIP_S:
            continue
        candidates.append(_words_to_candidate(words, start_idx, end_idx, "heuristic"))
    return dedupe_and_rank(candidates, top_n=top_n)


def overlap_fraction(a: Candidate, b: Candidate) -> float:
    """Time overlap of two candidates as a fraction of the shorter one."""
    overlap = max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
    shorter = min(a.duration_s, b.duration_s)
    return overlap / shorter if shorter > 0 else 0.0


def dedupe_and_rank(candidates: list[Candidate], top_n: int = MAX_CANDIDATES) -> list[Candidate]:
    """Merge, re-rank by score, dedupe >50% overlap keeping higher score."""
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    kept: list[Candidate] = []
    for cand in ranked:
        if any(overlap_fraction(cand, other) > DEDUPE_OVERLAP for other in kept):
            continue
        kept.append(cand)
        if len(kept) >= top_n:
            break
    return kept


# ---------------------------------------------------------------- LLM path

LLM_SYSTEM_PROMPT = load_highlight_prompt()


def _llm_candidates_for_chunk(
    words: list[Word], chunk: Chunk, api_key: str, base_url: str, model: str
) -> list[Candidate]:
    numbered = "\n".join(
        f"{i}: {words[i].text} [{words[i].start:.2f}-{words[i].end:.2f}]"
        for i in range(chunk.start_word_index, chunk.end_word_index)
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Transcript words {chunk.start_word_index}–{chunk.end_word_index - 1} "
                    f"({chunk.start_s:.1f}s–{chunk.end_s:.1f}s):\n{numbered}\n"
                    "Return the JSON object only."
                ),
            },
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    out: list[Candidate] = []
    for item in parsed.get("candidates", [])[:MAX_CANDIDATES]:
        try:
            s_idx, e_idx = int(item["start_word_index"]), int(item["end_word_index"])
            score = float(item["score"])
            hook = int(item["hook"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= s_idx < e_idx <= len(words)):
            continue
        duration = words[e_idx - 1].end - words[s_idx].start
        if not (MIN_CLIP_S - 2 <= duration <= MAX_CLIP_S + 10):
            continue
        _, _, signals = score_span(words, s_idx, e_idx)
        out.append(
            Candidate(
                start_word_index=s_idx,
                end_word_index=e_idx,
                start_s=words[s_idx].start,
                end_s=words[e_idx - 1].end,
                score=max(0.0, min(100.0, score)),
                hook=max(1, min(10, hook)),
                hook_line=str(item.get("hook_line", ""))[:200],
                reason=str(item.get("reason", ""))[:300],
                signals={k: round(v, 3) for k, v in signals.items()},
                source="llm",
            )
        )
    return out


def find_candidates(
    transcript: Transcript,
    top_n: int = MAX_CANDIDATES,
    use_llm: bool | None = None,
) -> list[Candidate]:
    """Top-N candidates: LLM when configured, else heuristic fallback.

    ``use_llm=None`` (default) means: use the LLM only if
    ``OPENAI_API_KEY`` is set; any LLM failure falls back to the
    heuristic rather than failing the job.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    want_llm = api_key if use_llm is None else bool(use_llm and api_key)
    if not want_llm:
        return find_heuristic_candidates(transcript, top_n=top_n)
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("CLIPFORGE_LLM_MODEL", "gpt-4o-mini")
    all_candidates: list[Candidate] = []
    try:
        for chunk in chunk_transcript(transcript):
            all_candidates.extend(
                _llm_candidates_for_chunk(transcript.words, chunk, api_key, base_url, model)
            )
    except Exception:
        # LLM is an enhancement, never a single point of failure.
        return find_heuristic_candidates(transcript, top_n=top_n)
    if not all_candidates:
        return find_heuristic_candidates(transcript, top_n=top_n)
    return dedupe_and_rank(all_candidates, top_n=top_n)
