"""One-click yt-dlp installer for ClipForge v0.8.1.

The Agent run was blocked at Ingest with ``yt_dlp_missing`` — the
same class of bug FFmpeg had in v0.4.0: a required tool is missing and
the app only errors instead of installing it. This module does the
whole install inside the app, mirroring
:mod:`clipforge.ffmpeg_install` (same structure: background install,
pollable state, progress, plain-language errors, retry, verified
download, atomic swap into the app data dir, bundled path used first
so no PATH restart is needed):

* Download the official release binary for the platform, with progress.
* Verify the published SHA-256 checksum from the release's
  ``SHA2-256SUMS`` file when it is reachable (strict: a mismatch is a
  hard failure, never a silent skip; a missing sums file is recorded
  honestly as ``checksum_verified: false``).
* Verify the downloaded binary runs (``yt-dlp --version``), then move
  it into place atomically — a failed install never leaves a
  half-installed ``<data_dir>/yt-dlp`` behind.
* Remember the installed binary (``settings.json`` ``yt_dlp_path`` via
  the ``on_installed`` callback) and prepend its folder to this
  process's PATH, so ingest / live capture / channel fetch work
  immediately with no restart.
* On Windows, also add the folder to the *user* PATH (registry
  ``HKCU\\Environment``, via :func:`clipforge.ffmpeg_install.add_to_user_path_windows`),
  so future runs and other tools find it too.

Download sources (verified 2026-11-22 — the live ``SHA2-256SUMS``
for the latest release, 2026.10.26, was fetched and lists every asset
below):

* Windows: ``https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe``
  — the "Windows (Win8+) standalone x64 binary (recommended for
  Windows)" file from the official yt-dlp README / releases page.
* Linux: ``https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux``
  — the standalone Linux executable. (The plain ``yt-dlp`` asset is a
  zipimport binary that needs a Python interpreter; the standalone
  ``yt-dlp_linux`` needs none, so it is the right one-click choice.)
* macOS: ``https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos``
  — the universal macOS standalone executable. Unlike FFmpeg, yt-dlp
  ships a macOS binary, so one-click install works on all three
  platforms.

Checksum: every release publishes ``SHA2-256SUMS`` next to the binaries
(``.../releases/latest/download/SHA2-256SUMS``, GNU-style
``<sha256>  <filename>`` lines). The installer fetches it and checks
the downloaded file against the line for its asset. The release also
publishes ``SHA2-256SUMS.sig`` (a GPG signature), but verifying it
needs GPG/minisign, which is not available inside the app — the
checksum over HTTPS from github.com is the verification, and the
module says so instead of pretending the signature was checked.

When the primary download fails the installer retries it once, then
tries the version-pinned fallback URL for the release verified at
build time (2026.10.26) — ``.../releases/download/2026.10.26/<asset>``.
Only official github.com/yt-dlp release URLs, never a random mirror.
A candidate only counts once the binary is downloaded, checksum
checked, and run: a source that answers HTTP 200 with an error page
(a proxy or captive portal returning HTML instead of the binary) is
caught by a magic-byte check right after the download, and a binary
that fails ``--version`` moves on to the next source instead of
failing the install. Every failure names the step that failed
(``failed_step``: download / verify / install) in plain language.

The installer is deliberately dependency-light (stdlib + httpx, which
the API already uses) and side-effect free to import: creating a
``YtDlpInstaller`` only reads/writes state under ``data_dir`` when an
install actually runs. The download function is injectable so tests
can install a fake build without touching the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from clipforge.ffmpeg_install import add_to_user_path_windows

# ---------------------------------------------------------------- sources
#: ``.../releases/latest/download/`` — always the newest stable release.
RELEASES_LATEST_BASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
#: Version-pinned fallback base for the release verified at build time
#: (2026.10.26, checked 2026-11-22): if the ``latest`` redirect ever
#: breaks, the pinned release still serves the same assets.
PINNED_VERSION = "2026.10.26"
PINNED_BASE = f"https://github.com/yt-dlp/yt-dlp/releases/download/{PINNED_VERSION}"
#: Published checksum manifest, next to the binaries in every release.
CHECKSUMS_URL = f"{RELEASES_LATEST_BASE}/SHA2-256SUMS"
#: The page the manual-install link points at.
YTDLP_DOWNLOAD_PAGE = "https://github.com/yt-dlp/yt-dlp/releases/latest"
#: Per-platform asset names (official release files, README "RELEASE FILES").
_ASSETS = {
    "Windows": "yt-dlp.exe",
    "Linux": "yt-dlp_linux",
    "Darwin": "yt-dlp_macos",
}

INSTALL_DIR_NAME = "yt-dlp"
STATE_FILE_NAME = "yt-dlp-install-state.json"

# Install states, in order. ``unsupported`` is terminal (unknown OS);
# ``done`` / ``error`` are terminal for a run; ``idle`` means never run.
# (No "extracting" state: yt-dlp ships as a single binary, not an archive.)
STATES = (
    "idle",
    "downloading",
    "verifying",
    "installing",
    "done",
    "error",
    "unsupported",
)
# Progress ranges per phase, so the UI bar moves smoothly through a run.
_PHASE_PROGRESS = {
    "downloading": (0, 70),
    "verifying": (70, 80),
    "installing": (80, 100),
}

#: Plain-language reasons for the failures users actually hit.
_NO_INTERNET = "Could not reach the download server. Check your internet connection and try again."
_CHECKSUM_MISMATCH = (
    "The downloaded file did not match its published checksum, so it was discarded — "
    "it may have been damaged in transit. Try again."
)
_TIMEOUT = "The download timed out. Check your internet connection and try again."
_NO_BINARY = (
    "The download finished but did not contain a working yt-dlp program. "
    "Try again, or install yt-dlp manually from "
    f"{YTDLP_DOWNLOAD_PAGE}."
)
_INCOMPLETE_DOWNLOAD = (
    "The download stopped before it finished, so it was discarded. "
    "Check your internet connection and try again."
)
_NOT_A_BINARY = (
    "The download server returned a web page instead of the yt-dlp program "
    "(this can happen behind a proxy or a captive portal). Check your "
    "internet connection and try again."
)

#: Human step label shown in front of an install error, so the UI can
#: say *where* it failed (download / verify / install).
_STEP_LABELS = {
    "download": "Download failed",
    "verify": "Verification failed",
    "install": "Install failed",
}


def ytdlp_missing_message() -> str:
    """Plain-language error for a missing yt-dlp binary.

    Used by ingest / agent / live / trending and by the worker's
    blocked-job records, so the message is identical everywhere: no
    traceback, no bare ``FileNotFoundError``, just what is missing and
    the one-click fix (the in-app Install button), with the manual
    download page as the fallback.
    """
    return (
        "yt-dlp is not installed — ClipForge needs it to download YouTube sources. "
        "Press 'Install yt-dlp' in ClipForge (Pipeline or Agent page, or "
        "Settings) to install it automatically, or get it from "
        f"{YTDLP_DOWNLOAD_PAGE} and make sure it is on PATH."
    )


def asset_for_platform(system: str | None = None) -> str | None:
    """Official release asset name for this OS, or ``None`` when unknown."""
    return _ASSETS.get(system or platform.system())


def download_url_for_platform(system: str | None = None) -> str | None:
    """Official download URL for this OS, or ``None`` when unsupported."""
    asset = asset_for_platform(system)
    return f"{RELEASES_LATEST_BASE}/{asset}" if asset else None


def download_urls_for_platform(system: str | None = None) -> tuple[str, ...]:
    """Primary + fallback download URLs for this OS, in try order."""
    asset = asset_for_platform(system)
    if asset is None:
        return ()
    return (
        f"{RELEASES_LATEST_BASE}/{asset}",
        f"{PINNED_BASE}/{asset}",
    )


def _binary_name(system: str | None = None) -> str:
    return "yt-dlp.exe" if (system or platform.system()) == "Windows" else "yt-dlp"


def bundled_yt_dlp_path(data_dir: str | Path | None, system: str | None = None) -> Path:
    """Where an installer-managed yt-dlp binary lives (may not exist yet)."""
    base = Path(data_dir) if data_dir else Path(os.environ.get("CLIPFORGE_DATA_DIR") or "./data")
    return base / INSTALL_DIR_NAME / _binary_name(system)


def _is_usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_yt_dlp_path(
    data_dir: str | Path | None = None,
    stored_path: str | None = None,
    system: str | None = None,
) -> str | None:
    """Best yt-dlp binary to run, in priority order.

    1. The path saved in settings after a one-click install.
    2. The installer-managed ``<data_dir>/yt-dlp/`` binary.
    3. Whatever is on PATH (a manual install).
    """
    if stored_path:
        candidate = Path(stored_path)
        if _is_usable(candidate):
            return str(candidate)
    bundled = bundled_yt_dlp_path(data_dir, system)
    if _is_usable(bundled):
        return str(bundled)
    # A bundled binary on a non-executable filesystem (or a download
    # that lost its exec bit) is still usable — it just needs invoking
    # by path; the installer chmods it on install, this is the fallback.
    if bundled.is_file():
        return str(bundled)
    return shutil.which("yt-dlp")


def ytdlp_version(binary: str | Path) -> str:
    """``yt-dlp --version`` output (``""`` when it does not run)."""
    try:
        proc = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip().splitlines()[0].strip() if proc.stdout else ""


def ensure_bundled_yt_dlp_on_path(data_dir: str | Path | None, system: str | None = None) -> bool:
    """Prepend the bundled yt-dlp dir to this process's PATH.

    This is what makes ingest / live capture / channel fetch work
    immediately after an install with no restart and no logoff: every
    ``yt-dlp`` call in this process (``shutil.which`` included)
    resolves to the bundled binary. Returns True when the dir was added
    (or was already there).
    """
    install_dir = str(bundled_yt_dlp_path(data_dir, system).parent)
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if any(os.path.normcase(p) == os.path.normcase(install_dir) for p in parts):
        return True
    if not bundled_yt_dlp_path(data_dir, system).is_file():
        return False
    os.environ["PATH"] = install_dir + (os.pathsep + path if path else "")
    return True


# ---------------------------------------------------------------- download
@dataclass
class DownloadResult:
    """What a download produced: the advertised size and the URL the
    download actually came from (after redirects).
    """

    total_bytes: int | None = None
    final_url: str | None = None


#: ``downloader(url, dest, progress_cb) -> DownloadResult``.
#: ``progress_cb(downloaded, total_or_None)`` is called per chunk.
Downloader = Callable[[str, Path, Callable[[int, int | None], None]], "DownloadResult | int | None"]


def _as_download_result(
    result: DownloadResult | int | None, url: str
) -> tuple[int | None, str | None]:
    """Normalise a downloader's return value to (total, final_url)."""
    if isinstance(result, DownloadResult):
        return result.total_bytes, result.final_url or url
    if isinstance(result, int):
        return result, url
    return None, url


def _download_with_httpx(
    url: str, dest: Path, progress_cb: Callable[[int, int | None], None]
) -> DownloadResult:
    """Stream a URL to ``dest`` with progress. Follows redirects.

    A malformed proxy entry in the environment raises
    ``httpx.InvalidURL`` before any byte moves — retry once with the
    proxy taken explicitly from the environment (``trust_env=False``)
    instead of failing the install for a proxy the download never
    needed. (Same behaviour as the FFmpeg installer.)
    """
    try:
        return _stream_once(url, dest, progress_cb, trust_env=True, proxy=None)
    except httpx.InvalidURL:
        return _stream_once(url, dest, progress_cb, trust_env=False, proxy=_proxy_url_from_env())


def _proxy_url_from_env() -> str | None:
    """HTTPS proxy from the environment, if one is configured."""
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def _stream_once(
    url: str,
    dest: Path,
    progress_cb: Callable[[int, int | None], None],
    *,
    trust_env: bool,
    proxy: str | None,
) -> DownloadResult:
    total: int | None = None
    downloaded = 0
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=60.0, trust_env=trust_env, proxy=proxy
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers["content-length"]) if "content-length" in resp.headers else None
        final_url = str(resp.url)
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                fh.write(chunk)
                downloaded += len(chunk)
                progress_cb(downloaded, total)
    return DownloadResult(total_bytes=total, final_url=final_url)


def _sha256_file(path: Path) -> str:
    """SHA-256 of a file, streamed (a yt-dlp binary is ~20–40 MB)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_sha256_sums(text: str, wanted_filename: str) -> str | None:
    """SHA-256 for ``wanted_filename`` in a GNU-style sums file.

    Lines look like ``<sha256>  <filename>`` (two spaces) or
    ``<sha256> *<filename>`` (binary marker). Returns None when the
    file has no line for this asset.
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            token, name = parts[0].strip().lower(), parts[1].lstrip("*").strip()
            if (
                name == wanted_filename
                and len(token) == 64
                and all(c in "0123456789abcdef" for c in token)
            ):
                return token
    return None


def _fetch_expected_sha256(asset: str) -> str | None:
    """Published SHA-256 for ``asset`` from the release's SHA2-256SUMS.

    Returns None when the sums file is missing/unreachable — the
    checksum is best-effort (same honesty rule as the FFmpeg
    installer): the download still came over HTTPS from github.com,
    but the caller records that it could NOT verify it.

    A malformed proxy entry in the environment (e.g. a bracketed IPv6
    address in ``NO_PROXY`` that httpx cannot parse) raises
    ``httpx.InvalidURL`` before any byte moves — retry once with the
    proxy taken explicitly from the environment (``trust_env=False``)
    instead of failing the install for a proxy the checksum fetch
    never needed. (Same behaviour as the download and as the FFmpeg
    installer's checksum fetch. ``InvalidURL`` is NOT an
    ``httpx.HTTPError`` subclass, so it needs its own except arm —
    catching only ``HTTPError`` here used to fail the whole install
    at the verify step on machines with such a proxy entry.)
    """
    try:
        resp = _get_text(CHECKSUMS_URL, trust_env=True, proxy=None)
    except httpx.InvalidURL:
        try:
            resp = _get_text(CHECKSUMS_URL, trust_env=False, proxy=_proxy_url_from_env())
        except (httpx.HTTPError, ValueError, IndexError):
            return None
    except (httpx.HTTPError, ValueError, IndexError):
        return None
    if resp.status_code != 200:
        return None
    try:
        return _parse_sha256_sums(resp.text, asset)
    except (ValueError, IndexError):
        return None


def _get_text(url: str, *, trust_env: bool, proxy: str | None) -> httpx.Response:
    return httpx.get(url, follow_redirects=True, timeout=30.0, trust_env=trust_env, proxy=proxy)


def _plain_error(exc: BaseException) -> str:
    """Map a failure to the plain-language message the UI shows."""
    if isinstance(exc, httpx.ConnectError):
        return _NO_INTERNET
    if isinstance(exc, httpx.InvalidURL):
        return _NO_INTERNET
    if isinstance(exc, httpx.TimeoutException):
        return _TIMEOUT
    if isinstance(exc, httpx.HTTPStatusError):
        return (
            f"The download server answered HTTP {exc.response.status_code}. "
            "Check your internet connection and try again."
        )
    if isinstance(exc, httpx.HTTPError):
        return _NO_INTERNET
    if isinstance(exc, OSError) and exc.errno == 28:
        return (
            "Not enough free disk space to install yt-dlp (about 40 MB). "
            "Free some space and try again."
        )
    if isinstance(exc, ValueError) and "checksum mismatch" in str(exc):
        return _CHECKSUM_MISMATCH
    if isinstance(exc, ValueError) and "stopped early" in str(exc):
        return _INCOMPLETE_DOWNLOAD
    if isinstance(exc, ValueError) and "not a binary" in str(exc):
        return _NOT_A_BINARY
    if isinstance(exc, FileNotFoundError):
        return _NO_BINARY
    text = str(exc) or exc.__class__.__name__
    return f"{text} Try again."


# ---------------------------------------------------------------- installer
@dataclass
class InstallState:
    """Pollable install state (also written to ``install-state.json``)."""

    state: str = "idle"
    progress_pct: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    message: str = ""
    error: str = ""
    version: str = ""
    path: str = ""
    found: bool = False
    download_url: str = ""
    platform: str = field(default_factory=platform.system)
    path_added: bool | None = None
    checksum_verified: bool | None = None
    #: Step that failed ("" when nothing failed): download / verify /
    #: install. The UI shows it so the user knows *where* the install
    #: stopped instead of guessing from a bare error.
    failed_step: str = ""
    #: Non-fatal Windows PATH warning on an otherwise successful install
    #: ("" when the PATH update worked or did not apply).
    path_warning: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "progress_pct": round(self.progress_pct, 1),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "message": self.message,
            "error": self.error,
            "version": self.version,
            "path": self.path,
            "found": self.found,
            "download_url": self.download_url,
            "platform": self.platform,
            "path_added": self.path_added,
            "checksum_verified": self.checksum_verified,
            "failed_step": self.failed_step,
            "path_warning": self.path_warning,
        }


class YtDlpInstaller:
    """One-click yt-dlp install into ``<data_dir>/yt-dlp/``.

    ``on_installed`` is called with the installed ``yt-dlp`` path on
    success (the API wires it to saving ``yt_dlp_path`` in settings);
    a failing callback never fails the install itself.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        downloader: Downloader | None = None,
        on_installed: Callable[[str], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.install_dir = self.data_dir / INSTALL_DIR_NAME
        self.state_path = self.data_dir / STATE_FILE_NAME
        self._downloader = downloader or _download_with_httpx
        self._on_installed = on_installed
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_state_write = 0.0
        # Step the current run is in (download / verify / install); the
        # error handler reports it as ``failed_step``.
        self._phase = "download"
        # True once a fallback download source has been tried, so the
        # error can say every official source failed.
        self._tried_fallback = False
        self._state = InstallState(download_url=download_url_for_platform() or "")
        self._load_state_file()

    # ------------------------------------------------------------ state
    def _load_state_file(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and raw.get("state") in STATES:
            # A crashed run must not look "running" forever.
            if raw["state"] in ("downloading", "verifying", "installing"):
                raw["state"] = "error"
                raw["error"] = (
                    "The install was interrupted — ClipForge closed while it was running. "
                    "Try again."
                )
            self._state = InstallState(
                **{k: v for k, v in raw.items() if k in InstallState.__dataclass_fields__}
            )

    def _set_state(self, **updates: object) -> None:
        with self._lock:
            for key, value in updates.items():
                setattr(self._state, key, value)
            snapshot = self._state.to_dict()
            failed = "state" in updates and updates["state"] == "failed"
            failed_step = self._state.failed_step
            failed_error = self._state.error
        if failed:
            # v0.9.0: a failed install is a notification, not a silent
            # red state the user has to stumble on (see
            # clipforge.notifications). Best-effort: never breaks the
            # installer when the DB is unavailable.
            from clipforge.notifications import notify_best_effort  # noqa: PLC0415

            step = f" ({failed_step})" if failed_step else ""
            notify_best_effort(
                "error",
                "yt-dlp install failed",
                f"yt-dlp could not be installed{step}: {failed_error or 'unknown error'} "
                "Press 'Install yt-dlp' in Settings to try again.",
                action={"label": "Open Settings", "url": "/settings"},
                dedupe_key="install-failed:yt-dlp",
            )
        # Progress callbacks fire per 256 KiB chunk; writing the state
        # file every time would hammer the disk, so throttle file writes
        # to ~2/s (state changes always write).
        now = time.monotonic()
        state_changed = "state" in updates
        if not state_changed and now - self._last_state_write < 0.5:
            return
        self._last_state_write = now
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        except OSError:
            pass  # state file is best-effort; the in-memory state is live

    def status(self) -> dict:
        """Current install state, enriched with live yt-dlp detection."""
        with self._lock:
            snapshot = self._state.to_dict()
        found_path = resolve_yt_dlp_path(self.data_dir)
        snapshot["found"] = bool(found_path)
        snapshot["path"] = found_path or snapshot["path"]
        if found_path:
            snapshot["version"] = ytdlp_version(found_path) or snapshot["version"]
        if not snapshot["download_url"]:
            snapshot["download_url"] = download_url_for_platform() or ""
        return snapshot

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ start
    def start_install(self, *, force: bool = False) -> dict:
        """Start the install in a background thread (idempotent).

        A second call while a run is active returns the live status
        instead of starting a duplicate download. When yt-dlp is
        already installed, returns the ``done`` status unless ``force``
        re-installs. Unsupported platforms return ``unsupported``.
        """
        if self.is_running:
            return self.status()
        url = download_url_for_platform()
        if url is None:
            self._set_state(
                state="unsupported",
                progress_pct=0.0,
                message="",
                error=(
                    "One-click install is not available on this system yet. "
                    "Install yt-dlp manually from "
                    f"{YTDLP_DOWNLOAD_PAGE}, then restart ClipForge."
                ),
                failed_step="",
                path_warning="",
            )
            return self.status()
        existing = resolve_yt_dlp_path(self.data_dir)
        if existing and not force:
            version = ytdlp_version(existing)
            self._set_state(
                state="done",
                progress_pct=100.0,
                message="yt-dlp is already installed.",
                error="",
                version=version,
                path=existing,
                found=True,
                failed_step="",
                path_warning="",
            )
            return self.status()
        self._set_state(
            state="downloading",
            progress_pct=0.0,
            downloaded_bytes=0,
            total_bytes=None,
            message="Downloading yt-dlp…",
            error="",
            path_added=None,
            checksum_verified=None,
            failed_step="",
            path_warning="",
        )
        thread = threading.Thread(
            target=self._run_install, args=(url,), daemon=True, name="ytdlp-install"
        )
        self._thread = thread
        thread.start()
        return self.status()

    # ------------------------------------------------------------ worker
    def _run_install(self, url: str) -> None:
        system = platform.system()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._phase = "download"
        self._tried_fallback = False
        try:
            with tempfile.TemporaryDirectory(prefix=".ytdlp-install-", dir=self.data_dir) as tmp:
                tmpdir = Path(tmp)
                binary = tmpdir / _binary_name(system)
                self._download_verify(url, binary, system)
                self._phase = "install"
                self._set_state(state="installing", progress_pct=80.0, message="Installing yt-dlp…")
                self._swap_into_place(binary, system)
                installed = bundled_yt_dlp_path(self.data_dir, system)
                version = ytdlp_version(installed)
                if not version:
                    raise FileNotFoundError("installed yt-dlp did not run")
                ensure_bundled_yt_dlp_on_path(self.data_dir, system)
                path_added: bool | None = None
                path_warning = ""
                if system == "Windows":
                    result = add_to_user_path_windows(self.install_dir)
                    path_added = bool(result["ok"])
                    if not result["ok"]:
                        # Non-fatal: yt-dlp already works inside
                        # ClipForge; only new terminals would miss it.
                        path_warning = result["error"].replace("FFmpeg", "yt-dlp")
                self._notify_installed(str(installed))
                message = "yt-dlp is installed and ready — YouTube ingest will work now."
                if path_added:
                    message += (
                        " It was also added to your Windows PATH, so new terminals find it too."
                    )
                elif path_warning:
                    message += f" {path_warning}"
                self._set_state(
                    state="done",
                    progress_pct=100.0,
                    message=message,
                    error="",
                    version=version,
                    path=str(installed),
                    found=True,
                    path_added=path_added,
                    path_warning=path_warning,
                    failed_step="",
                )
        except Exception as exc:  # noqa: BLE001  (install must report, not crash)
            error = _plain_error(exc)
            label = _STEP_LABELS.get(self._phase)
            if label:
                error = f"{label}: {error}"
            if self._phase in ("download", "verify") and self._tried_fallback:
                error += " Tried every official download source and none worked."
            self._set_state(
                state="error",
                progress_pct=0.0,
                message="",
                error=error,
                failed_step=self._phase,
            )

    def _download_verify(self, primary_url: str, binary: Path, system: str) -> None:
        """Download + verify one candidate, else the next.

        A candidate only counts once the binary is downloaded, its
        checksum matches (when published), and ``--version`` runs: a
        source that answers HTTP 200 with an error page (proxy /
        captive portal) sails through the download and only dies at
        the magic-byte / checksum / version check — so a bad candidate
        retries the next source instead of failing the install. The
        primary gets one retry on a failure; the fallback gets one
        attempt. A failed attempt deletes its partial file before the
        next try, so a truncated download can never be mistaken for a
        complete one. Raises the last failure when every official
        source has failed.
        """
        candidates = download_urls_for_platform()
        if primary_url not in candidates:
            candidates = (primary_url, *candidates)
        last_exc: Exception | None = None
        for index, candidate in enumerate(candidates):
            if index > 0:
                self._tried_fallback = True
            attempts = 2 if index == 0 else 1
            for _attempt in range(attempts):
                try:
                    self._download(candidate, binary)
                    self._verify_checksum(candidate, binary)
                    self._verify_binary(binary, system)
                    self._set_state(download_url=candidate)
                    return
                except Exception as exc:  # noqa: BLE001  (try the next source)
                    last_exc = exc
                    if binary.exists():
                        binary.unlink(missing_ok=True)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no download source available")

    def _download(self, url: str, binary: Path) -> None:
        """Download ``url`` to ``binary``; a download that stops early
        is a download failure, not a damaged binary: when the server
        advertised a size (``Content-Length``) the file on disk must
        match it exactly.
        """
        self._phase = "download"
        self._set_state(state="downloading", message="Downloading yt-dlp…")
        lo, hi = _PHASE_PROGRESS["downloading"]

        def _progress(downloaded: int, total: int | None) -> None:
            pct = lo + (hi - lo) * (downloaded / total) if total else lo
            self._set_state(
                downloaded_bytes=downloaded,
                total_bytes=total,
                progress_pct=min(hi, pct),
            )

        result = self._downloader(url, binary, _progress)
        total, _final_url = _as_download_result(result, url)
        if not binary.is_file() or binary.stat().st_size == 0:
            raise ValueError("download produced an empty file")
        if total is not None and binary.stat().st_size != total:
            raise ValueError(
                f"download stopped early: got {binary.stat().st_size} of {total} bytes"
            )
        self._check_binary_magic(binary)

    def _check_binary_magic(self, binary: Path) -> None:
        """Fail fast when the download is not a binary at all.

        A proxy or captive portal can answer HTTP 200 with an HTML
        error page instead of the program; the size check passes (the
        page has a Content-Length) and the checksum is often
        unavailable, so without this the install would only die later
        at the version check with a confusing error. Windows binaries
        start ``MZ``, Linux ones ``\\x7fELF``, macOS ones with a Mach-O
        magic (``\\xcf\\xfa\\xed\\xfe`` / ``\\xce\\xfa\\xed\\xfe`` /
        ``\\xca\\xfe\\xba\\xbe``).
        """
        system = platform.system()
        if system == "Windows":
            expected = (b"MZ",)
        elif system == "Linux":
            expected = (b"\x7fELF",)
        else:
            expected = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
        with binary.open("rb") as fh:
            head = fh.read(4)
        if not head.startswith(expected):
            raise ValueError("download is not a binary file (the server returned a web page?)")

    def _verify_checksum(self, requested_url: str, binary: Path) -> None:
        self._phase = "verify"
        self._set_state(state="verifying", progress_pct=72.0, message="Checking the download…")
        asset = requested_url.rstrip("/").rsplit("/", 1)[-1]
        expected = _fetch_expected_sha256(asset)
        if expected is None:
            # Sums file unreachable or no line for this asset: the
            # download is still over HTTPS from the official source, so
            # proceed — but record honestly that it was NOT verified
            # instead of leaving the UI to assume it was.
            self._set_state(checksum_verified=False)
            return
        digest = _sha256_file(binary)
        if digest != expected:
            raise ValueError(f"checksum mismatch: {digest} != {expected}")
        self._set_state(checksum_verified=True)

    def _verify_binary(self, binary: Path, system: str) -> Path:
        """Run the freshly downloaded binary before it moves into place."""
        if os.name != "nt":
            # The standalone binary must be executable to run at all;
            # the installer chmods it again on install — this is the
            # pre-flight check, not the final permission. (os.name, not
            # the platform string: the exec bit matters on POSIX
            # filesystems whatever the target platform is.)
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        version = ytdlp_version(binary)
        if not version:
            raise FileNotFoundError("downloaded yt-dlp did not run")
        return binary

    def _swap_into_place(self, binary: Path, system: str) -> None:
        """Atomically replace <data_dir>/yt-dlp/yt-dlp[.exe].

        The old binary (if any) is moved aside first and restored on
        failure, so a failed swap never leaves the install dir without
        a working yt-dlp when one was there before.
        """
        target = bundled_yt_dlp_path(self.data_dir, system)
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        backup = self.data_dir / ".ytdlp-install-old"
        if backup.exists():
            backup.unlink(missing_ok=True)
        had_old = target.exists()
        try:
            if had_old:
                os.replace(target, backup)
            try:
                os.replace(binary, target)
            except OSError:
                if had_old:
                    os.replace(backup, target)
                raise
        except OSError:
            raise
        else:
            if backup.exists():
                backup.unlink(missing_ok=True)

    def _notify_installed(self, yt_dlp_path: str) -> None:
        if self._on_installed is None:
            return
        try:
            self._on_installed(yt_dlp_path)
        except Exception:  # noqa: BLE001  (callback must not fail the install)
            pass


__all__ = [
    "YTDLP_DOWNLOAD_PAGE",
    "CHECKSUMS_URL",
    "INSTALL_DIR_NAME",
    "PINNED_VERSION",
    "RELEASES_LATEST_BASE",
    "STATES",
    "DownloadResult",
    "YtDlpInstaller",
    "InstallState",
    "asset_for_platform",
    "bundled_yt_dlp_path",
    "download_url_for_platform",
    "download_urls_for_platform",
    "ensure_bundled_yt_dlp_on_path",
    "resolve_yt_dlp_path",
    "ytdlp_missing_message",
    "ytdlp_version",
]

# Keep the module importable without a display / Windows-only deps.
if __name__ == "__main__":  # pragma: no cover - exercised via scripts/install-ytdlp.py
    sys.exit("Use scripts/install-ytdlp.py to install yt-dlp from the command line.")
