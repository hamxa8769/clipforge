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

import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipforge.workflow import ffmpeg_missing_message

_log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(os.environ.get("CLIPFORGE_DATA_DIR", "data")) / "transcripts"


# ------------------------------------------------------------------ memory
# v0.9.3: the in-process faster-whisper model load is the top suspect
# for the server dying abruptly with no traceback (a native-code
# segfault or OOM in the dispatcher thread kills the whole uvicorn
# process instantly). Moving transcription into its own subprocess
# is the real fix and is still open — this guard is the minimum that
# fits now: log the model load with RSS + free memory (so the next
# death is attributable from the log), and REFUSE to load the model
# when free RAM cannot hold it (the job then fails with a plain
# message instead of the server dying). psutil is deliberately NOT a
# dependency: free/RSS are read with stdlib-only calls (ctypes on
# Windows, /proc on Linux), and when memory cannot be measured the
# guard logs and proceeds rather than blocking valid runs.
_WHISPER_MODEL_MB: dict[str, int] = {
    "tiny": 75,
    "base": 150,
    "small": 500,
    "medium": 1500,
    "large": 3000,
    "large-v2": 3000,
    "large-v3": 3000,
    "turbo": 800,
}
# Headroom to keep free after the model is loaded (CTranslate2
# workspace + the audio + the process itself).
_MIN_FREE_MB_AFTER_LOAD = 512


def _free_memory_bytes() -> int | None:
    """Free RAM in bytes, or None when it cannot be measured here."""
    try:
        import psutil  # noqa: PLC0415  (optional: only when installed)

        return int(psutil.virtual_memory().available)
    except Exception:  # noqa: BLE001  (psutil absent or broken: fall through)
        pass
    if sys.platform == "win32":
        try:

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except Exception:  # noqa: BLE001  (best effort only)
            pass
        return None
    try:
        # Linux: /proc/meminfo MemAvailable (kB) is the honest number
        # (free + reclaimable cache), not MemFree.
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001  (not Linux, or /proc unavailable)
        pass
    return None


def _rss_bytes() -> int | None:
    """This process's CURRENT resident memory in bytes, or None."""
    if sys.platform == "win32":
        try:

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return int(counters.WorkingSetSize)
        except Exception:  # noqa: BLE001  (best effort only)
            pass
        return None
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001  (not Linux, or /proc unavailable)
        pass
    return None


def _fmt_mb(n: int | None) -> str:
    return f"{n // (1024 * 1024)} MB" if n is not None else "unknown"


def _check_memory_for_model(model: str) -> None:
    """Refuse to load a faster-whisper model the machine cannot hold.

    Raises RuntimeError (the worker turns it into a failed job with
    the plain message — never a dead server) when free RAM is below
    the model's size plus headroom. When free memory cannot be
    measured on this platform, logs and proceeds: refusing blind
    would block valid runs.
    """
    need_mb = _WHISPER_MODEL_MB.get(model, 500)
    free = _free_memory_bytes()
    if free is None:
        _log.warning(
            "transcription memory check unavailable on this platform; "
            "loading faster-whisper model %s without a free-memory check",
            model,
        )
        return
    need_bytes = (need_mb + _MIN_FREE_MB_AFTER_LOAD) * 1024 * 1024
    if free < need_bytes:
        raise RuntimeError(
            f"Not enough free memory to load the faster-whisper '{model}' model "
            f"(needs about {need_mb} MB plus {_MIN_FREE_MB_AFTER_LOAD} MB headroom; "
            f"only {_fmt_mb(free)} free). Free memory (close other apps) or use "
            "the Whisper API key path / a smaller model, then retry the job."
        )


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
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        # ffmpeg binary absent: say so plainly instead of leaking a
        # traceback-style "[Errno 2] No such file or directory: 'ffmpeg'".
        # The worker turns this into a blocked job (see worker.py).
        raise RuntimeError(ffmpeg_missing_message()) from None
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {proc.stderr.strip()}")
    return Path(tmp.name)


def _transcribe_faster_whisper(source: Path, model: str, language: str | None) -> Transcript:
    # v0.9.3: check BEFORE importing the package or extracting audio
    # — a refusal here fails the job with a plain message instead of
    # OOM-killing the whole server mid-load. The load itself is
    # logged with RSS + free memory so an abrupt death during it is
    # attributable from the server log.
    _check_memory_for_model(model)
    _log.info(
        "loading faster-whisper model %s (process RSS %s, free memory %s)",
        model,
        _fmt_mb(_rss_bytes()),
        _fmt_mb(_free_memory_bytes()),
    )
    from faster_whisper import WhisperModel  # lazy: optional dependency

    audio = _extract_audio_wav(source)
    try:
        fw = WhisperModel(model, device="auto", compute_type="int8")
        _log.info(
            "faster-whisper model %s loaded (process RSS %s, free memory %s)",
            model,
            _fmt_mb(_rss_bytes()),
            _fmt_mb(_free_memory_bytes()),
        )
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
