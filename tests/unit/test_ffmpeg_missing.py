"""Unit tests for the FFmpeg-missing paths (v0.4.0).

A missing ffmpeg/ffprobe binary must surface one plain-language error
("FFmpeg is missing — install it from https://ffmpeg.org/download.html")
at every layer that shells out to it — never a bare FileNotFoundError
and never a silent empty result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.ingest import IngestError, ffprobe_info
from clipforge.render import RenderError, _run, ffprobe_verify
from clipforge.transcribe import _extract_audio_wav
from clipforge.workflow import FFMPEG_DOWNLOAD_URL, ffmpeg_missing_message


def _raise_file_not_found(*args, **kwargs):
    raise FileNotFoundError(2, "No such file or directory", "ffmpeg")


def test_ffmpeg_missing_message_mentions_url() -> None:
    assert FFMPEG_DOWNLOAD_URL in ffmpeg_missing_message()
    assert "FFmpeg is missing" in ffmpeg_missing_message()
    assert FFMPEG_DOWNLOAD_URL in ffmpeg_missing_message("ffprobe")
    assert "ffprobe is missing" in ffmpeg_missing_message("ffprobe")


def test_transcribe_audio_extraction_names_missing_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import clipforge.transcribe as transcribe_mod

    monkeypatch.setattr(transcribe_mod.subprocess, "run", _raise_file_not_found)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake")
    with pytest.raises(RuntimeError) as excinfo:
        _extract_audio_wav(source)
    assert "FFmpeg is missing" in str(excinfo.value)
    assert FFMPEG_DOWNLOAD_URL in str(excinfo.value)
    assert "FileNotFoundError" not in str(excinfo.value)
    assert "Errno 2" not in str(excinfo.value)


def test_render_run_wrapper_names_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.render as render_mod

    monkeypatch.setattr(render_mod.subprocess, "run", _raise_file_not_found)
    with pytest.raises(RenderError) as excinfo:
        _run(["ffmpeg", "-version"], timeout=5, tool="ffmpeg")
    assert "FFmpeg is missing" in str(excinfo.value)
    assert FFMPEG_DOWNLOAD_URL in str(excinfo.value)
    with pytest.raises(RenderError) as excinfo:
        _run(["ffprobe", "-version"], timeout=5, tool="ffprobe")
    assert "ffprobe is missing" in str(excinfo.value)


def test_render_ffprobe_verify_names_missing_ffprobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import clipforge.render as render_mod

    monkeypatch.setattr(render_mod.subprocess, "run", _raise_file_not_found)
    with pytest.raises(RenderError, match="ffprobe is missing"):
        ffprobe_verify(tmp_path / "clip.mp4")


def test_ingest_ffprobe_info_names_missing_ffprobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import clipforge.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.subprocess, "run", _raise_file_not_found)
    with pytest.raises(IngestError) as excinfo:
        ffprobe_info(tmp_path / "source.mp4")
    assert "ffprobe is missing" in str(excinfo.value)
    assert FFMPEG_DOWNLOAD_URL in str(excinfo.value)
