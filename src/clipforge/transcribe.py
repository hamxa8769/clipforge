"""Word-level transcription for ClipForge.

Backend priority (first available wins):

1. ``faster-whisper`` (local, if installed) — preferred: no audio leaves
   the machine, word timestamps included.
2. OpenAI Whisper API (``whisper-1``) when ``OPENAI_API_KEY`` is set —
   audio is extracted with FFmpeg and posted via the standard library
   (no ``openai`` package required).
3. Deterministic fallback: a caller-supplied transcript text is spread
   across the known duration into timed words. This keeps the pipeline
   smoke-testable and unit-testable with no model and no key, and is
   what the v0.1 smoke test uses. It is *not* real transcription and
   its output is labelled ``engine="fallback-text"``.

Transcripts are cached as JSON under ``data/transcripts/<sha256>.json``
(keyed by source hash, per report §2 stage 2 — never re-transcribe).
The cache directory is overridable via ``CLIPFORGE_DATA_DIR`` or the
``cache_dir`` argument.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CACHE_DIR = Path(os.environ.get("CLIPFORGE_DATA_DIR", "data")) / "transcripts"


@dataclass
class Word:
    """One transcribed word with its time span (seconds)."""

    text: str
    start: float
    end: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Segment:
    """A contiguous run of words (roughly one sentence / utterance)."""

    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }


@dataclass
class Transcript:
    """Full word-level transcript of one source."""

    words: list[Word] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    language: str = "en"
    duration_s: float = 0.0
    engine: str = "fallback-text"  # faster-whisper | whisper-api | fallback-text
    source_sha256: str = ""

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def to_dict(self) -> dict:
        return {
            "words": [w.to_dict() for w in self.words],
            "segments": [s.to_dict() for s in self.segments],
            "language": self.language,
            "duration_s": self.duration_s,
            "engine": self.engine,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Transcript:
        words = [Word(**w) for w in data.get("words", [])]
        segments = []
        for seg in data.get("segments", []):
            segments.append(Segment(words=[Word(**w) for w in seg.get("words", [])]))
        if not segments and words:
            segments = _group_words_into_segments(words)
        return cls(
            words=words,
            segments=segments,
            language=data.get("language", "en"),
            duration_s=float(data.get("duration_s", 0.0)),
            engine=data.get("engine", "fallback-text"),
            source_sha256=data.get("source_sha256", ""),
        )


def _group_words_into_segments(words: list[Word], max_gap_s: float = 1.0) -> list[Segment]:
    """Split words into segments at gaps and sentence-ending punctuation."""
    segments: list[Segment] = []
    current: list[Word] = []
    for word in words:
        if current and word.start - current[-1].end > max_gap_s:
            segments.append(Segment(words=current))
            current = []
        current.append(word)
        if word.text.endswith((".", "!", "?")):
            segments.append(Segment(words=current))
            current = []
    if current:
        segments.append(Segment(words=current))
    return segments


def cache_path_for(source_sha256: str, cache_dir: str | Path | None = None) -> Path:
    base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    return base / f"{source_sha256}.json"


def load_cached_transcript(
    source_sha256: str, cache_dir: str | Path | None = None
) -> Transcript | None:
    """Return the cached transcript for a source hash, or None."""
    path = cache_path_for(source_sha256, cache_dir)
    if not path.exists():
        return None
    try:
        return Transcript.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def save_transcript(transcript: Transcript, cache_dir: str | Path | None = None) -> Path:
    """Persist a transcript under its source hash; returns the cache path."""
    path = cache_path_for(transcript.source_sha256, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript.to_dict(), indent=2), encoding="utf-8")
    return path


def fallback_transcript_from_text(
    text: str, duration_s: float, source_sha256: str = ""
) -> Transcript:
    """Deterministic timed words from a supplied transcript text.

    Words are spread evenly across ``duration_s`` (each word gets an
    equal slice). Pure function — no FFmpeg, no model, no network.
    """
    tokens = text.split()
    if not tokens or duration_s <= 0:
        return Transcript(
            words=[],
            segments=[],
            duration_s=duration_s,
            engine="fallback-text",
            source_sha256=source_sha256,
        )
    per_word = duration_s / len(tokens)
    words = [
        Word(text=tok, start=i * per_word, end=(i + 1) * per_word) for i, tok in enumerate(tokens)
    ]
    return Transcript(
        words=words,
        segments=_group_words_into_segments(words),
        duration_s=duration_s,
        engine="fallback-text",
        source_sha256=source_sha256,
    )


def _extract_audio_wav(source: Path) -> Path:
    """Extract 16 kHz mono WAV for Whisper; caller deletes the temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        tmp.name,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {proc.stderr.strip()}")
    return Path(tmp.name)


def _transcribe_faster_whisper(source: Path, model: str, language: str | None) -> Transcript:
    from faster_whisper import WhisperModel  # lazy: optional dependency

    audio = _extract_audio_wav(source)
    try:
        fw = WhisperModel(model, device="auto", compute_type="int8")
        segments, info = fw.transcribe(str(audio), language=language, word_timestamps=True)
        words: list[Word] = []
        for seg in segments:
            for w in seg.words or []:
                words.append(Word(text=w.word.strip(), start=float(w.start), end=float(w.end)))
        detected = getattr(info, "language", None) or language or "en"
        duration = words[-1].end if words else 0.0
        return Transcript(
            words=words,
            segments=_group_words_into_segments(words),
            language=detected,
            duration_s=duration,
            engine="faster-whisper",
        )
    finally:
        audio.unlink(missing_ok=True)


def _transcribe_whisper_api(source: Path, api_key: str, base_url: str, model: str) -> Transcript:
    """OpenAI-compatible Whisper API via stdlib urllib (multipart form)."""
    audio = _extract_audio_wav(source)
    try:
        boundary = "----clipforge-whisper-boundary"
        body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="timestamp_granularities[]"\r\n\r\nword\r\n'
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode()
            + audio.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/audio/transcriptions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        with urllib.request.urlopen(req, timeout=1800) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        words = [
            Word(text=w["word"].strip(), start=float(w["start"]), end=float(w["end"]))
            for w in payload.get("words", [])
        ]
        duration = float(payload.get("duration", words[-1].end if words else 0.0))
        return Transcript(
            words=words,
            segments=_group_words_into_segments(words),
            language=payload.get("language", "en"),
            duration_s=duration,
            engine="whisper-api",
        )
    finally:
        audio.unlink(missing_ok=True)


def transcribe(
    source: str | Path,
    *,
    source_sha256: str = "",
    duration_s: float = 0.0,
    transcript_text: str | None = None,
    model: str = "small",
    language: str | None = None,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    force_engine: str | None = None,
) -> Transcript:
    """Transcribe ``source`` to a word-level :class:`Transcript`.

    ``force_engine`` may be ``"faster-whisper"``, ``"whisper-api"`` or
    ``"fallback-text"`` (useful for tests / smoke runs). Otherwise the
    backend is picked automatically: faster-whisper if installed, else
    the Whisper API if ``OPENAI_API_KEY`` is set, else the fallback —
    which *requires* ``transcript_text`` and raises otherwise.
    """
    path = Path(source)
    if use_cache and source_sha256:
        cached = load_cached_transcript(source_sha256, cache_dir)
        if cached is not None:
            return cached

    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    engine = force_engine
    if engine is None:
        try:
            import faster_whisper  # noqa: F401

            engine = "faster-whisper"
        except ImportError:
            engine = "whisper-api" if api_key else "fallback-text"

    if engine == "faster-whisper":
        result = _transcribe_faster_whisper(path, model, language)
    elif engine == "whisper-api":
        if not api_key:
            raise RuntimeError("whisper-api engine selected but OPENAI_API_KEY is not set")
        result = _transcribe_whisper_api(
            path, api_key, base_url, os.environ.get("CLIPFORGE_WHISPER_MODEL", "whisper-1")
        )
    elif engine == "fallback-text":
        if transcript_text is None:
            raise RuntimeError(
                "no transcription backend available (faster-whisper not "
                "installed, OPENAI_API_KEY not set) and no transcript_text "
                "was supplied for the fallback engine"
            )
        result = fallback_transcript_from_text(transcript_text, duration_s, source_sha256)
    else:
        raise ValueError(f"unknown transcription engine: {engine!r}")

    result.source_sha256 = source_sha256
    if duration_s and not result.duration_s:
        result.duration_s = duration_s
    if use_cache and source_sha256:
        save_transcript(result, cache_dir)
    return result
