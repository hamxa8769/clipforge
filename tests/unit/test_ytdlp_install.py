"""Unit + API tests for the one-click yt-dlp installer (v0.8.1).

No real download ever runs here: the installer takes an injectable
``downloader`` that writes a fake build (a tiny shell script that
answers ``yt-dlp --version``), and the checksum fetch is monkeypatched.
The fake build exercises the real code paths — download, verify,
atomic swap, PATH prepend, settings save, failure cleanup.

Platform note: the fake binary is a POSIX shell script, not a real
ELF/Mach-O/MZ binary, so the install-path tests stub the magic-byte
check (``_check_binary_magic``) — the check itself is unit-tested
directly below with crafted byte prefixes. The Windows branch
(registry user-PATH update) is tested with a stubbed
``add_to_user_path_windows``, like the FFmpeg tests.

v0.9.1 detection tests (at the end of this file) simulate the Windows
installs the app used to miss — a pip user Scripts install and a
winget shim, neither on the process PATH — plus the re-check endpoint
and the found-but-broken report. The Windows platform is simulated by
monkeypatching ``platform.system`` and the APPDATA / LOCALAPPDATA
folders; the fake ``yt-dlp.exe`` is a shell script, which runs on
Linux via its shebang.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.ytdlp_install import (
    CHECKSUMS_URL,
    PINNED_BASE,
    RELEASES_LATEST_BASE,
    DownloadResult,
    YtDlpInstaller,
    _parse_sha256_sums,
    asset_for_platform,
    bundled_yt_dlp_path,
    download_url_for_platform,
    download_urls_for_platform,
    ensure_bundled_yt_dlp_on_path,
    resolve_yt_dlp_path,
    ytdlp_missing_message,
    ytdlp_version,
)

FAKE_VERSION = "2026.10.26"
FAKE_YTDLP_SCRIPT = f"#!/bin/sh\necho '{FAKE_VERSION}'\n".encode()


def _fake_downloader(payload: bytes):
    """A downloader that writes ``payload`` and reports progress like a real one."""

    def _download(url: str, dest: Path, progress_cb) -> DownloadResult:
        dest.write_bytes(payload)
        progress_cb(len(payload), len(payload))
        return DownloadResult(total_bytes=len(payload), final_url=url)

    return _download


@pytest.fixture(autouse=True)
def _restore_process_path(monkeypatch: pytest.MonkeyPatch):
    """A successful install prepends <data_dir>/yt-dlp to this
    process's PATH (that is the feature). In the test session that dir
    holds a *fake* yt-dlp, so restore PATH after every test — otherwise
    later tests would resolve the fake instead of the real one."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))


@pytest.fixture
def no_checksum(monkeypatch: pytest.MonkeyPatch):
    """Checksum file unreachable — install must still proceed (HTTPS source)."""
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: None)


@pytest.fixture
def no_magic_check(monkeypatch: pytest.MonkeyPatch):
    """The fake binary is a shell script, not a real ELF — stub the
    magic-byte check for install-path tests (it is unit-tested
    directly in test_magic_check_* below)."""
    monkeypatch.setattr(YtDlpInstaller, "_check_binary_magic", lambda self, binary: None)


def _wait_done(installer: YtDlpInstaller, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while installer.is_running and time.monotonic() < deadline:
        time.sleep(0.05)
    return installer.status()


def _installer(tmp_path: Path, **kwargs) -> YtDlpInstaller:
    kwargs.setdefault("downloader", _fake_downloader(FAKE_YTDLP_SCRIPT))
    return YtDlpInstaller(tmp_path, **kwargs)


# ---------------------------------------------------------------- sources
def test_download_urls_are_the_official_ones() -> None:
    assert download_url_for_platform("Windows") == f"{RELEASES_LATEST_BASE}/yt-dlp.exe"
    assert download_url_for_platform("Linux") == f"{RELEASES_LATEST_BASE}/yt-dlp_linux"
    assert download_url_for_platform("Darwin") == f"{RELEASES_LATEST_BASE}/yt-dlp_macos"
    assert download_url_for_platform("FreeBSD") is None
    assert asset_for_platform("Windows") == "yt-dlp.exe"
    assert asset_for_platform("Linux") == "yt-dlp_linux"
    assert asset_for_platform("Darwin") == "yt-dlp_macos"
    assert CHECKSUMS_URL == f"{RELEASES_LATEST_BASE}/SHA2-256SUMS"
    # Fallback is the version-pinned release verified at build time.
    assert download_urls_for_platform("Linux") == (
        f"{RELEASES_LATEST_BASE}/yt-dlp_linux",
        f"{PINNED_BASE}/yt-dlp_linux",
    )


def test_missing_message_points_at_the_install_button() -> None:
    message = ytdlp_missing_message()
    assert "yt-dlp is not installed" in message
    assert "Install yt-dlp" in message
    assert "github.com/yt-dlp/yt-dlp" in message


# ------------------------------------------------------- happy path (Linux binary)
def test_linux_install_succeeds_with_mocked_download(
    tmp_path: Path, no_checksum, no_magic_check, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    saved: dict[str, str] = {}
    installer = _installer(tmp_path, on_installed=lambda p: saved.setdefault("path", p))
    # force: the installer short-circuits to "already installed" when a
    # yt-dlp is already usable, and this machine may gain one.
    assert installer.start_install(force=True)["state"] in (
        "downloading",
        "verifying",
        "installing",
        "done",
    )
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["progress_pct"] == 100.0
    assert status["found"] is True
    assert status["version"] == FAKE_VERSION
    installed = tmp_path / "yt-dlp" / "yt-dlp"
    assert installed.is_file()
    assert os.access(installed, os.X_OK)
    assert saved.get("path") == str(installed)
    # No temp litter left behind.
    assert not list(tmp_path.glob(".ytdlp-install-*"))
    assert not (tmp_path / ".ytdlp-install-old").exists()
    # State file on disk agrees with the live state.
    assert json.loads((tmp_path / "yt-dlp-install-state.json").read_text())["state"] == "done"


def test_installed_binary_is_preferred_and_on_path(
    tmp_path: Path, no_checksum, no_magic_check
) -> None:
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done"
    assert resolve_yt_dlp_path(tmp_path) == str(tmp_path / "yt-dlp" / "yt-dlp")
    assert ensure_bundled_yt_dlp_on_path(tmp_path) is True
    assert str(tmp_path / "yt-dlp") in os.environ["PATH"].split(os.pathsep)


def test_second_start_without_force_reports_done(
    tmp_path: Path, no_checksum, no_magic_check
) -> None:
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    again = installer.start_install()
    assert again["state"] == "done"
    assert again["path"] == str(tmp_path / "yt-dlp" / "yt-dlp")


def test_force_reinstall_replaces_existing(tmp_path: Path, no_checksum, no_magic_check) -> None:
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    assert (tmp_path / "yt-dlp" / "yt-dlp").is_file()


def test_second_start_while_running_returns_live_status(
    tmp_path: Path, no_checksum, no_magic_check
) -> None:
    def _slow(url: str, dest: Path, progress_cb):
        time.sleep(1.0)
        dest.write_bytes(FAKE_YTDLP_SCRIPT)
        progress_cb(len(FAKE_YTDLP_SCRIPT), len(FAKE_YTDLP_SCRIPT))
        return DownloadResult(total_bytes=len(FAKE_YTDLP_SCRIPT), final_url=url)

    installer = YtDlpInstaller(tmp_path, downloader=_slow)
    first = installer.start_install(force=True)
    assert first["state"] in ("downloading", "verifying", "installing", "done")
    second = installer.start_install(force=True)
    assert second["state"] in ("downloading", "verifying", "installing", "done")
    assert _wait_done(installer)["state"] == "done"


def test_interrupted_run_is_not_stuck_running(tmp_path: Path) -> None:
    (tmp_path / "yt-dlp-install-state.json").write_text(
        json.dumps(
            {
                "state": "downloading",
                "progress_pct": 42.0,
                "downloaded_bytes": 100,
                "total_bytes": 1000,
                "message": "Downloading yt-dlp…",
                "error": "",
                "version": "",
                "path": "",
                "found": False,
                "download_url": download_url_for_platform("Linux"),
                "platform": "Linux",
                "path_added": None,
                "checksum_verified": None,
                "failed_step": "",
                "path_warning": "",
            }
        ),
        encoding="utf-8",
    )
    installer = YtDlpInstaller(tmp_path)
    status = installer.status()
    assert status["state"] == "error"
    assert "interrupted" in status["error"].lower()


# ---------------------------------------------------------------- checksum
def test_checksum_mismatch_fails_cleanly(
    tmp_path: Path, no_magic_check, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: "0" * 64)
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert "checksum" in status["error"].lower()
    assert status["failed_step"] == "verify"
    assert not (tmp_path / "yt-dlp").exists()
    assert not list(tmp_path.glob(".ytdlp-install-*"))


def test_matching_checksum_is_recorded(
    tmp_path: Path, no_magic_check, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(
        mod,
        "_fetch_expected_sha256",
        lambda *args: hashlib.sha256(FAKE_YTDLP_SCRIPT).hexdigest(),
    )
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["checksum_verified"] is True


def test_missing_checksum_is_recorded_not_hidden(
    tmp_path: Path, no_checksum, no_magic_check
) -> None:
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["checksum_verified"] is False


def test_parse_sha256_sums() -> None:
    text = (
        "e51e69e89489f199530f13d32a4342da3ddb2cf34842f364603a066db05afbf3  yt-dlp\n"
        "f5389d3698ecde4ef06f4e6b1ce0294fe9ac1396228c8990b8d3416d6d177a7d  yt-dlp.exe\n"
        "767ba9dce7052aa196f8b12652f433d76de128ba168966fa84b6394d5e6c6795 *yt-dlp_linux\n"
    )
    assert _parse_sha256_sums(text, "yt-dlp_linux") == (
        "767ba9dce7052aa196f8b12652f433d76de128ba168966fa84b6394d5e6c6795"
    )
    assert _parse_sha256_sums(text, "yt-dlp.exe").startswith("f5389d36")
    assert _parse_sha256_sums(text, "yt-dlp_win.zip") is None
    assert _parse_sha256_sums("", "yt-dlp_linux") is None


def test_checksum_fetch_survives_broken_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a bracketed IPv6 address in NO_PROXY makes httpx
    raise InvalidURL (not an HTTPError) before any byte moves. The
    checksum fetch must retry with trust_env=False instead of failing
    the whole install at the verify step."""
    import httpx

    import clipforge.ytdlp_install as mod

    wanted = hashlib.sha256(FAKE_YTDLP_SCRIPT).hexdigest()
    calls: list[dict] = []

    class _Resp:
        status_code = 200
        text = f"{wanted}  yt-dlp_linux\n"

    def _fake_get(url: str, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise httpx.InvalidURL("Invalid port: ':1]'")
        return _Resp()

    monkeypatch.setattr(mod.httpx, "get", _fake_get)
    assert mod._fetch_expected_sha256("yt-dlp_linux") == wanted
    assert len(calls) == 2
    assert calls[1].get("trust_env") is False


# ---------------------------------------------------------------- failures
def test_download_failure_leaves_no_partial_install(tmp_path: Path) -> None:
    def _boom(url: str, dest: Path, progress_cb):
        raise ConnectionError("no network here")

    installer = YtDlpInstaller(tmp_path, downloader=_boom)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert status["error"]  # plain-language message, not a bare traceback
    assert "Traceback" not in status["error"]
    assert status["failed_step"] == "download"
    assert not (tmp_path / "yt-dlp").exists()
    assert not list(tmp_path.glob(".ytdlp-install-*"))


def test_html_error_page_fails_cleanly(tmp_path: Path, no_checksum) -> None:
    """A proxy returning an HTML page instead of the binary is caught
    by the magic-byte check (real check here, not the stub)."""
    installer = YtDlpInstaller(
        tmp_path, downloader=_fake_downloader(b"<html><body>blocked</body></html>")
    )
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert "web page" in status["error"]
    assert not (tmp_path / "yt-dlp").exists()


def test_binary_that_does_not_run_fails_cleanly(
    tmp_path: Path, no_checksum, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download that is a valid ELF-looking file but does not run
    ``--version`` fails at verification, with no partial install."""
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(YtDlpInstaller, "_check_binary_magic", lambda self, binary: None)
    # The probe calls ytdlp_version(path, timeout=...) — the stub must
    # accept the keyword (v0.9.2: same call shape as v0.9.1).
    monkeypatch.setattr(mod, "ytdlp_version", lambda binary, *args, **kwargs: "")
    installer = YtDlpInstaller(tmp_path, downloader=_fake_downloader(b"\x7fELF" + b"\x00" * 100))
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert not (tmp_path / "yt-dlp").exists()


def test_truncated_download_fails_as_download(tmp_path: Path, no_checksum) -> None:
    def _short(url: str, dest: Path, progress_cb):
        dest.write_bytes(b"short")
        progress_cb(5, 100)
        return DownloadResult(total_bytes=100, final_url=url)

    installer = YtDlpInstaller(tmp_path, downloader=_short)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert "stopped" in status["error"].lower()
    assert status["failed_step"] == "download"


def test_all_sources_failing_says_so(tmp_path: Path, no_checksum, no_magic_check) -> None:
    def _boom(url: str, dest: Path, progress_cb):
        raise ConnectionError("no network here")

    installer = YtDlpInstaller(tmp_path, downloader=_boom)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "error", status
    assert "every official download source" in status["error"]


# ---------------------------------------------------------------- magic bytes
def test_magic_check_accepts_elf_and_rejects_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    checker = YtDlpInstaller(tmp_path)._check_binary_magic
    good = tmp_path / "good"
    good.write_bytes(b"\x7fELF" + b"\x00" * 10)
    checker(good)  # must not raise
    bad = tmp_path / "bad"
    bad.write_bytes(b"<html>not a binary</html>")
    with pytest.raises(ValueError, match="not a binary"):
        checker(bad)


def test_magic_check_accepts_mz_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    checker = YtDlpInstaller(tmp_path)._check_binary_magic
    good = tmp_path / "good.exe"
    good.write_bytes(b"MZ" + b"\x00" * 10)
    checker(good)  # must not raise
    bad = tmp_path / "bad.exe"
    bad.write_bytes(b"\x7fELF" + b"\x00" * 10)
    with pytest.raises(ValueError, match="not a binary"):
        checker(bad)


# ---------------------------------------------------------------- resolution
def test_resolve_prefers_bundled_over_path(
    tmp_path: Path, no_checksum, no_magic_check, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    # A different yt-dlp earlier on PATH must not win over the bundled one.
    other = tmp_path / "other-bin"
    other.mkdir()
    fake = other / "yt-dlp"
    fake.write_text("#!/bin/sh\necho '1999.01.01'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(other) + os.pathsep + os.environ.get("PATH", ""))
    assert resolve_yt_dlp_path(tmp_path) == str(tmp_path / "yt-dlp" / "yt-dlp")
    assert ytdlp_version(resolve_yt_dlp_path(tmp_path)) == FAKE_VERSION


def test_resolve_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "bin"
    other.mkdir()
    fake = other / "yt-dlp"
    fake.write_text("#!/bin/sh\necho '1999.01.01'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(other))
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    assert resolve_yt_dlp_path(tmp_path) == str(fake)


def test_bundled_path_layout(tmp_path: Path) -> None:
    assert bundled_yt_dlp_path(tmp_path) == tmp_path / "yt-dlp" / "yt-dlp"
    assert bundled_yt_dlp_path(tmp_path, "Windows") == tmp_path / "yt-dlp" / "yt-dlp.exe"


def test_ensure_bundled_on_path_needs_a_binary(tmp_path: Path) -> None:
    assert ensure_bundled_yt_dlp_on_path(tmp_path) is False


# ---------------------------------------------------------------- Windows branch (mocked platform)
def test_windows_install_calls_user_path_update(
    tmp_path: Path, no_checksum, no_magic_check, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "add_to_user_path_windows",
        lambda bin_dir: (
            calls.append(str(bin_dir)) or {"ok": True, "already_present": False, "error": ""}
        ),
    )
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    status = _wait_done(installer)
    assert status["state"] == "done", status
    assert status["path_added"] is True
    assert (tmp_path / "yt-dlp" / "yt-dlp.exe").is_file()
    assert calls == [str(tmp_path / "yt-dlp")]


# ---------------------------------------------------------------- API endpoints
@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: None)
    monkeypatch.setattr(YtDlpInstaller, "_check_binary_magic", lambda self, binary: None)
    import importlib

    app_mod = importlib.import_module("clipforge_api.app")
    monkeypatch.setattr(app_mod, "_YTDLP_INSTALLERS", {})
    installer = YtDlpInstaller(
        tmp_path,
        downloader=_fake_downloader(FAKE_YTDLP_SCRIPT),
        on_installed=lambda p: app_mod._settings_store().set_yt_dlp_path(p),
    )
    app_mod._YTDLP_INSTALLERS[str(tmp_path)] = installer
    with TestClient(app_mod.app) as client:
        yield client, installer


def test_api_status_reports_idle_before_install(api_client) -> None:
    client, _ = api_client
    resp = client.get("/yt-dlp/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    # found/path reflect this machine (yt-dlp may already be on PATH
    # here); the install state itself is what this test pins down.
    assert body["download_url"] == download_url_for_platform()


def test_api_install_then_status_reports_done(api_client, monkeypatch: pytest.MonkeyPatch) -> None:
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod, "_fetch_expected_sha256", lambda *args: None)
    client, installer = api_client
    resp = client.post("/yt-dlp/install?force=true")
    assert resp.status_code == 202
    assert resp.json()["state"] in ("downloading", "verifying", "installing", "done")
    status = _wait_done(installer)
    assert status["state"] == "done", status
    body = client.get("/yt-dlp/status").json()
    assert body["state"] == "done"
    assert body["found"] is True
    assert body["path"].endswith("/yt-dlp/yt-dlp")
    # GET /settings now reports the installed yt-dlp too.
    settings = client.get("/settings").json()
    assert settings["yt_dlp"]["found"] is True
    assert settings["yt_dlp"]["path"] == body["path"]
    assert settings["yt_dlp"]["version"] == FAKE_VERSION


def test_api_install_is_idempotent_while_running(api_client) -> None:
    client, installer = api_client
    first = client.post("/yt-dlp/install?force=true")
    assert first.status_code == 202
    second = client.post("/yt-dlp/install?force=true")
    assert second.status_code == 202
    assert _wait_done(installer)["state"] == "done"


# ---------------------------------------------------------------- ingest / doctor wiring
def test_ingest_youtube_without_yt_dlp_names_the_install_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge.ingest import IngestError, ingest_youtube

    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    # HOME too: v0.9.1 detection also checks ~/.local/bin, so a real
    # yt-dlp there would otherwise be found and the download attempted.
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            own_content=True,
            out_dir=tmp_path / "uploads",
            data_dir=tmp_path,
        )
    assert "Install yt-dlp" in str(excinfo.value)


def test_check_dependencies_counts_bundled_yt_dlp(
    tmp_path: Path, no_checksum, no_magic_check, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge.workflow import check_dependencies

    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    # HOME too: v0.9.1 detection also checks ~/.local/bin, so a real
    # yt-dlp there would otherwise count as installed before the test.
    monkeypatch.setenv("HOME", str(tmp_path))
    before = {d.name: d.available for d in check_dependencies()}
    assert before["yt-dlp"] is False
    installer = _installer(tmp_path)
    installer.start_install(force=True)
    assert _wait_done(installer)["state"] == "done"
    after = {d.name: d.available for d in check_dependencies()}
    assert after["yt-dlp"] is True


# ------------------------------------------------------- detection (v0.9.1)
# Regression tests for the user report "yt-dlp IS installed (via
# PowerShell) but the app says it is missing": the old detection only
# checked the stored path, the bundled dir, and shutil.which() on the
# process PATH — which misses a pip user Scripts install (never on
# PATH) and any install made after the server started (frozen PATH).

FAKE_YTDLP_EXE_SCRIPT = f"#!/bin/sh\necho '{FAKE_VERSION}'\n".encode()


def _fake_windows_exe(directory: Path) -> Path:
    """A fake yt-dlp.exe (shell script) in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "yt-dlp.exe"
    fake.write_bytes(FAKE_YTDLP_EXE_SCRIPT)
    fake.chmod(0o755)
    return fake


@pytest.fixture
def windows_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Simulate Windows: platform + per-user folders under tmp_path."""
    import clipforge.ytdlp_install as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    appdata = tmp_path / "AppData" / "Roaming"
    localappdata = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    return tmp_path


def test_resolve_finds_pip_user_scripts_location_not_on_path(tmp_path: Path, windows_env) -> None:
    """The reported case: `pip install yt-dlp` puts yt-dlp.exe in
    %APPDATA%\\Python\\Python3XX\\Scripts, which is NOT on PATH — the
    old shutil.which() detection missed it entirely."""
    from clipforge.ytdlp_install import probe_yt_dlp, resolve_yt_dlp_path

    scripts = tmp_path / "AppData" / "Roaming" / "Python" / "Python313" / "Scripts"
    fake = _fake_windows_exe(scripts)
    assert resolve_yt_dlp_path(tmp_path, system="Windows") == str(fake)
    probe = probe_yt_dlp(tmp_path, system="Windows")
    assert probe["found"] is True
    assert probe["version"] == FAKE_VERSION
    assert probe["path"] == str(fake)
    assert "pip install" in probe["source"]


def test_resolve_finds_winget_shim_location_not_on_path(tmp_path: Path, windows_env) -> None:
    """A winget install (WindowsApps shim) is found even when the
    process PATH does not contain the shim dir."""
    from clipforge.ytdlp_install import probe_yt_dlp, resolve_yt_dlp_path

    shim_dir = tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    fake = _fake_windows_exe(shim_dir)
    assert resolve_yt_dlp_path(tmp_path, system="Windows") == str(fake)
    probe = probe_yt_dlp(tmp_path, system="Windows")
    assert probe["found"] is True
    assert probe["source"] == "winget (WindowsApps)"


def test_status_reports_version_path_and_source_for_path_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detection finds a PATH-installed yt-dlp and the status reports
    its version, its path, and where it was found."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "yt-dlp"
    fake.write_bytes(FAKE_YTDLP_SCRIPT)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    status = YtDlpInstaller(tmp_path).status()
    assert status["found"] is True
    assert status["version"] == FAKE_VERSION
    assert status["path"] == str(fake)
    assert status["source"] == "PATH"
    assert status["broken"] is False


def test_api_recheck_finds_path_install_after_server_start(
    api_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale-status regression: the server started before yt-dlp
    was installed, so GET /yt-dlp/status says missing. Installing it
    into a new PATH dir and POSTing /yt-dlp/recheck finds it — no
    server restart, no one-click install."""
    client, _installer = api_client
    empty = tmp_path / "empty-path"
    empty.mkdir()
    new_dir = tmp_path / "newbin"
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert client.get("/yt-dlp/status").json()["found"] is False
    # The user installs yt-dlp (PowerShell) after the server started.
    new_dir.mkdir()
    fake = new_dir / "yt-dlp"
    fake.write_bytes(FAKE_YTDLP_SCRIPT)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(new_dir) + os.pathsep + str(empty))
    resp = client.post("/yt-dlp/recheck")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True, body
    assert body["version"] == FAKE_VERSION
    assert body["path"] == str(fake)
    assert body["source"] == "PATH"
    assert body["path_refreshed"] == []
    # The fresh result sticks: a plain status poll agrees.
    assert client.get("/yt-dlp/status").json()["found"] is True


def test_recheck_refreshes_path_from_windows_registry(
    tmp_path: Path, windows_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows core of the fix: the registry PATH gains the install
    dir after the server started. Re-check merges it into the running
    process's PATH and finds the binary — the old code needed a full
    app restart for this."""
    new_dir = tmp_path / "newbin"
    fake = _fake_windows_exe(new_dir)
    installer = YtDlpInstaller(tmp_path)
    assert installer.status()["found"] is False
    assert str(new_dir) not in os.environ["PATH"].split(os.pathsep)
    # The user installs yt-dlp; the registry PATH gains the new dir
    # (the running process's PATH does not). v0.9.2: the registry
    # reader lives in clipforge.ffmpeg_install (shared with the FFmpeg
    # probe) — stub it there, where refresh_process_path_from_registry
    # looks it up.
    import clipforge.ffmpeg_install as ffmpeg_mod

    monkeypatch.setattr(ffmpeg_mod, "_read_windows_registry_path", lambda: [str(new_dir)])
    status = installer.recheck()
    assert status["found"] is True, status
    assert status["version"] == FAKE_VERSION
    assert status["path"] == str(fake)
    assert status["path_refreshed"] == [str(new_dir)]
    assert str(new_dir) in os.environ["PATH"].split(os.pathsep)


def test_status_reports_broken_binary_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A yt-dlp file that exists but does not run (--version fails)
    is reported as broken with its path — never as 'found' / ready."""
    bin_dir = tmp_path / "brokenbin"
    bin_dir.mkdir()
    fake = bin_dir / "yt-dlp"
    fake.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    status = YtDlpInstaller(tmp_path).status()
    assert status["found"] is False
    assert status["broken"] is True
    assert status["path"] == str(fake)
    assert status["version"] == ""
    assert str(fake) in status["broken_reason"]
    assert "does not run" in status["broken_reason"]


def test_probe_prefers_working_binary_over_broken_path_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken yt-dlp earlier on PATH must not shadow a working one
    later on PATH."""
    from clipforge.ytdlp_install import probe_yt_dlp

    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    broken = broken_dir / "yt-dlp"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(0o755)
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good = good_dir / "yt-dlp"
    good.write_bytes(FAKE_YTDLP_SCRIPT)
    good.chmod(0o755)
    monkeypatch.setenv("PATH", str(broken_dir) + os.pathsep + str(good_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    probe = probe_yt_dlp(tmp_path)
    assert probe["found"] is True
    assert probe["path"] == str(good)
    assert probe["version"] == FAKE_VERSION
    assert probe["broken"] is False


def test_require_tool_finds_scripts_location(
    tmp_path: Path, windows_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker gate that blocked the agent run (require_tool /
    check_dependencies) uses the same full detection, so a pip
    Scripts install unblocks the run without a restart."""
    from clipforge.workflow import MissingDependencyError, check_dependencies, require_tool

    scripts = tmp_path / "AppData" / "Roaming" / "Python" / "Python313" / "Scripts"
    _fake_windows_exe(scripts)
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    require_tool("yt-dlp", "yt-dlp downloads the YouTube source", "Install yt-dlp")
    deps = {d.name: d.available for d in check_dependencies()}
    assert deps["yt-dlp"] is True
    # And it still raises when nothing is installed anywhere.
    missing_env = tmp_path / "no-yt-dlp-here"
    missing_env.mkdir()
    monkeypatch.setenv("APPDATA", str(missing_env))
    monkeypatch.setenv("LOCALAPPDATA", str(missing_env))
    monkeypatch.setenv("HOME", str(missing_env))
    with pytest.raises(MissingDependencyError):
        require_tool("yt-dlp", "yt-dlp downloads the YouTube source", "Install yt-dlp")


def test_settings_ytdlp_info_reports_source_and_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /settings reports the same verified detection (version +
    path + source, broken plainly)."""
    from clipforge.auth.settings_store import ytdlp_info

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "yt-dlp"
    fake.write_bytes(FAKE_YTDLP_SCRIPT)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLIPFORGE_DATA_DIR", raising=False)
    info = ytdlp_info(tmp_path)
    assert info["found"] is True
    assert info["version"] == FAKE_VERSION
    assert info["path"] == str(fake)
    assert info["source"] == "PATH"
    fake.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    info = ytdlp_info(tmp_path)
    assert info["found"] is False
    assert info["broken"] is True
    assert info["path"] == str(fake)
