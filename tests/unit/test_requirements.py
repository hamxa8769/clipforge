"""Unit tests for complete pre-flight requirements verification (v0.9.2).

The v0.9.1 user report: a job failed *mid-download* with a yt-dlp
error even though yt-dlp was installed. The fix is verified
detection — every requirement is *run* (``--version`` / ``-version``)
before any job starts, and a binary that exists but does not run is
reported ``broken``, never "installed".

Isolation: this VM has real ffmpeg / ffprobe on PATH, a real yt-dlp
in ``~/.local/bin`` and faster-whisper installed, so every test that
simulates missing / broken tools first points PATH and HOME at empty
temp dirs (``Path.home()`` follows ``$HOME`` on POSIX) and points
``CLIPFORGE_DATA_DIR`` at an empty temp dir, so no real install can
leak into the probe. Fake binaries are tiny shell scripts.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from clipforge.ffmpeg_install import FfmpegInstaller, probe_ffmpeg, probe_ffprobe
from clipforge.requirements import check_requirements, probe_tool, probe_whisper
from clipforge.workflow import MissingDependencyError, check_dependencies, require_tool
from clipforge.ytdlp_install import probe_yt_dlp

FAKE_YTDLP_VERSION = "2026.10.26"
FAKE_FFMPEG_VERSION = "ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers"
FAKE_FFPROBE_VERSION = "ffprobe version 8.1.2 Copyright (c) 2007-2026 the FFmpeg developers"


def _write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _working_ytdlp(path: Path) -> Path:
    return _write_script(path, f"echo '{FAKE_YTDLP_VERSION}'")


def _working_ffmpeg(path: Path) -> Path:
    return _write_script(path, f"echo '{FAKE_FFMPEG_VERSION}'")


def _working_ffprobe(path: Path) -> Path:
    return _write_script(path, f"echo '{FAKE_FFPROBE_VERSION}'")


def _broken(path: Path) -> Path:
    return _write_script(path, "echo 'boom' >&2; exit 1")


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Empty PATH + HOME + data dir: no real tool can leak into a probe."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return {"bin": bin_dir, "home": home, "data": data_dir}


@pytest.fixture
def no_faster_whisper(monkeypatch: pytest.MonkeyPatch):
    """Simulate faster-whisper not installed (it is, on this VM)."""
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args, **kwargs):
        if name == "faster_whisper":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)


# ------------------------------------------------------- yt-dlp (v0.9.1, re-pinned)
def test_probe_yt_dlp_path_install_found(isolated_env) -> None:
    _working_ytdlp(isolated_env["bin"] / "yt-dlp")
    probe = probe_yt_dlp(isolated_env["data"])
    assert probe["found"] is True
    assert probe["version"] == FAKE_YTDLP_VERSION
    assert probe["broken"] is False
    assert probe["source"] == "PATH"


def test_probe_yt_dlp_bundled_found(isolated_env) -> None:
    bundled = isolated_env["data"] / "yt-dlp"
    bundled.mkdir()
    _working_ytdlp(bundled / "yt-dlp")
    probe = probe_yt_dlp(isolated_env["data"])
    assert probe["found"] is True
    assert probe["version"] == FAKE_YTDLP_VERSION
    assert probe["source"] == "app data folder (one-click install)"


def test_probe_yt_dlp_broken_binary_reported_broken(isolated_env) -> None:
    broken = _broken(isolated_env["bin"] / "yt-dlp")
    probe = probe_yt_dlp(isolated_env["data"])
    assert probe["found"] is False
    assert probe["broken"] is True
    assert probe["path"] == str(broken)
    assert "does not run" in probe["broken_reason"]
    assert str(broken) in probe["broken_reason"]


def test_probe_yt_dlp_missing_when_nothing_installed(isolated_env) -> None:
    probe = probe_yt_dlp(isolated_env["data"])
    assert probe == {
        "found": False,
        "path": "",
        "version": "",
        "broken": False,
        "broken_reason": "",
        "source": "",
    }


def test_probe_prefers_working_binary_over_broken_path_one(isolated_env) -> None:
    # A broken yt-dlp earlier on PATH must not shadow a working one
    # later — the probe skips the broken candidate and keeps looking.
    _broken(isolated_env["bin"] / "yt-dlp")
    later = isolated_env["data"] / "later-bin"
    later.mkdir()
    _working_ytdlp(later / "yt-dlp")
    monkeypatch_path = os.pathsep.join([str(isolated_env["bin"]), str(later)])
    os.environ["PATH"] = monkeypatch_path
    probe = probe_yt_dlp(isolated_env["data"])
    assert probe["found"] is True
    assert probe["version"] == FAKE_YTDLP_VERSION
    assert probe["path"] == str(later / "yt-dlp")


# ------------------------------------------------------- ffmpeg / ffprobe
def test_probe_ffmpeg_path_install_found(isolated_env) -> None:
    _working_ffmpeg(isolated_env["bin"] / "ffmpeg")
    probe = probe_ffmpeg(isolated_env["data"])
    assert probe["found"] is True
    assert probe["version"] == FAKE_FFMPEG_VERSION
    assert probe["broken"] is False
    assert probe["source"] == "PATH"


def test_probe_ffmpeg_bundled_found(isolated_env) -> None:
    bundled = isolated_env["data"] / "ffmpeg" / "bin"
    bundled.mkdir(parents=True)
    _working_ffmpeg(bundled / "ffmpeg")
    probe = probe_ffmpeg(isolated_env["data"])
    assert probe["found"] is True
    assert probe["source"] == "app data folder (one-click install)"


def test_probe_ffmpeg_broken_binary_reported_broken(isolated_env) -> None:
    broken = _broken(isolated_env["bin"] / "ffmpeg")
    probe = probe_ffmpeg(isolated_env["data"])
    assert probe["found"] is False
    assert probe["broken"] is True
    assert probe["path"] == str(broken)
    assert "does not run" in probe["broken_reason"]


def test_probe_ffmpeg_missing_when_nothing_installed(isolated_env) -> None:
    probe = probe_ffmpeg(isolated_env["data"])
    assert probe["found"] is False
    assert probe["broken"] is False
    assert probe["path"] == ""


def test_probe_ffprobe_found_and_broken(isolated_env) -> None:
    _working_ffprobe(isolated_env["bin"] / "ffprobe")
    probe = probe_ffprobe(isolated_env["data"])
    assert probe["found"] is True
    assert probe["version"] == FAKE_FFPROBE_VERSION
    (isolated_env["bin"] / "ffprobe").unlink()
    _broken(isolated_env["bin"] / "ffprobe")
    probe = probe_ffprobe(isolated_env["data"])
    assert probe["found"] is False
    assert probe["broken"] is True


def test_ffmpeg_installer_status_reports_broken_plainly(isolated_env) -> None:
    bundled = isolated_env["data"] / "ffmpeg" / "bin"
    bundled.mkdir(parents=True)
    _broken(bundled / "ffmpeg")
    status = FfmpegInstaller(isolated_env["data"]).status()
    assert status["found"] is False
    assert status["broken"] is True
    assert "does not run" in status["broken_reason"]


def test_ffmpeg_installer_start_install_reinstalls_broken_binary(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken FFmpeg must not short-circuit start_install to "done"."""
    bundled = isolated_env["data"] / "ffmpeg" / "bin"
    bundled.mkdir(parents=True)
    _broken(bundled / "ffmpeg")
    installer = FfmpegInstaller(isolated_env["data"])
    # The real download must never run: stub the worker thread's entry.
    started: list[str] = []
    monkeypatch.setattr(installer, "_run_install", lambda url: started.append(url))
    result = installer.start_install()
    assert started, "a broken install must start a real install, not report done"
    assert result["state"] == "downloading"


# ------------------------------------------------------- Whisper
def test_probe_whisper_missing_without_backend(isolated_env, no_faster_whisper) -> None:
    probe = probe_whisper()
    assert probe["status"] == "missing"
    assert probe["found"] is False
    assert probe["engine"] == "none"
    assert "faster-whisper" in probe["detail"]
    assert "OPENAI_API_KEY" in probe["detail"]


def test_probe_whisper_api_key_counts(
    isolated_env, no_faster_whisper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    probe = probe_whisper()
    assert probe["status"] == "ok"
    assert probe["found"] is True
    assert probe["engine"] == "whisper-api"


def test_probe_whisper_local_model_not_cached_is_degraded(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args, **kwargs):
        if name == "faster_whisper":
            return object()  # installed, but no model in the cache
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    monkeypatch.setenv("HF_HUB_CACHE", str(isolated_env["home"] / "hub"))
    probe = probe_whisper("small")
    assert probe["status"] == "degraded"
    assert probe["found"] is True
    assert probe["engine"] == "faster-whisper"
    assert probe["model_cached"] is False
    assert "500 MB" in probe["detail"]


def test_probe_whisper_local_model_cached_is_ok(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args, **kwargs):
        if name == "faster_whisper":
            return object()
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    snap = (
        isolated_env["home"]
        / "hub"
        / "models--Systran--faster-whisper-small"
        / "snapshots"
        / "abc123"
    )
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"fake-model")
    monkeypatch.setenv("HF_HUB_CACHE", str(isolated_env["home"] / "hub"))
    probe = probe_whisper("small")
    assert probe["status"] == "ok"
    assert probe["model_cached"] is True


# ------------------------------------------------------- the whole check
def test_check_requirements_all_ready(isolated_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _working_ytdlp(isolated_env["bin"] / "yt-dlp")
    _working_ffmpeg(isolated_env["bin"] / "ffmpeg")
    _working_ffprobe(isolated_env["bin"] / "ffprobe")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    result = check_requirements(isolated_env["data"])
    assert result["all_ready"] is True
    assert result["blocking"] == []
    assert [r["id"] for r in result["requirements"]] == [
        "yt-dlp",
        "ffmpeg",
        "ffprobe",
        "whisper",
    ]
    assert all(r["status"] == "ok" for r in result["requirements"][:3])
    # Whisper: the API key path is ready; on a machine with
    # faster-whisper installed but the model not yet in the (temp)
    # cache it reports "degraded" instead — still ready, never
    # blocking. Either way the check passes.
    assert result["requirements"][3]["status"] in ("ok", "degraded")
    yt = result["requirements"][0]
    assert yt["version"] == FAKE_YTDLP_VERSION
    assert yt["path"] == str(isolated_env["bin"] / "yt-dlp")
    assert result["checked_at"]


def test_check_requirements_blocks_on_missing_and_broken(isolated_env, no_faster_whisper) -> None:
    _broken(isolated_env["bin"] / "ffmpeg")
    # yt-dlp + ffprobe + whisper all absent/broken: yt-dlp missing,
    # ffmpeg broken, ffprobe missing, whisper missing.
    result = check_requirements(isolated_env["data"])
    assert result["all_ready"] is False
    assert result["blocking"] == ["yt-dlp", "ffmpeg", "ffprobe", "whisper"]
    by_id = {r["id"]: r for r in result["requirements"]}
    assert by_id["yt-dlp"]["status"] == "missing"
    assert by_id["yt-dlp"]["fix_action"] == "install-yt-dlp"
    assert "Install yt-dlp" in by_id["yt-dlp"]["fix_label"]
    assert by_id["ffmpeg"]["status"] == "broken"
    assert "does not run" in by_id["ffmpeg"]["detail"]
    assert by_id["ffmpeg"]["fix_action"] == "install-ffmpeg"
    assert by_id["ffprobe"]["status"] == "missing"
    assert by_id["ffprobe"]["fix_action"] == "install-ffmpeg"
    assert by_id["whisper"]["status"] == "missing"


def test_check_requirements_degraded_whisper_does_not_block(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _working_ytdlp(isolated_env["bin"] / "yt-dlp")
    _working_ffmpeg(isolated_env["bin"] / "ffmpeg")
    _working_ffprobe(isolated_env["bin"] / "ffprobe")
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args, **kwargs):
        if name == "faster_whisper":
            return object()
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    monkeypatch.setenv("HF_HUB_CACHE", str(isolated_env["home"] / "hub"))
    result = check_requirements(isolated_env["data"])
    assert result["all_ready"] is True
    assert result["blocking"] == []
    assert {r["id"]: r["status"] for r in result["requirements"]}["whisper"] == "degraded"


def test_probe_tool_dispatch(isolated_env) -> None:
    _working_ytdlp(isolated_env["bin"] / "yt-dlp")
    assert probe_tool("yt-dlp", isolated_env["data"])["found"] is True
    assert probe_tool("ffmpeg", isolated_env["data"])["found"] is False
    assert probe_tool("ffprobe", isolated_env["data"])["found"] is False


# ------------------------------------------------------- the job gate
def test_require_tool_passes_for_working_binary(isolated_env) -> None:
    _working_ytdlp(isolated_env["bin"] / "yt-dlp")
    require_tool("yt-dlp", "downloads the source", "Install yt-dlp")
    _working_ffmpeg(isolated_env["bin"] / "ffmpeg")
    require_tool("ffmpeg", "renders clips", "Install FFmpeg")


def test_require_tool_blocks_broken_binary_with_plain_message(isolated_env) -> None:
    broken = _broken(isolated_env["bin"] / "yt-dlp")
    with pytest.raises(MissingDependencyError) as excinfo:
        require_tool("yt-dlp", "downloads the source", "Install yt-dlp")
    err = excinfo.value
    assert err.code == "yt_dlp_broken"
    assert err.dependency == "yt-dlp"
    assert str(broken) in err.detail
    assert "does not run" in err.detail
    assert err.required_action == "Install yt-dlp"


def test_require_tool_blocks_missing_binary_with_missing_code(isolated_env) -> None:
    with pytest.raises(MissingDependencyError) as excinfo:
        require_tool("yt-dlp", "downloads the source", "Install yt-dlp")
    assert excinfo.value.code == "yt_dlp_missing"


def test_require_tool_blocks_broken_ffmpeg_before_render(isolated_env) -> None:
    _broken(isolated_env["bin"] / "ffmpeg")
    with pytest.raises(MissingDependencyError) as excinfo:
        require_tool("ffmpeg", "renders clips", "Install FFmpeg")
    assert excinfo.value.code == "ffmpeg_broken"


def test_check_dependencies_reports_broken_as_unavailable(isolated_env) -> None:
    _working_ytdlp(isolated_env["bin"] / "yt-dlp")
    _broken(isolated_env["bin"] / "ffmpeg")
    deps = {d.name: d for d in check_dependencies()}
    assert deps["yt-dlp"].available is True
    assert deps["ffmpeg"].available is False
    assert "does not run" in deps["ffmpeg"].detail
    assert deps["ffprobe"].available is False


def test_agent_preflight_blocks_broken_ytdlp(isolated_env) -> None:
    from clipforge.agent import AgentConfig, AgentError, _require_pipeline_tools

    _broken(isolated_env["bin"] / "yt-dlp")
    with pytest.raises(AgentError) as excinfo:
        _require_pipeline_tools(AgentConfig(), isolated_env["data"])
    assert "does not run" in str(excinfo.value)


def test_agent_preflight_passes_for_working_ytdlp(isolated_env) -> None:
    from clipforge.agent import AgentConfig, _require_pipeline_tools

    _working_ytdlp(isolated_env["bin"] / "yt-dlp")
    _require_pipeline_tools(AgentConfig(), isolated_env["data"])
