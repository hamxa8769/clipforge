"""End-to-end clipping pipeline for ClipForge (synchronous, v0.1).

``run_pipeline`` orchestrates ingest -> transcribe -> highlights ->
(top N) -> reframe + captions + render, and returns a JSON-serialisable
job result with candidates and rendered paths.

Queue/worker integration is another agent's job — this module exposes
one clean synchronous function that a worker can call. No network is
used unless the caller enables the LLM / Whisper-API paths (via env)
or passes a YouTube URL with ``own_content=True``.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from clipforge.captions import STYLES
from clipforge.highlights import find_candidates
from clipforge.ingest import SourceInfo, ingest_local, ingest_youtube
from clipforge.reframe import compute_crop_path
from clipforge.render import RenderError, render_clip
from clipforge.transcribe import transcribe

DEFAULT_DATA_DIR = Path(os.environ.get("CLIPFORGE_DATA_DIR", "data"))


def run_pipeline(
    source_path: str | Path | None = None,
    *,
    youtube_url: str | None = None,
    own_content: bool = False,
    top_n: int = 3,
    caption_style: str = "bold-pop",
    blurred_background: bool = False,
    transcript_text: str | None = None,
    transcribe_engine: str | None = None,
    use_llm: bool | None = None,
    out_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> dict:
    """Run the full clipping pipeline on one source.

    Either ``source_path`` (local mp4/mov) or ``youtube_url`` (requires
    ``own_content=True``) must be given. ``transcript_text`` supplies
    the deterministic fallback transcript when no Whisper backend is
    available; ``transcribe_engine`` forces one backend
    (``"faster-whisper"`` / ``"whisper-api"`` / ``"fallback-text"``).
    Returns a JSON-serialisable dict; per-clip render failures are
    recorded on the clip entry instead of failing the whole job.
    """
    if caption_style not in STYLES:
        raise ValueError(f"unknown caption style {caption_style!r}; choose from {sorted(STYLES)}")
    if bool(source_path) == bool(youtube_url):
        raise ValueError("pass exactly one of source_path or youtube_url")
    job_id = uuid.uuid4().hex[:12]
    started = time.time()
    data = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    out = Path(out_dir) if out_dir else data / "clips" / job_id
    out.mkdir(parents=True, exist_ok=True)

    if youtube_url:
        source: SourceInfo = ingest_youtube(
            youtube_url,
            own_content=own_content,
            out_dir=data / "uploads",
        )
    else:
        source = ingest_local(str(source_path))

    transcript = transcribe(
        source.path,
        source_sha256=source.sha256,
        duration_s=source.duration_s,
        transcript_text=transcript_text,
        force_engine=transcribe_engine,
        cache_dir=data / "transcripts",
    )
    candidates = find_candidates(transcript, top_n=top_n, use_llm=use_llm)

    rendered: list[dict] = []
    for i, cand in enumerate(candidates):
        entry: dict = {"rank": i + 1, "candidate": cand.to_dict()}
        try:
            plan = compute_crop_path(
                str(source.path),
                source.duration_s,
                source.width,
                source.height,
                start_s=cand.start_s,
                end_s=cand.end_s,
                blurred_background=blurred_background,
            )
            result = render_clip(
                source.path,
                cand,
                out / f"clip_{i + 1:02d}.mp4",
                style=caption_style,
                words=transcript.words,
                plan=plan,
                blurred_background=blurred_background,
            )
            entry["render"] = result.to_dict()
            entry["status"] = "rendered"
        except (RenderError, Exception) as exc:  # per-clip failure != job failure
            entry["status"] = "failed"
            entry["error"] = str(exc)
        rendered.append(entry)

    return {
        "job_id": job_id,
        "engine": "clipforge 0.2.0",
        "elapsed_s": round(time.time() - started, 2),
        "source": source.to_dict(),
        "transcript": {
            "engine": transcript.engine,
            "language": transcript.language,
            "word_count": len(transcript.words),
            "duration_s": transcript.duration_s,
            "cached": bool(transcript.source_sha256),
        },
        "candidates": [c.to_dict() for c in candidates],
        "clips": rendered,
        "rendered_count": sum(1 for c in rendered if c["status"] == "rendered"),
        "out_dir": str(out),
    }
