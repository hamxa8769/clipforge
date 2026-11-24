"""Unit tests for system-Python faster-whisper detection + transcription (v0.10.2).

The v0.10.1 user report: they ran ``python -m pip install
faster-whisper`` in PowerShell, restarted the app, and the app still
did not detect Whisper. Root cause: the frozen ``clipforge-server.exe``
can never see the system Python's ``site-packages``, and both the
requirements probe and ``transcribe()`` only looked inside the frozen
process. The fix detects a system-Python install and transcribes in
it as a subprocess.

Isolation: faster-whisper is NOT installed in this VM, so the
in-process path is genuinely absent — but tests must not depend on
that (another machine may have it), so ``find_spec`` is faked to
``None`` for ``faster_whisper`` in every test. A fake ``python``
executable (a real Python script with a shebang, found via a temp
``PATH``) plays the role of the user's system Python: it answers the
probe / transcribe ``-c`` programs with canned output, chosen by the
``FAKE_FW_MODE`` env var. No real model, no network, no ffmpeg
(the audio-extraction step is stubbed where transcription is tested).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from clipforge import transcribe as transcribe_mod
from clipforge.requirements import check_requirements, probe_whisper
from clipforge.whisper_system import (
    clear_detection_cache,
    detect_faster_whisper,
    parse_transcript_json,
)
from clipforge.workflow import MissingDependencyError, require_transcription_backend

_FAKE_PYTHON = '''#!/usr/bin/python3
"""Fake system Python: answers the whisper_system probe/transcribe programs."""
import json
import os
import sys

mode = os.environ.get("FAKE_FW_MODE", "ok")
code = sys.argv[sys.argv.index("-c") + 1] if "-c" in sys.argv else ""
if "clipforge-fw-probe-v1" in code:
    if mode == "broken":
        print("FW_BROKEN DLL load failed while importing ctranslate2: libiomp5md.dll not found")
    elif mode == "missing-dep":
        print("FW_BROKEN missing dependency: No module named 'ctranslate2'")
    elif mode == "av19":
        print(
            'FW_BROKEN faster-whisper 1.2.1 is incompatible with PyAV 19.0.2: it passes '
            'metadata_errors= to av.open(), which PyAV 19 removed, so every transcription '
            'crashes. Fix: pip install "av<19" in the same Python, then press Re-check.'
        )
    elif mode == "missing":
        print("FW_MISSING")
    else:
        print("FW_OK 1.2.3\\t/tmp/fake-python/faster_whisper/__init__.py")
elif "clipforge-fw-transcribe-v1" in code:
    if mode == "transcribe-fail":
        print("ctranslate2 exploded", file=sys.stderr)
        sys.exit(1)
    elif mode == "transcribe-garbage":
        print("this is not json")
    else:
        print(json.dumps({
            "words": [
                {"text": "hello", "start": 0.0, "end": 0.5},
                {"text": "world", "start": 0.5, "end": 1.0},
            ],
            "language": "en",
        }))
else:
    print("FW_MISSING")
'''


@pytest.fixture
def isolated_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Empty PATH + HOME + data dir, with a fake `python` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(_FAKE_PYTHON, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FAKE_FW_MODE", raising=False)
    clear_detection_cache()
    yield {"bin": bin_dir, "home": home, "data": data_dir, "python": fake}
    clear_detection_cache()


@pytest.fixture
def no_faster_whisper_in_process(monkeypatch: pytest.MonkeyPatch):
    """The frozen-exe situation: no in-process faster-whisper import."""
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args, **kwargs):
        if name == "faster_whisper":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)


def _cached_model(home: Path) -> None:
    snap = home / "hub" / "models--Systran--faster-whisper-small" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"fake-model")


# ------------------------------------------------------- detection
def test_detect_finds_system_python_install(isolated_python, no_faster_whisper_in_process) -> None:
    """The reported scenario: in-process import absent, system Python
    has faster-whisper — detection must find it, with version + path."""
    det = detect_faster_whisper(fresh=True)
    assert det["available"] is True
    assert det["via"] == "system-python"
    assert det["version"] == "1.2.3"
    assert det["python"] == str(isolated_python["python"])
    assert det["python_argv"] == [str(isolated_python["python"])]
    assert "system Python" in det["source"]
    assert det["broken"] is False


def test_detect_reports_broken_install_not_missing(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A system Python whose faster-whisper import crashes (missing
    native DLL) is `broken` with the reason — never "installed", and
    never silently "missing"."""
    monkeypatch.setenv("FAKE_FW_MODE", "broken")
    det = detect_faster_whisper(fresh=True)
    assert det["available"] is False
    assert det["broken"] is True
    assert "ctranslate2" in det["broken_reason"]
    assert "libiomp5md" in det["broken_reason"]


def test_detect_reports_missing_dependency_as_broken(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """faster-whisper present but its own dependency (ctranslate2)
    missing is a broken install, not a missing faster-whisper."""
    monkeypatch.setenv("FAKE_FW_MODE", "missing-dep")
    det = detect_faster_whisper(fresh=True)
    assert det["available"] is False
    assert det["broken"] is True
    assert "ctranslate2" in det["broken_reason"]


def test_detect_reports_pyav19_incompatibility_as_broken(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyAV 19 removed the `metadata_errors` argument that
    faster-whisper 1.2.1 still passes to `av.open()` — a fresh `pip
    install faster-whisper` gets av 19 and every transcription
    crashes, even though the import succeeds. The probe must report
    that install `broken` with the `av<19` fix, never "installed"."""
    monkeypatch.setenv("FAKE_FW_MODE", "av19")
    det = detect_faster_whisper(fresh=True)
    assert det["available"] is False
    assert det["broken"] is True
    assert "metadata_errors" in det["broken_reason"]
    assert "av<19" in det["broken_reason"]
    probe = probe_whisper("small")
    assert probe["status"] == "broken"
    assert probe["broken"] is True
    assert "av<19" in probe["detail"]


def test_av_metadata_errors_check_with_fake_av(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process PyAV check: a real `av.open` that rejects the
    `metadata_errors` kwarg is the broken pair; an `av.open` that
    accepts it (or no `av` at all) is not this problem."""
    import sys
    import types

    from clipforge.whisper_system import _av_metadata_errors_broken

    def _fake_av(open_fn):
        mod = types.ModuleType("av")
        mod.open = open_fn
        return mod

    def _rejects(*args, **kwargs):
        raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    monkeypatch.setitem(sys.modules, "av", _fake_av(_rejects))
    reason = _av_metadata_errors_broken()
    assert reason is not None
    assert "metadata_errors" in reason
    assert "av<19" in reason

    class _Container:
        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "av", _fake_av(lambda *a, **k: _Container()))
    assert _av_metadata_errors_broken() is None

    monkeypatch.delitem(sys.modules, "av", raising=False)
    assert _av_metadata_errors_broken() is None


def test_detect_in_process_pyav19_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same PyAV-19 breakage, in-process (dev run from source with
    faster-whisper installed): detection must report it broken, not
    "installed" — the import succeeds, only the decode call fails."""
    import sys
    import types

    fw = types.ModuleType("faster_whisper")
    fw.__version__ = "1.2.1"
    fw.__file__ = "/fake/site-packages/faster_whisper/__init__.py"
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args, **kwargs):
        if name == "faster_whisper":
            return object()
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)

    def _rejects(*args, **kwargs):
        raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    av_mod = types.ModuleType("av")
    av_mod.open = _rejects
    monkeypatch.setitem(sys.modules, "av", av_mod)
    clear_detection_cache()
    det = detect_faster_whisper(fresh=True)
    assert det["available"] is False
    assert det["broken"] is True
    assert "metadata_errors" in det["broken_reason"]
    assert "av<19" in det["broken_reason"]


def test_detect_missing_when_no_python_has_it(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FW_MODE", "missing")
    det = detect_faster_whisper(fresh=True)
    assert det["available"] is False
    assert det["via"] == "none"
    assert det["broken"] is False


def test_detect_uses_cache_but_fresh_bypasses_it(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = detect_faster_whisper(fresh=True)
    assert first["available"] is True
    # The install "disappears" — a cached call still sees it …
    monkeypatch.setenv("FAKE_FW_MODE", "missing")
    assert detect_faster_whisper()["available"] is True
    # … while a fresh probe (what Re-check does) sees the truth.
    assert detect_faster_whisper(fresh=True)["available"] is False


def test_parse_transcript_json_ignores_stray_stdout_lines() -> None:
    payload = parse_transcript_json(
        "some warning line\n"
        '{"words": [{"text": "hi", "start": 0.0, "end": 0.25}], "language": "en"}\n'
    )
    assert payload["words"][0]["text"] == "hi"
    with pytest.raises(ValueError, match="did not return a JSON transcript"):
        parse_transcript_json("no json here\n")


# ------------------------------------------------------- the probe (requirements panel)
def test_probe_whisper_system_python_cached_model_is_ok(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's exact flow: pip-installed faster-whisper + the
    pre-downloaded 'small' model in the shared HF cache → the panel
    shows Installed with version + path. No restart involved: the
    probe runs fresh on every Re-check."""
    _cached_model(isolated_python["home"])
    monkeypatch.setenv("HF_HUB_CACHE", str(isolated_python["home"] / "hub"))
    probe = probe_whisper("small")
    assert probe["status"] == "ok"
    assert probe["found"] is True
    assert probe["engine"] == "faster-whisper"
    assert probe["model_cached"] is True
    assert probe["version"] == "1.2.3"
    assert probe["path"] == str(isolated_python["python"])
    assert "system Python" in probe["source"]
    assert "1.2.3" in probe["detail"]


def test_probe_whisper_system_python_uncached_model_is_degraded(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(isolated_python["home"] / "hub"))
    probe = probe_whisper("small")
    assert probe["status"] == "degraded"
    assert probe["found"] is True
    assert probe["model_cached"] is False
    assert probe["version"] == "1.2.3"


def test_probe_whisper_broken_system_python_is_broken(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FW_MODE", "broken")
    probe = probe_whisper("small")
    assert probe["status"] == "broken"
    assert probe["found"] is False
    assert probe["broken"] is True
    assert "ctranslate2" in probe["detail"]
    assert probe["path"] == str(isolated_python["python"])
    # … and the whole check blocks on it, like a broken ffmpeg.
    result = check_requirements(isolated_python["data"])
    by_id = {r["id"]: r for r in result["requirements"]}
    assert by_id["whisper"]["status"] == "broken"
    assert "whisper" in result["blocking"]
    assert result["all_ready"] is False


def test_probe_whisper_broken_but_api_key_falls_back_without_blocking(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken local install + a configured API key: transcription
    still works (via the API), so the check must not block — but the
    broken install must still be named, never hidden."""
    monkeypatch.setenv("FAKE_FW_MODE", "broken")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    probe = probe_whisper("small")
    assert probe["status"] == "degraded"
    assert probe["engine"] == "whisper-api"
    assert probe["broken"] is True
    assert "ctranslate2" in probe["detail"]
    assert "Whisper API" in probe["detail"]


def test_probe_whisper_api_key_still_works_when_nothing_local(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OpenAI API key fallback is untouched by the new detection."""
    monkeypatch.setenv("FAKE_FW_MODE", "missing")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    probe = probe_whisper("small")
    assert probe["status"] == "ok"
    assert probe["engine"] == "whisper-api"


# ------------------------------------------------------- transcription via subprocess
def _stub_audio_extraction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Skip ffmpeg: pretend the source was extracted to this WAV."""
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFFfake")
    monkeypatch.setattr(transcribe_mod, "_extract_audio_wav", lambda source: wav)
    monkeypatch.setattr(transcribe_mod, "_check_memory_for_model", lambda model: None)
    return wav


def test_transcribe_via_system_python_returns_timed_words(
    isolated_python,
    no_faster_whisper_in_process,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the app path: auto engine selection finds
    the system-Python install and transcription returns real words
    with timestamps (not the fallback spread)."""
    _stub_audio_extraction(monkeypatch, tmp_path)
    result = transcribe_mod.transcribe(
        tmp_path / "source.mp4", source_sha256="abc", use_cache=False
    )
    assert result.engine == "faster-whisper"
    assert [w.text for w in result.words] == ["hello", "world"]
    assert result.words[0].start == 0.0
    assert result.words[0].end == 0.5
    assert result.words[1].start == 0.5
    assert result.words[1].end == 1.0
    assert result.segments and result.segments[0].text == "hello world"


def test_transcribe_forced_faster_whisper_uses_system_python(
    isolated_python,
    no_faster_whisper_in_process,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_audio_extraction(monkeypatch, tmp_path)
    result = transcribe_mod.transcribe(
        tmp_path / "source.mp4",
        source_sha256="abc",
        use_cache=False,
        force_engine="faster-whisper",
    )
    assert result.engine == "faster-whisper"
    assert len(result.words) == 2


def test_transcribe_subprocess_failure_is_a_plain_error(
    isolated_python,
    no_faster_whisper_in_process,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_FW_MODE", "transcribe-fail")
    _stub_audio_extraction(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="faster-whisper transcription failed"):
        transcribe_mod.transcribe(
            tmp_path / "source.mp4",
            source_sha256="abc",
            use_cache=False,
            force_engine="faster-whisper",
        )


def test_transcribe_subprocess_garbage_output_is_a_plain_error(
    isolated_python,
    no_faster_whisper_in_process,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_FW_MODE", "transcribe-garbage")
    _stub_audio_extraction(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="no usable transcript"):
        transcribe_mod.transcribe(
            tmp_path / "source.mp4",
            source_sha256="abc",
            use_cache=False,
            force_engine="faster-whisper",
        )


def test_transcribe_forced_faster_whisper_without_any_backend_raises(
    isolated_python,
    no_faster_whisper_in_process,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_FW_MODE", "missing")
    _stub_audio_extraction(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="no working install was found"):
        transcribe_mod.transcribe(
            tmp_path / "source.mp4",
            source_sha256="abc",
            use_cache=False,
            force_engine="faster-whisper",
        )


# ------------------------------------------------------- the job gate
def test_require_transcription_backend_passes_for_system_python(
    isolated_python, no_faster_whisper_in_process
) -> None:
    require_transcription_backend()  # must not raise


def test_require_transcription_backend_broken_has_its_own_code(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FW_MODE", "broken")
    with pytest.raises(MissingDependencyError) as excinfo:
        require_transcription_backend()
    assert excinfo.value.code == "transcription_backend_broken"
    assert "ctranslate2" in excinfo.value.detail


def test_require_transcription_backend_missing_still_blocks(
    isolated_python, no_faster_whisper_in_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FW_MODE", "missing")
    with pytest.raises(MissingDependencyError) as excinfo:
        require_transcription_backend()
    assert excinfo.value.code == "transcription_backend_missing"
    assert "Re-check" in excinfo.value.required_action
