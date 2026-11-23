"""Unit tests for the v0.9.3 transcription memory guard
(``src/clipforge/transcribe.py``).

The in-process faster-whisper model load is the top suspect for the
server dying abruptly with no traceback (a native-code segfault or
OOM in the dispatcher thread kills the whole uvicorn process). The
guard is the minimum that fits now: log the load with RSS + free
memory, and refuse to load a model the machine cannot hold — the job
then fails with a plain message instead of the server dying.
Moving transcription into its own subprocess is the real fix and is
still open (see the v0.9.3 diagnosis, fix 7).
"""

from __future__ import annotations

import pytest

from clipforge import transcribe as transcribe_mod


def test_free_memory_is_measurable_on_this_machine() -> None:
    """On this Linux VM /proc/meminfo must answer with a real number."""
    free = transcribe_mod._free_memory_bytes()
    assert isinstance(free, int)
    assert free > 0


def test_rss_is_measurable_on_this_machine() -> None:
    """Current RSS (not peak) must answer with a real number."""
    rss = transcribe_mod._rss_bytes()
    assert isinstance(rss, int)
    assert rss > 0


def test_guard_passes_with_ample_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default 'small' model (~500 MB + 512 MB headroom) fits when
    8 GB are free, so the guard must let it through. Free memory is
    mocked: the ambient free RAM on a shared VM fluctuates (this VM
    showed 853 MB free while the full suite was running), so an
    absolute assertion would be flaky by design."""
    monkeypatch.setattr(transcribe_mod, "_free_memory_bytes", lambda: 8 * 1024 * 1024 * 1024)
    assert transcribe_mod._check_memory_for_model("small") is None


def test_guard_refuses_when_free_memory_is_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100 MB free cannot hold the 'small' model: refuse with a plain
    message naming the model and the free memory — never load it."""
    monkeypatch.setattr(transcribe_mod, "_free_memory_bytes", lambda: 100 * 1024 * 1024)
    with pytest.raises(RuntimeError, match="Not enough free memory.*'small'"):
        transcribe_mod._check_memory_for_model("small")


def test_guard_sizes_models_differently(monkeypatch: pytest.MonkeyPatch) -> None:
    """600 MB free holds 'tiny' (75 + 512 MB headroom) but not 'small'
    (500 + 512 MB): the threshold must follow the model, not a flat
    number."""
    monkeypatch.setattr(transcribe_mod, "_free_memory_bytes", lambda: 600 * 1024 * 1024)
    assert transcribe_mod._check_memory_for_model("tiny") is None
    with pytest.raises(RuntimeError, match="'small'"):
        transcribe_mod._check_memory_for_model("small")


def test_guard_proceeds_when_memory_cannot_be_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When free memory cannot be measured on a platform, the guard
    logs and proceeds — refusing blind would block valid runs."""
    monkeypatch.setattr(transcribe_mod, "_free_memory_bytes", lambda: None)
    assert transcribe_mod._check_memory_for_model("small") is None


def test_faster_whisper_refuses_before_audio_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With low memory, ``_transcribe_faster_whisper`` must refuse
    before extracting audio (or importing the model): no wasted
    ffmpeg work, no model load, no dead server."""
    monkeypatch.setattr(transcribe_mod, "_free_memory_bytes", lambda: 100 * 1024 * 1024)

    def _must_not_run(*args, **kwargs):  # noqa: ANN001, ANN002, ANN202
        raise AssertionError("audio extraction must not run on a refused load")

    monkeypatch.setattr(transcribe_mod, "_extract_audio_wav", _must_not_run)
    with pytest.raises(RuntimeError, match="Not enough free memory"):
        transcribe_mod._transcribe_faster_whisper(
            transcribe_mod.Path("/tmp/does-not-exist.mp4"), "small", None
        )
