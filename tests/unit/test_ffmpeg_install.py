"""Unit + API tests for the one-click FFmpeg installer (v0.4.1).

No real download ever runs here: the installer takes an injectable
``downloader`` that writes a fake build (a tiny shell script that
answers ``ffmpeg -version``), and the checksum fetch is monkeypatched.
The fake build exercises the real code paths — extract, verify,
atomic swap, PATH prepend, settings save, failure cleanup.

Platform note: the fake binaries are POSIX shell scripts, so the
install-path tests run as the Linux build; the Windows branch
(registry user-PATH update) is tested with a fake ``winreg`` module
injected into ``sys.modules``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from clipforge.ffmpeg_install import (
    LINUX_DOWNLOAD_URL,
    WINDOWS_DOWNLOAD_URL,
    DownloadResult,
    FfmpegInstaller,
    add_to_user_path_windows,
    download_url_for_platform,
    download_urls_for_platform,
    ensure_bundled_on_path,
    resolve_ffmpeg_path,
)

FAKE_VERSION_LINE = "ffmpeg version 9.1-essentials_build-www.gyan.dev Copyright (c) 2000-2026"
FAKE_FFMPEG_SCRIPT = f"#!/bin/sh\necho '{FAKE_VERSION_LINE}'\n".encode()
FAKE_FFPROBE_SCRIPT = b"#!/bin/sh\necho 'ffprobe version 9.1-essentials_build-www.gyan.dev'\n"


def _fake_tarxz_bytes() -> bytes:
    """A minimal static-build-shaped tar.xz: ffmpeg-9.1-amd64-static/{ffmpeg,ffprobe}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, payload in (
            ("ffmpeg-9.1-amd64-static/ffmpeg", FAKE_FFMPEG_SCRIPT),
            ("ffmpeg-9.1-amd64-static/ffprobe", FAKE_FFPROBE_SCRIPT),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _fake_zip_bytes() -> bytes:
    """A minimal gyan-shaped zip: ffmpeg-9.1-essentials_build/bin/{ffmpeg.exe,ffprobe.exe}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in (
            ("ffmpeg-9.1-essentials_build/bin/ffmpeg.exe", FAKE_FFMPEG_SCRIPT),
            ("ffmpeg-9.1-essentials_build/bin/ffprobe.exe", FAKE_FFPROBE_SCRIPT),
        ):
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o755 << 16
            zf.writestr(info, payload)
    return buf.getvalue()


def _fake_downloader(payload: bytes):
    """A downloader that writes ``payload`` and reports progress like a real one."""

    def _download(url: str, dest: Path, progress_cb) -> DownloadResult:
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    return _download


@pytest.fixture(autouse=True)
def _restore_process_path(monkeypatch: pytest.MonkeyPatch):
    """A successful install prepends <data_dir>/ffmpeg/bin to this
    process's PATH (that is the feature). In the test session that dir
    holds a *fake* ffmpeg, so restore PATH after every test — otherwise
    later tests (render_broll) would run the fake instead of the real
    ffmpeg/ffprobe and fail with JSON errors from the fake output."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))


@pytest.fixture
def no_checksum(monkeypatch: pytest.MonkeyPatch):
    """Checksum file unreachable — install must still proceed (HTTPS source)."""
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: None)


def _wait_done(installer: FfmpegInstaller, timeout_s: float = 10.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while installer.is_running and time.monotonic() < deadline:
        time.sleep(0.05)
    return installer.status()


# ---------------------------------------------------------------- sources
def test_download_urls_are_the_official_ones() -> None:
    assert (
        WINDOWS_DOWNLOAD_URL == "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    )
    assert (
        LINUX_DOWNLOAD_URL
        == "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    )
    assert download_url_for_platform("Windows") == WINDOWS_DOWNLOAD_URL
    assert download_url_for_platform("Linux") == LINUX_DOWNLOAD_URL
    assert download_url_for_platform("Darwin") is None


# ---------------------------------------------------------------- happy path (Linux tar.xz)
def test_linux_install_succeeds_with_mocked_download(
    tmp_path: Path, no_checksum, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    saved: dict[str, str] = {}
    installer = FfmpegInstaller(
        tmp_path,
        downloader=_fake_downloader(_fake_tarxz_bytes()),
        on_installed=lambda p: saved.setdefault("path", p),
    )
    # force: this VM already has a system ffmpeg on PATH, and the
    # installer correctly short-circuits to "already installed" then.
    assert installer.start_install(force=True)["state"] in (
        "downloading",
        "verifying",
        "extracting",
        "installing",
        "done",
    )
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["progress_pct"] == 100.0
    assert status["found"] is True
    assert FAKE_VERSION_LINE.split()[2] in status["version"]
    installed = tmp_path / "ffmpeg" / "bin" / "ffmpeg"
    assert installed.is_file()
    assert os.access(installed, os.X_OK)
    assert (tmp_path / "ffmpeg" / "bin" / "ffprobe").is_file()
    assert saved.get("path") == str(installed)
    # No temp litter left behind.
    assert not list(tmp_path.glob(".ffmpeg-install-*"))
    assert not (tmp_path / ".ffmpeg-install-old").exists()
    # State file on disk agrees with the live state.
    assert json.loads((tmp_path / "ffmpeg-install-state.json").read_text())["state"] == "done"


def test_installed_binary_is_preferred_and_on_path(
    tmp_path: Path, no_checksum, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(_fake_tarxz_bytes()))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done"
    assert resolve_ffmpeg_path(tmp_path) == str(tmp_path / "ffmpeg" / "bin" / "ffmpeg")
    assert ensure_bundled_on_path(tmp_path) is True
    assert str(tmp_path / "ffmpeg" / "bin") in os.environ["PATH"].split(os.pathsep)


def test_second_start_without_force_reports_done(tmp_path: Path, no_checksum) -> None:
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(_fake_tarxz_bytes()))
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    again = installer.start_install()
    assert again["state"] == "done"
    assert again["path"] == str(tmp_path / "ffmpeg" / "bin" / "ffmpeg")


def test_force_reinstall_replaces_existing(tmp_path: Path, no_checksum) -> None:
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(_fake_tarxz_bytes()))
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg").is_file()


# ---------------------------------------------------------------- checksum
def test_checksum_mismatch_fails_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.ffmpeg_install as mod

    payload = _fake_tarxz_bytes()
    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: "0" * 64)
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(payload))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert "checksum" in status["error"].lower()
    assert not (tmp_path / "ffmpeg").exists()
    assert not list(tmp_path.glob(".ffmpeg-install-*"))


def test_matching_checksum_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.ffmpeg_install as mod

    payload = _fake_tarxz_bytes()
    monkeypatch.setattr(
        mod, "_fetch_expected_sha256", lambda *args: hashlib.sha256(payload).hexdigest()
    )
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(payload))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["checksum_verified"] is True


# ---------------------------------------------------------------- failures
def test_download_failure_leaves_no_partial_install(tmp_path: Path) -> None:
    def _boom(url: str, dest: Path, progress_cb):
        raise ConnectionError("no network here")

    installer = FfmpegInstaller(tmp_path, downloader=_boom)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert status["error"]  # plain-language message, not a bare traceback
    assert "Traceback" not in status["error"]
    assert not (tmp_path / "ffmpeg").exists()
    assert not list(tmp_path.glob(".ffmpeg-install-*"))


def test_corrupt_archive_fails_cleanly(tmp_path: Path, no_checksum) -> None:
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(b"this is not a tar file"))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert not (tmp_path / "ffmpeg").exists()
    assert not list(tmp_path.glob(".ffmpeg-install-*"))


def test_archive_without_ffmpeg_fails_cleanly(tmp_path: Path, no_checksum) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        info = tarfile.TarInfo("ffmpeg-9.1-amd64-static/README.txt")
        data = b"no binaries here"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(buf.getvalue()))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert "FFmpeg" in status["error"]
    assert not (tmp_path / "ffmpeg").exists()


def test_second_start_while_running_returns_live_status(tmp_path: Path, no_checksum) -> None:
    import time

    def _slow(url: str, dest: Path, progress_cb):
        time.sleep(1.0)
        payload = _fake_tarxz_bytes()
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    installer = FfmpegInstaller(tmp_path, downloader=_slow)
    first = installer.start_install(force=True)
    assert first["state"] == "downloading"
    second = installer.start_install(force=True)
    assert second["state"] == "downloading"  # no duplicate download started
    assert _wait_done(installer)["state"] == "done"


def test_interrupted_run_is_not_stuck_running(tmp_path: Path) -> None:
    state = {
        "state": "downloading",
        "progress_pct": 10.0,
        "downloaded_bytes": 1,
        "total_bytes": 100,
        "message": "Downloading FFmpeg…",
        "error": "",
        "version": "",
        "path": "",
        "found": False,
        "download_url": LINUX_DOWNLOAD_URL,
        "platform": "Linux",
        "path_added": None,
        "checksum_verified": None,
    }
    (tmp_path / "ffmpeg-install-state.json").write_text(json.dumps(state))
    installer = FfmpegInstaller(tmp_path)
    status = installer.status()
    assert status["state"] == "error"
    assert "interrupted" in status["error"].lower()


# ---------------------------------------------------------------- Windows PATH (fake winreg)
def _fake_winreg_module(initial_path: str) -> ModuleType:
    """A minimal winreg stand-in backed by a dict."""
    mod = ModuleType("winreg")
    store = {"Path": initial_path}
    mod.HKEY_CURRENT_USER = "HKCU"
    mod.KEY_READ = 1
    mod.KEY_SET_VALUE = 2
    mod.REG_EXPAND_SZ = 2
    mod.REG_SZ = 1

    class _Key:
        pass

    def OpenKey(root, subkey, reserved=0, access=0):  # noqa: ANN001, ANN202
        return _Key()

    def QueryValueEx(key, name):  # noqa: ANN001, ANN202
        if name not in store:
            raise FileNotFoundError(name)
        return store[name], mod.REG_EXPAND_SZ

    def SetValueEx(key, name, reserved, reg_type, value):  # noqa: ANN001, ANN202
        store[name] = value

    def CloseKey(key):  # noqa: ANN001, ANN202
        pass

    mod.OpenKey = OpenKey
    mod.QueryValueEx = QueryValueEx
    mod.SetValueEx = SetValueEx
    mod.CloseKey = CloseKey
    mod._store = store  # type: ignore[attr-defined]
    return mod


def test_windows_path_append_and_idempotence(monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    fake = _fake_winreg_module(r"C:\Windows;C:\Windows\System32")
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
    monkeypatch.setattr(mod, "_broadcast_environment_change", lambda: None)
    result = add_to_user_path_windows(r"C:\Users\Me\AppData\Roaming\com.clipforge.app\ffmpeg\bin")
    assert result == {"ok": True, "already_present": False, "error": ""}
    assert fake._store["Path"].endswith(r"com.clipforge.app\ffmpeg\bin")
    again = add_to_user_path_windows(r"C:\Users\Me\AppData\Roaming\com.clipforge.app\ffmpeg\bin")
    assert again["already_present"] is True
    assert fake._store["Path"].count("ffmpeg") == 1


def test_windows_path_reports_registry_failure_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    fake = _fake_winreg_module("")
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)

    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("access denied")

    fake.OpenKey = _raise  # type: ignore[method-assign]
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 1})())
    result = add_to_user_path_windows(r"C:\ffmpeg\bin")
    assert result["ok"] is False
    assert "PATH" in result["error"]
    assert "Traceback" not in result["error"]


# ---------------------------------------------------------------- API endpoints
@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: None)
    import importlib

    app_mod = importlib.import_module("clipforge_api.app")
    monkeypatch.setattr(app_mod, "_INSTALLERS", {})
    installer = FfmpegInstaller(
        tmp_path,
        downloader=_fake_downloader(_fake_tarxz_bytes()),
        on_installed=lambda p: app_mod._settings_store().set_ffmpeg_path(p),
    )
    app_mod._INSTALLERS[str(tmp_path)] = installer
    with TestClient(app_mod.app) as client:
        yield client, installer


def test_api_status_reports_idle_before_install(api_client) -> None:
    client, _ = api_client
    resp = client.get("/ffmpeg/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    # found/path reflect this machine (ffmpeg may already be on PATH
    # here); the install state itself is what this test pins down.
    assert body["download_url"] == LINUX_DOWNLOAD_URL


def test_api_install_then_status_reports_done(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: None)
    client, installer = api_client
    resp = client.post("/ffmpeg/install?force=true")
    assert resp.status_code == 202
    assert resp.json()["state"] in ("downloading", "verifying", "extracting", "installing", "done")
    status = _wait_done(installer)
    assert status["state"] == "done", status
    body = client.get("/ffmpeg/status").json()
    assert body["state"] == "done"
    assert body["found"] is True
    assert body["path"].endswith("/ffmpeg/bin/ffmpeg")
    # GET /settings now reports the installed FFmpeg too.
    settings = client.get("/settings").json()
    assert settings["ffmpeg"]["found"] is True
    assert settings["ffmpeg"]["path"] == body["path"]


def test_api_install_is_idempotent_while_running(api_client) -> None:
    client, installer = api_client
    first = client.post("/ffmpeg/install?force=true")
    assert first.status_code == 202
    second = client.post("/ffmpeg/install?force=true")
    assert second.status_code == 202
    assert _wait_done(installer)["state"] == "done"


# ------------------------------------------------- Windows zip branch (mocked platform)
def test_windows_zip_install_calls_user_path_update(
    tmp_path: Path, no_checksum, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "add_to_user_path_windows",
        lambda bin_dir: (
            calls.append(str(bin_dir)) or {"ok": True, "already_present": False, "error": ""}
        ),
    )
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(_fake_zip_bytes()))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["path_added"] is True
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").is_file()
    assert (tmp_path / "ffmpeg" / "bin" / "ffprobe.exe").is_file()
    assert calls == [str(tmp_path / "ffmpeg" / "bin")]


def test_invalid_url_becomes_plain_language_error() -> None:
    import httpx

    from clipforge.ffmpeg_install import _plain_error

    message = _plain_error(httpx.InvalidURL("Invalid port: ':1]'"))
    assert "internet" in message.lower()
    assert "Invalid port" not in message


def test_download_retries_direct_when_proxy_env_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bracketed IPv6 entry in NO_PROXY makes httpx raise InvalidURL
    before any byte moves; the downloader retries direct (trust_env=False)."""
    import httpx

    import clipforge.ffmpeg_install as mod

    class _FakeResp:
        headers: dict = {}
        url = "https://example.invalid/x"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size=1):
            yield b"fake-bytes"

    calls: list = []

    def _fake_stream(method, url, **kwargs):
        calls.append((kwargs.get("trust_env"), kwargs.get("proxy")))
        if kwargs.get("trust_env"):
            raise httpx.InvalidURL("Invalid port: ':1]'")
        return _FakeResp()

    monkeypatch.setattr(mod.httpx, "stream", _fake_stream)
    dest = tmp_path / "f.bin"
    result = mod._download_with_httpx("https://example.invalid/x", dest, lambda d, t: None)
    expected_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    assert calls == [(True, None), (False, expected_proxy)]
    assert dest.read_bytes() == b"fake-bytes"
    assert result.total_bytes is None
    assert result.final_url == "https://example.invalid/x"


# ------------------------------------------------- v0.4.2 hardening: redirect,
# size + checksum against the redirected file, retry + fallback sources,
# step-tagged errors, PATH failure surfacing.
def test_checksum_is_fetched_for_the_redirected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checksum describes the file the redirect points at, not the
    short URL that redirected to it: the installer asks for the
    checksum of the FINAL url the download came from."""
    import clipforge.ffmpeg_install as mod

    payload = _fake_tarxz_bytes()
    seen: list[tuple[str, str | None]] = []

    def _record(requested_url: str, final_url: str | None = None) -> str:
        seen.append((requested_url, final_url))
        return hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(mod, "_fetch_expected_sha256", _record)

    def _redirecting_downloader(url: str, dest: Path, progress_cb) -> DownloadResult:
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        # gyan.dev 303s to the versioned package; the download came
        # from there, so that is the file the checksum must describe.
        return DownloadResult(
            total_bytes=len(payload),
            final_url="https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.1-essentials_build.zip",
        )

    installer = FfmpegInstaller(tmp_path, downloader=_redirecting_downloader)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["checksum_verified"] is True
    assert seen == [
        (
            LINUX_DOWNLOAD_URL,
            "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.1-essentials_build.zip",
        )
    ]


def test_download_reports_final_url_after_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx follows the redirect; the downloader reports the URL the
    bytes actually came from."""
    import clipforge.ffmpeg_install as mod

    payload = b"redirected-bytes"

    class _FakeResp:
        headers = {"content-length": str(len(payload))}
        url = "https://example.invalid/packages/ffmpeg-9.1-essentials_build.zip"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size=1):
            yield payload

    monkeypatch.setattr(
        mod.httpx,
        "stream",
        lambda *a, **k: _FakeResp(),  # noqa: ARG005
    )
    dest = tmp_path / "f.zip"
    result = mod._download_with_httpx(
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        dest,
        lambda d, t: None,
    )
    assert dest.read_bytes() == payload
    assert result.total_bytes == len(payload)
    assert result.final_url == "https://example.invalid/packages/ffmpeg-9.1-essentials_build.zip"


def test_truncated_download_fails_as_download_not_damaged_archive(
    tmp_path: Path, no_checksum
) -> None:
    """A download that stops early must say so at the download step —
    not fail later at extraction as a 'damaged archive'."""
    payload = _fake_tarxz_bytes()

    def _truncated(url: str, dest: Path, progress_cb) -> DownloadResult:
        # Server advertised the full size; only half the bytes arrived.
        dest.write_bytes(payload[: len(payload) // 2])
        progress_cb(len(payload) // 2, len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    installer = FfmpegInstaller(tmp_path, downloader=_truncated)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert status["failed_step"] == "download"
    assert status["error"].startswith("Download failed:")
    assert "stopped" in status["error"].lower()
    assert not (tmp_path / "ffmpeg").exists()


def test_checksum_mismatch_names_the_verify_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipforge.ffmpeg_install as mod

    payload = _fake_tarxz_bytes()
    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: "0" * 64)
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(payload))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert status["failed_step"] == "verify"
    assert status["error"].startswith("Verification failed:")
    assert "checksum" in status["error"].lower()
    assert not (tmp_path / "ffmpeg").exists()


def test_missing_checksum_is_recorded_not_hidden(tmp_path: Path, no_checksum) -> None:
    """No checksum file published for this build: the install proceeds
    (HTTPS from the official source) but says so — checksum_verified
    is False, not a silent None the UI could read as 'verified'."""
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(_fake_tarxz_bytes()))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["checksum_verified"] is False


def test_checksum_manifest_lookup_for_btbN(monkeypatch: pytest.MonkeyPatch) -> None:
    """BtbN publishes one checksums.sha256 manifest per release, not
    per-file .sha256 files — the installer reads the hash for the asset
    from the manifest next to the download URL."""
    import clipforge.ffmpeg_install as mod

    manifest = (
        "7b9b1e63f3ee423bc1d16ca36174d7dada1a805a715b308af60a1487da521c54"
        "  ffmpeg-master-latest-linux64-gpl.tar.xz\n"
        "72bfeff541fcdf3421a555bc466dada18a8b4d158944b8ea8aa7b67302908553"
        "  ffmpeg-master-latest-win64-gpl.zip\n"
    )

    def _fake_fetch(url: str) -> str | None:
        if url.endswith("checksums.sha256"):
            return manifest
        return None

    monkeypatch.setattr(mod, "_fetch_url_text", _fake_fetch)
    got = mod._fetch_expected_sha256(
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-linux64-gpl.tar.xz"
    )
    assert got == "7b9b1e63f3ee423bc1d16ca36174d7dada1a805a715b308af60a1487da521c54"


def test_per_file_sha256_wins_over_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """gyan.dev's <url>.sha256 is tried first; the manifest is only a
    fallback for hosts that publish one."""
    import clipforge.ffmpeg_install as mod

    def _fake_fetch(url: str) -> str | None:
        if url.endswith(".sha256") and "checksums" not in url:
            return "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd\n"
        return None

    monkeypatch.setattr(mod, "_fetch_url_text", _fake_fetch)
    got = mod._fetch_expected_sha256(
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.1-essentials_build.zip",
    )
    assert got == "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd"


def test_primary_retry_then_fallback_source(tmp_path: Path, no_checksum) -> None:
    """Primary fails twice (one retry), the fallback succeeds: the
    install still completes, and the status says which URL was used."""
    payload = _fake_tarxz_bytes()
    calls: list[str] = []

    def _flaky(url: str, dest: Path, progress_cb) -> DownloadResult:
        calls.append(url)
        if url == LINUX_DOWNLOAD_URL:
            raise ConnectionError("primary host unreachable")
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    installer = FfmpegInstaller(tmp_path, downloader=_flaky)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert calls.count(LINUX_DOWNLOAD_URL) == 2  # first try + one retry
    fallback = download_urls_for_platform("Linux")[1]
    assert calls.count(fallback) == 1
    assert status["download_url"] == fallback


def test_transient_primary_failure_retried_once(tmp_path: Path, no_checksum) -> None:
    """A single blip on the primary does not fail the install: the
    same URL is retried once before any fallback is tried."""
    payload = _fake_tarxz_bytes()
    calls: list[str] = []

    def _blip_then_ok(url: str, dest: Path, progress_cb) -> DownloadResult:
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionError("connection reset")
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    installer = FfmpegInstaller(tmp_path, downloader=_blip_then_ok)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert calls == [LINUX_DOWNLOAD_URL, LINUX_DOWNLOAD_URL]


def test_all_sources_failing_says_so(tmp_path: Path, no_checksum) -> None:
    """Every official source down: the error names the download step
    and says all sources were tried, instead of blaming the last one."""
    installer = FfmpegInstaller(
        tmp_path,
        downloader=lambda url, dest, progress_cb: (_ for _ in ()).throw(
            ConnectionError("no network here")
        ),
    )
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert status["failed_step"] == "download"
    assert "every official download source" in status["error"]


def test_flat_zip_layout_without_top_folder_installs(
    tmp_path: Path, no_checksum, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction is layout-agnostic: a zip whose binaries sit directly
    under bin/ (no versioned top-level folder) installs the same way
    as the nested gyan.dev layout."""
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        mod,
        "add_to_user_path_windows",
        lambda bin_dir: {"ok": True, "already_present": False, "error": ""},
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in (
            ("bin/ffmpeg.exe", FAKE_FFMPEG_SCRIPT),
            ("bin/ffprobe.exe", FAKE_FFPROBE_SCRIPT),
        ):
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o755 << 16
            zf.writestr(info, payload)
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(buf.getvalue()))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").is_file()
    assert (tmp_path / "ffmpeg" / "bin" / "ffprobe.exe").is_file()


def test_already_installed_never_calls_downloader(tmp_path: Path, no_checksum) -> None:
    """Short-circuit: FFmpeg already present means no download at all."""
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(_fake_tarxz_bytes()))
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"

    calls: list[str] = []

    def _must_not_run(url: str, dest: Path, progress_cb):
        calls.append(url)
        raise AssertionError("downloader must not run when FFmpeg is installed")

    second = FfmpegInstaller(tmp_path, downloader=_must_not_run)
    status = second.start_install()
    assert status["state"] == "done"
    assert status["failed_step"] == ""
    assert calls == []


def test_legacy_int_returning_downloader_still_works(tmp_path: Path, no_checksum) -> None:
    """Downloaders written against the old protocol (returning a plain
    total-bytes int) keep working: total = the int, final URL = the
    requested URL."""

    def _legacy(url: str, dest: Path, progress_cb) -> int:
        payload = _fake_tarxz_bytes()
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return len(payload)

    installer = FfmpegInstaller(tmp_path, downloader=_legacy)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status


# ------------------------------------------------- Windows PATH failure surfacing
def test_windows_path_failure_is_a_warning_not_a_silent_done(
    tmp_path: Path, no_checksum, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PATH update fails but FFmpeg itself installed: the install
    still reports done (clipping works inside ClipForge) — but the
    failure is surfaced in path_warning and in the message, not
    swallowed."""
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        mod,
        "add_to_user_path_windows",
        lambda bin_dir: {
            "ok": False,
            "already_present": False,
            "error": (
                "Could not add FFmpeg to your Windows PATH "
                "(registry: access denied; setx failed). FFmpeg still works "
                "inside ClipForge — new terminals will need the folder added by hand."
            ),
        },
    )
    installer = FfmpegInstaller(tmp_path, downloader=_fake_downloader(_fake_zip_bytes()))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["path_added"] is False
    assert status["path_warning"]  # surfaced, not discarded
    assert "Windows PATH" in status["path_warning"]
    assert "Windows PATH" in status["message"]
    assert status["failed_step"] == ""


def test_setx_fallback_passes_the_full_path_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry write fails but the read worked: setx gets the FULL
    current PATH plus the new dir — never a literal '%PATH%', which no
    shell expands here and which would have wiped the user's entries."""
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    fake = _fake_winreg_module(r"C:\Windows;C:\Windows\System32")
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)

    def _fail_write(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("access denied")

    fake.SetValueEx = _fail_write  # type: ignore[method-assign]
    seen: list = []

    def _record_run(argv, **kwargs):  # noqa: ANN001, ANN003
        seen.append(argv)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(mod.subprocess, "run", _record_run)
    monkeypatch.setattr(mod, "_broadcast_environment_change", lambda: None)
    result = add_to_user_path_windows(r"C:\clipforge\ffmpeg\bin")
    assert result["ok"] is True
    assert seen == [["setx", "Path", r"C:\Windows;C:\Windows\System32;C:\clipforge\ffmpeg\bin"]]
    assert "%PATH%" not in seen[0][2]


def test_setx_refused_when_registry_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry cannot even be read: setx is refused outright —
    with no readable current PATH it would have stored a literal
    '%PATH%' and destroyed the user's existing entries."""
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    fake = _fake_winreg_module("")
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)

    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("access denied")

    fake.OpenKey = _raise  # type: ignore[method-assign]
    calls: list = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: calls.append(a) or type("P", (), {"returncode": 0})(),
    )
    result = add_to_user_path_windows(r"C:\ffmpeg\bin")
    assert result["ok"] is False
    assert "PATH" in result["error"]
    assert "Traceback" not in result["error"]
    assert calls == []  # setx never ran


def test_setx_refused_when_path_would_be_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """setx silently truncates values over 1024 characters: a PATH
    that long is refused instead of cutting the user's PATH in half."""
    import clipforge.ffmpeg_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    fake = _fake_winreg_module(";".join(f"C:\\dir{i}" for i in range(120)))
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)

    def _fail_write(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("access denied")

    fake.SetValueEx = _fail_write  # type: ignore[method-assign]
    calls: list = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: calls.append(a) or type("P", (), {"returncode": 0})(),
    )
    result = add_to_user_path_windows(r"C:\ffmpeg\bin")
    assert result["ok"] is False
    assert "1024" in result["error"]
    assert calls == []  # setx never ran


def test_html_error_page_falls_back_to_next_source(tmp_path: Path, no_checksum) -> None:
    """A proxy/captive portal answering HTTP 200 with an HTML error
    page instead of the archive: the magic-byte check catches it at
    the download step and the installer moves on to the fallback
    instead of dying at extraction."""
    from clipforge.ffmpeg_install import download_urls_for_platform

    payload = _fake_tarxz_bytes()
    html_page = b"<html><body>Proxy error: host unreachable</body></html>"
    calls: list[str] = []

    def _proxy_poisoned(url: str, dest: Path, progress_cb) -> DownloadResult:
        calls.append(url)
        if url == LINUX_DOWNLOAD_URL:
            dest.write_bytes(html_page)
            progress_cb(len(html_page), len(html_page))
            return DownloadResult(total_bytes=len(html_page), final_url=url)
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    installer = FfmpegInstaller(tmp_path, downloader=_proxy_poisoned)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    fallback = download_urls_for_platform("Linux")[1]
    assert status["download_url"] == fallback
    assert calls.count(LINUX_DOWNLOAD_URL) == 2  # first try + one retry
    assert calls.count(fallback) == 1


def test_corrupt_archive_falls_back_to_next_source(tmp_path: Path, no_checksum) -> None:
    """An archive that unpacks wrong on the primary (right magic, bad
    contents) moves on to the fallback instead of failing the install
    with an unpack error."""
    from clipforge.ffmpeg_install import download_urls_for_platform

    payload = _fake_tarxz_bytes()
    calls: list[str] = []

    def _corrupt_primary(url: str, dest: Path, progress_cb) -> DownloadResult:
        calls.append(url)
        if url == LINUX_DOWNLOAD_URL:
            # xz magic, then garbage: passes the magic check, dies at
            # extraction.
            bad = b"\xfd7zXZ\x00" + b"garbage-garbage-garbage"
            dest.write_bytes(bad)
            progress_cb(len(bad), len(bad))
            return DownloadResult(total_bytes=len(bad), final_url=url)
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    installer = FfmpegInstaller(tmp_path, downloader=_corrupt_primary)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    fallback = download_urls_for_platform("Linux")[1]
    assert status["download_url"] == fallback


def test_tarfile_error_is_plain_language() -> None:
    """An unpack failure must never leak a raw multi-line tarfile
    exception into the UI."""
    import tarfile

    from clipforge.ffmpeg_install import _plain_error

    message = _plain_error(
        tarfile.ReadError(
            "file could not be opened successfully:\n"
            "- method gz: ReadError('not a gzip file')\n"
            "- method xz: ReadError('not an lzma file')"
        )
    )
    assert "ReadError" not in message
    assert "damaged" in message.lower()
    assert "\n" not in message
