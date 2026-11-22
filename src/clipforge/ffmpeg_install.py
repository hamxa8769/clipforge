"""One-click FFmpeg installer for ClipForge v0.4.1.

v0.4.0 could only *detect* a missing FFmpeg and point the user at a
download page; the user still had to download, unzip, and edit PATH by
hand. This module does the whole install inside the app:

* Download the official build for the platform, with progress.
* Verify the published SHA-256 checksum when the checksum file is
  reachable (best-effort: the download itself is always over HTTPS).
* Extract to a temp dir, verify ``ffmpeg -version`` runs, then move the
  result into place atomically — a failed install never leaves a
  half-extracted ``<data_dir>/ffmpeg`` behind.
* Remember the installed binary (``settings.json`` ``ffmpeg_path`` via
  the ``on_installed`` callback) and prepend its folder to this
  process's PATH, so clipping works immediately with no restart.
* On Windows, also add the folder to the *user* PATH (registry
  ``HKCU\\Environment``, the proper mechanism — ``setx`` only as a
  fallback), so future runs and other tools find it too.

Download sources (verified 2026-11-21 — see README "FFmpeg install"):

* Windows: ``https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip``
  — the "Windows builds from gyan.dev" link on the official
  https://ffmpeg.org/download.html page. FFmpeg itself ships source
  only; gyan.dev is the build provider ffmpeg.org links to. The URL
  redirects to the current versioned package
  (``ffmpeg-9.1-essentials_build.zip`` at verification time) and a
  matching ``.sha256`` file sits next to it.
* Linux: ``https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz``
  — the static Linux build linked from ffmpeg.org/download.html.
  No package manager, no sudo: the static binaries land in the app
  data dir, so the install works the same on every distro.
* macOS: not auto-installed yet — the installer reports this plainly
  (``brew install ffmpeg`` is the one-command route) instead of
  pretending to install.

When the primary download fails (transient network error, bad
checksum, …) the installer retries it once, then tries the fallback
sources in order — the direct gyan.dev packages URL for the current
versioned build, then the BtbN build (also linked from
ffmpeg.org/download.html). Only official, ffmpeg.org-linked sources,
never a random mirror. A candidate only counts once its binaries are
extracted and run: a source that answers HTTP 200 with an error page
(a proxy or captive portal returning HTML instead of the archive) is
caught by a magic-byte check right after the download, and an archive
that fails to unpack moves on to the next source instead of failing
the install. Every attempt is verified the same way: the downloaded
size must match ``Content-Length`` when the server sends one, and the
SHA-256 must match the published checksum when one is available —
``<url>.sha256`` for gyan.dev, the release ``checksums.sha256``
manifest for BtbN. A checksum mismatch is a hard failure, never a
silent skip; a missing checksum file is recorded honestly
(``checksum_verified: false``) and shown in the UI instead of
pretending the download was verified.

Every failure names the step that failed (``failed_step``: download /
verify / extract / install) in plain language, and the Windows user
PATH step reports its own warning (``path_warning``) without failing
an install that already works inside ClipForge.

The installer is deliberately dependency-light (stdlib + httpx, which
the API already uses) and side-effect free to import: creating an
``FfmpegInstaller`` only reads/writes state under ``data_dir`` when an
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
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# ---------------------------------------------------------------- sources
#: Windows build, linked from the official https://ffmpeg.org/download.html
#: page ("Windows builds from gyan.dev"). Redirects to the current
#: versioned essentials package; a ``.sha256`` file sits at the same URL
#: with a ``.sha256`` suffix.
WINDOWS_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
#: Static Linux build, linked from https://ffmpeg.org/download.html.
LINUX_DOWNLOAD_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
#: Fallback sources, tried in order after the primary fails (see module
#: docstring). Only official, ffmpeg.org-linked sources. The gyan.dev
#: packages URL is version-pinned (9.1 at verification time): when
#: gyan.dev moves to a newer build it 404s and the installer moves on
#: to BtbN, whose ``/latest`` URL never rots. BtbN is the "Windows /
#: Linux builds from BtbN" link on ffmpeg.org/download.html; its
#: archives have the same ``<top>/bin/{ffmpeg,ffprobe}`` layout, so the
#: extractor needs no special case.
WINDOWS_FALLBACK_URLS = (
    "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.1-essentials_build.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
)
LINUX_FALLBACK_URLS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
)
#: The page both URLs are linked from — the human-readable fallback.
FFMPEG_DOWNLOAD_PAGE = "https://ffmpeg.org/download.html"

INSTALL_DIR_NAME = "ffmpeg"
STATE_FILE_NAME = "ffmpeg-install-state.json"

# Install states, in order. ``unsupported`` is terminal (macOS today);
# ``done`` / ``error`` are terminal for a run; ``idle`` means never run.
STATES = (
    "idle",
    "downloading",
    "verifying",
    "extracting",
    "installing",
    "done",
    "error",
    "unsupported",
)
# Progress ranges per phase, so the UI bar moves smoothly through a run.
_PHASE_PROGRESS = {
    "downloading": (0, 70),
    "verifying": (70, 75),
    "extracting": (75, 90),
    "installing": (90, 100),
}

#: Plain-language reasons for the failures users actually hit.
_NO_INTERNET = "Could not reach the download server. Check your internet connection and try again."
_CHECKSUM_MISMATCH = (
    "The downloaded file did not match its published checksum, so it was discarded — "
    "it may have been damaged in transit. Try again."
)
_DAMAGED_ARCHIVE = (
    "The downloaded file was damaged and could not be opened. Try again — "
    "if it keeps happening, download FFmpeg manually from "
    f"{FFMPEG_DOWNLOAD_PAGE}."
)
_TIMEOUT = "The download timed out. Check your internet connection and try again."
_NO_BINARY = (
    "The download finished but did not contain a working FFmpeg program. "
    "Try again, or install FFmpeg manually from "
    f"{FFMPEG_DOWNLOAD_PAGE}."
)
_INCOMPLETE_DOWNLOAD = (
    "The download stopped before it finished, so it was discarded. "
    "Check your internet connection and try again."
)
_NOT_AN_ARCHIVE = (
    "The download server returned a web page instead of the FFmpeg archive "
    "(this can happen behind a proxy or a captive portal). Check your "
    "internet connection and try again."
)

#: Human step label shown in front of an install error, so the UI can
#: say *where* it failed (download / verify / extract / install).
_STEP_LABELS = {
    "download": "Download failed",
    "verify": "Verification failed",
    "extract": "Extraction failed",
    "install": "Install failed",
}


def download_url_for_platform(system: str | None = None) -> str | None:
    """Official download URL for this OS, or ``None`` when unsupported."""
    system = system or platform.system()
    if system == "Windows":
        return WINDOWS_DOWNLOAD_URL
    if system == "Linux":
        return LINUX_DOWNLOAD_URL
    return None


def download_urls_for_platform(system: str | None = None) -> tuple[str, ...]:
    """Primary + fallback download URLs for this OS, in try order."""
    system = system or platform.system()
    if system == "Windows":
        return (WINDOWS_DOWNLOAD_URL, *WINDOWS_FALLBACK_URLS)
    if system == "Linux":
        return (LINUX_DOWNLOAD_URL, *LINUX_FALLBACK_URLS)
    return ()


def _exe_name(system: str | None = None) -> str:
    return "ffmpeg.exe" if (system or platform.system()) == "Windows" else "ffmpeg"


def _probe_name(system: str | None = None) -> str:
    return "ffprobe.exe" if (system or platform.system()) == "Windows" else "ffprobe"


def bundled_ffmpeg_path(data_dir: str | Path, system: str | None = None) -> Path:
    """Where an installer-managed FFmpeg binary lives (may not exist yet)."""
    return Path(data_dir) / INSTALL_DIR_NAME / "bin" / _exe_name(system)


def bundled_ffprobe_path(data_dir: str | Path, system: str | None = None) -> Path:
    """Where an installer-managed ffprobe binary lives (may not exist yet)."""
    return Path(data_dir) / INSTALL_DIR_NAME / "bin" / _probe_name(system)


def _is_usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_ffmpeg_path(
    data_dir: str | Path, stored_path: str | None = None, system: str | None = None
) -> str | None:
    """Best FFmpeg binary to run, in priority order.

    1. The path saved in settings after a one-click install.
    2. The installer-managed ``<data_dir>/ffmpeg/bin/`` binary.
    3. Whatever is on PATH (a manual install).
    """
    if stored_path:
        candidate = Path(stored_path)
        if _is_usable(candidate):
            return str(candidate)
    bundled = bundled_ffmpeg_path(data_dir, system)
    if _is_usable(bundled):
        return str(bundled)
    # A bundled binary on a non-executable filesystem (or a zip that lost
    # its exec bit) is still usable — it just needs invoking by path.
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("ffmpeg")
    return found


def ffmpeg_version(binary: str | Path) -> str:
    """First line of ``ffmpeg -version`` (``""`` when it does not run)."""
    try:
        proc = subprocess.run([str(binary), "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").splitlines()[0].strip() if proc.stdout else ""


def ensure_bundled_on_path(data_dir: str | Path, system: str | None = None) -> bool:
    """Prepend the bundled ``bin`` dir to this process's PATH.

    This is what makes clipping work immediately after an install with
    no restart and no logoff: every ``ffmpeg``/``ffprobe`` call in this
    process (render, ingest, transcribe, ``shutil.which``) resolves to
    the bundled binary. Returns True when the dir was added (or was
    already there).
    """
    bin_dir = str(bundled_ffmpeg_path(data_dir, system).parent)
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if any(os.path.normcase(p) == os.path.normcase(bin_dir) for p in parts):
        return True
    if not bundled_ffmpeg_path(data_dir, system).is_file():
        return False
    os.environ["PATH"] = bin_dir + (os.pathsep + path if path else "")
    return True


# ------------------------------------------------------- Windows user PATH
def add_to_user_path_windows(bin_dir: str | Path) -> dict:
    """Add ``bin_dir`` to the current user's PATH (Windows only).

    Uses the registry (``HKCU\\Environment\\Path``) — the proper
    mechanism; ``setx`` is only a fallback because it silently
    truncates values over 1024 characters. Broadcasts
    ``WM_SETTINGCHANGE`` so new processes (Explorer, new terminals)
    pick the change up without a logoff; already-running processes
    (including this one) keep their old PATH, which is why the app
    also prepends the dir to its own process PATH.
    """
    bin_dir = str(bin_dir)
    if platform.system() != "Windows":
        return {"ok": False, "already_present": False, "error": "PATH update is Windows-only."}
    try:
        import winreg  # noqa: PLC0415  (Windows-only stdlib module)
    except ImportError:
        return {
            "ok": False,
            "already_present": False,
            "error": "Windows registry access is unavailable.",
        }
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        )
    except OSError as exc:
        # Could not even open the key (locked down machine, group
        # policy...): fall back to setx, which edits the same user
        # PATH — without a readable current value, setx is refused
        # (see _add_to_user_path_setx).
        return _add_to_user_path_setx(bin_dir, None, str(exc))
    try:
        try:
            current, reg_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, reg_type = "", winreg.REG_EXPAND_SZ
        except OSError as exc:
            # Value unreadable: no current PATH to hand to setx, so the
            # fallback is refused rather than risking the PATH.
            return _add_to_user_path_setx(bin_dir, None, str(exc))
        parts = [p for p in str(current).split(";") if p]
        if any(os.path.normcase(p) == os.path.normcase(bin_dir) for p in parts):
            return {"ok": True, "already_present": True, "error": ""}
        new_value = ";".join([*parts, bin_dir])
        try:
            winreg.SetValueEx(key, "Path", 0, reg_type, new_value)
        except OSError as exc:
            # Write failed but the current value was read: hand the
            # full value to setx so it stores the real PATH.
            return _add_to_user_path_setx(bin_dir, str(current), str(exc))
    finally:
        winreg.CloseKey(key)
    _broadcast_environment_change()
    return {"ok": True, "already_present": False, "error": ""}


def _add_to_user_path_setx(bin_dir: str, current_path: str | None, registry_error: str) -> dict:
    """``setx`` fallback for the user PATH. Last resort only.

    ``current_path`` is the user PATH as read from the registry (None
    when even the read failed). The full value — current entries plus
    ``bin_dir`` — is passed to setx, because setx stores its argument
    verbatim: the old ``"%PATH%;<dir>"`` form only worked when a shell
    expanded ``%PATH%`` first, and this call has no shell, so it would
    have stored a literal ``%PATH%`` and wiped the user's existing
    entries. With no readable current value setx is refused outright
    rather than risk that. setx also silently truncates values over
    1024 characters, so a PATH that long is refused too.
    """
    if current_path is None:
        return {
            "ok": False,
            "already_present": False,
            "error": (
                "Could not add FFmpeg to your Windows PATH "
                f"(registry: {registry_error}). FFmpeg still works "
                "inside ClipForge — new terminals will need the folder added by hand."
            ),
        }
    new_value = ";".join([p for p in current_path.split(";") if p] + [bin_dir])
    if len(new_value) > 1024:
        return {
            "ok": False,
            "already_present": False,
            "error": (
                "Could not add FFmpeg to your Windows PATH "
                f"(registry: {registry_error}; your PATH is too long for the "
                "setx fallback, which truncates values over 1024 characters). "
                "FFmpeg still works inside ClipForge — new terminals will need "
                "the folder added by hand."
            ),
        }
    try:
        proc = subprocess.run(
            ["setx", "Path", new_value],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "already_present": False,
            "error": (
                "Could not add FFmpeg to your Windows PATH "
                f"(registry: {registry_error}; setx: {exc}). FFmpeg still works "
                "inside ClipForge — new terminals will need the folder added by hand."
            ),
        }
    if proc.returncode != 0:
        return {
            "ok": False,
            "already_present": False,
            "error": (
                "Could not add FFmpeg to your Windows PATH "
                f"(registry: {registry_error}; setx failed). FFmpeg still works "
                "inside ClipForge — new terminals will need the folder added by hand."
            ),
        }
    _broadcast_environment_change()
    return {"ok": True, "already_present": False, "error": ""}


def _broadcast_environment_change() -> None:
    """Tell running Windows programs the environment changed."""
    try:
        import ctypes  # noqa: PLC0415

        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x0002, 5000, None)
    except Exception:  # noqa: BLE001  (best-effort notification only)
        pass


# ---------------------------------------------------------------- download
@dataclass
class DownloadResult:
    """What a download produced: the advertised size and the URL the
    download actually came from (after redirects).

    The final URL matters for verification: the published checksum
    describes the file the redirect points at, not the short URL that
    redirected to it (gyan.dev's ``.sha256`` itself redirects, but a
    fallback host's may not). ``total_bytes`` is the response
    ``Content-Length`` when the server sent one, else None.
    """

    total_bytes: int | None = None
    final_url: str | None = None


#: ``downloader(url, dest, progress_cb) -> DownloadResult``.
#: ``progress_cb(downloaded, total_or_None)`` is called per chunk.
#: Older downloaders may return a plain ``int`` (total bytes) or None;
#: the installer treats those as "same URL, that total".
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

    A malformed proxy entry in the environment (e.g. a bracketed IPv6
    address in ``NO_PROXY`` that httpx cannot parse) raises
    ``httpx.InvalidURL`` before any byte moves — retry once with the
    proxy taken explicitly from the environment (``trust_env=False``)
    instead of failing the install for a proxy the download never
    needed. With no proxy configured that retry is a direct connection.
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
    """SHA-256 of a file, streamed (a build archive is ~100 MB)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_sha256_token(text: str) -> str | None:
    """First token of a checksum file, when it is a bare SHA-256 hex digest."""
    try:
        token = text.split()[0].strip().lower()
    except (ValueError, IndexError):
        return None
    return token if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else None


def _fetch_expected_sha256(requested_url: str, final_url: str | None = None) -> str | None:
    """Published checksum for a build, or None when none is published.

    Tries, in order: ``<requested>.sha256`` (gyan.dev: the URL itself
    303-redirects to the versioned checksum file), ``<final>.sha256``
    (the URL the download actually came from — the checksum describes
    that file, not the short URL that redirected to it), and a
    ``checksums.sha256`` manifest next to the requested URL (BtbN
    publishes one manifest per release instead of per-file checksums).
    A missing checksum file is not a failed download: the checksum is
    best-effort, and the caller records that it could not verify.
    """
    final_url = final_url or requested_url
    for candidate in (requested_url + ".sha256", final_url + ".sha256"):
        text = _fetch_url_text(candidate)
        if text is not None:
            token = _parse_sha256_token(text)
            if token is not None:
                return token
    manifest = _fetch_url_text(_manifest_url(requested_url))
    if manifest is None:
        return None
    wanted = requested_url.rstrip("/").rsplit("/", 1)[-1]
    for line in manifest.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == wanted:
            token = parts[0].strip().lower()
            if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
                return token
    return None


def _manifest_url(url: str) -> str:
    """``checksums.sha256`` next to ``url`` (same directory)."""
    return url.rstrip("/").rsplit("/", 1)[0] + "/checksums.sha256"


def _fetch_url_text(url: str) -> str | None:
    """GET ``url`` as text, or None when it is missing/unreachable."""
    try:
        resp = _get_text(url, trust_env=True, proxy=None)
    except httpx.InvalidURL:
        try:
            resp = _get_text(url, trust_env=False, proxy=_proxy_url_from_env())
        except (httpx.HTTPError, ValueError, IndexError):
            return None
    except (httpx.HTTPError, ValueError, IndexError):
        return None
    # A missing checksum file (404 — johnvansickle publishes none at
    # this URL) is not a failed download: the checksum is best-effort.
    try:
        if resp.status_code != 200:
            return None
        return resp.text
    except (ValueError, IndexError):
        return None


def _get_text(url: str, *, trust_env: bool, proxy: str | None) -> httpx.Response:
    return httpx.get(url, follow_redirects=True, timeout=30.0, trust_env=trust_env, proxy=proxy)


def _plain_error(exc: BaseException) -> str:
    """Map a failure to the plain-language message the UI shows."""
    if isinstance(exc, httpx.ConnectError):
        return _NO_INTERNET
    if isinstance(exc, httpx.InvalidURL):
        # A bad proxy entry in the environment, or a bad download URL:
        # either way the server could not be reached.
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
            "Not enough free disk space to install FFmpeg (about 100 MB on Windows, "
            "40 MB on Linux). Free some space and try again."
        )
    if isinstance(exc, zipfile.BadZipFile):
        return _DAMAGED_ARCHIVE
    if isinstance(exc, ValueError) and "checksum mismatch" in str(exc):
        return _CHECKSUM_MISMATCH
    if isinstance(exc, ValueError) and "stopped early" in str(exc):
        return _INCOMPLETE_DOWNLOAD
    if isinstance(exc, ValueError) and "not an archive" in str(exc):
        return _NOT_AN_ARCHIVE
    if isinstance(exc, (zipfile.BadZipFile, tarfile.TarError)):
        return _DAMAGED_ARCHIVE
    if isinstance(exc, (EOFError, OSError)) and "damaged" in str(exc).lower():
        return _DAMAGED_ARCHIVE
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
    #: extract / install. The UI shows it so the user knows *where* the
    #: install stopped instead of guessing from a bare error.
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


class FfmpegInstaller:
    """One-click FFmpeg install into ``<data_dir>/ffmpeg/``.

    ``on_installed`` is called with the installed ``ffmpeg`` path on
    success (the API wires it to saving ``ffmpeg_path`` in settings);
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
        self.bin_dir = self.install_dir / "bin"
        self.state_path = self.data_dir / STATE_FILE_NAME
        self._downloader = downloader or _download_with_httpx
        self._on_installed = on_installed
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_state_write = 0.0
        # Step the current run is in (download / verify / extract /
        # install); the error handler reports it as ``failed_step``.
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
            if raw["state"] in ("downloading", "verifying", "extracting", "installing"):
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
                "FFmpeg install failed",
                f"FFmpeg could not be installed{step}: {failed_error or 'unknown error'} "
                "Press 'Install FFmpeg' in Settings to try again.",
                action={"label": "Open Settings", "url": "/settings"},
                dedupe_key="install-failed:ffmpeg",
            )
        # Progress callbacks fire per 256 KiB chunk; writing the state
        # file every time would hammer the disk on a ~100 MB download,
        # so throttle file writes to ~2/s (state changes always write).
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
        """Current install state, enriched with live FFmpeg detection."""
        with self._lock:
            snapshot = self._state.to_dict()
        found_path = resolve_ffmpeg_path(self.data_dir)
        snapshot["found"] = bool(found_path)
        snapshot["path"] = found_path or snapshot["path"]
        if found_path:
            snapshot["version"] = ffmpeg_version(found_path) or snapshot["version"]
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
        instead of starting a duplicate download. When FFmpeg is
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
                    "One-click install is not available on macOS yet. "
                    "Install FFmpeg with one command instead: brew install ffmpeg "
                    f"(or download it from {FFMPEG_DOWNLOAD_PAGE}), then restart ClipForge."
                ),
                failed_step="",
                path_warning="",
            )
            return self.status()
        existing = resolve_ffmpeg_path(self.data_dir)
        if existing and not force:
            version = ffmpeg_version(existing)
            self._set_state(
                state="done",
                progress_pct=100.0,
                message="FFmpeg is already installed.",
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
            message="Downloading FFmpeg…",
            error="",
            path_added=None,
            checksum_verified=None,
            failed_step="",
            path_warning="",
        )
        thread = threading.Thread(
            target=self._run_install, args=(url,), daemon=True, name="ffmpeg-install"
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
            with tempfile.TemporaryDirectory(prefix=".ffmpeg-install-", dir=self.data_dir) as tmp:
                tmpdir = Path(tmp)
                archive = tmpdir / ("ffmpeg.zip" if system == "Windows" else "ffmpeg.tar.xz")
                new_install = tmpdir / "new-install"
                self._download_extract_verify(url, archive, new_install, system)
                self._phase = "install"
                self._set_state(state="installing", progress_pct=90.0, message="Installing FFmpeg…")
                self._swap_into_place(new_install)
                installed = bundled_ffmpeg_path(self.data_dir, system)
                version = ffmpeg_version(installed)
                if not version:
                    raise FileNotFoundError("installed FFmpeg did not run")
                ensure_bundled_on_path(self.data_dir, system)
                path_added: bool | None = None
                path_warning = ""
                if system == "Windows":
                    result = add_to_user_path_windows(self.bin_dir)
                    path_added = bool(result["ok"])
                    if not result["ok"]:
                        # Non-fatal: FFmpeg already works inside
                        # ClipForge; only new terminals would miss it.
                        # Surface it instead of silently reporting a
                        # clean install.
                        path_warning = result["error"]
                self._notify_installed(str(installed))
                message = "FFmpeg is installed and ready — clipping will work now."
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

    def _download_extract_verify(
        self, primary_url: str, archive: Path, new_install: Path, system: str
    ) -> None:
        """Download + verify + extract one candidate, else the next.

        A candidate only counts once its binaries are extracted and
        run: a source that answers HTTP 200 with an error page (proxy /
        captive portal) sails through the download and the checksum
        (none published for that build) and only dies at extraction —
        so an extraction failure retries the next source instead of
        failing the install. The primary gets one retry on a failure;
        each fallback gets one attempt. A failed attempt deletes its
        partial files before the next try, so a truncated download can
        never be mistaken for a complete one. Raises the last failure
        when every official source has failed.
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
                    final_url = self._download(candidate, archive)
                    self._verify_checksum(candidate, final_url, archive)
                    self._extract(archive, new_install, system)
                    self._verify_binaries(new_install, system)
                    self._set_state(download_url=candidate)
                    return
                except Exception as exc:  # noqa: BLE001  (try the next source)
                    last_exc = exc
                    if archive.exists():
                        archive.unlink(missing_ok=True)
                    shutil.rmtree(new_install, ignore_errors=True)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no download source available")

    def _download(self, url: str, archive: Path) -> str | None:
        """Download ``url`` to ``archive``; return the final URL after redirects.

        A download that stops early is a download failure, not a
        damaged archive: when the server advertised a size
        (``Content-Length``) the file on disk must match it exactly.
        """
        self._phase = "download"
        self._set_state(state="downloading", message="Downloading FFmpeg…")
        lo, hi = _PHASE_PROGRESS["downloading"]

        def _progress(downloaded: int, total: int | None) -> None:
            pct = lo + (hi - lo) * (downloaded / total) if total else lo
            self._set_state(
                downloaded_bytes=downloaded,
                total_bytes=total,
                progress_pct=min(hi, pct),
            )

        result = self._downloader(url, archive, _progress)
        total, final_url = _as_download_result(result, url)
        if not archive.is_file() or archive.stat().st_size == 0:
            raise ValueError("download produced an empty file")
        if total is not None and archive.stat().st_size != total:
            raise ValueError(
                f"download stopped early: got {archive.stat().st_size} of {total} bytes"
            )
        self._check_archive_magic(archive)
        return final_url

    def _check_archive_magic(self, archive: Path) -> None:
        """Fail fast when the download is not an archive at all.

        A proxy or captive portal can answer HTTP 200 with an HTML
        error page instead of the build; the size check passes (the
        page has a Content-Length) and the checksum is often
        unavailable, so without this the install would only die later
        at extraction with a confusing unpack error. Windows builds
        are zips (``PK``), Linux builds are xz tarballs (``\\xfd7zXZ``).
        """
        system = platform.system()
        expected = b"PK" if system == "Windows" else b"\xfd7zXZ\x00"
        with archive.open("rb") as fh:
            head = fh.read(len(expected))
        if not head.startswith(expected):
            raise ValueError("download is not an archive file (the server returned a web page?)")

    def _verify_checksum(self, requested_url: str, final_url: str | None, archive: Path) -> None:
        self._phase = "verify"
        self._set_state(state="verifying", progress_pct=72.0, message="Checking the download…")
        expected = _fetch_expected_sha256(requested_url, final_url)
        if expected is None:
            # Checksum file unreachable or not published for this build
            # (johnvansickle publishes none): the download is still
            # over HTTPS from the official source, so proceed — but
            # record honestly that it was NOT verified instead of
            # leaving the UI to assume it was.
            self._set_state(checksum_verified=False)
            return
        digest = _sha256_file(archive)
        if digest != expected:
            raise ValueError(f"checksum mismatch: {digest} != {expected}")
        self._set_state(checksum_verified=True)

    def _extract(self, archive: Path, new_install: Path, system: str) -> None:
        self._phase = "extract"
        self._set_state(state="extracting", progress_pct=78.0, message="Unpacking FFmpeg…")
        bin_dir = new_install / "bin"
        bin_dir.mkdir(parents=True)
        exe = _exe_name(system)
        probe = _probe_name(system)
        if system == "Windows":
            try:
                with zipfile.ZipFile(archive) as zf:
                    members = [
                        m
                        for m in zf.infolist()
                        if m.filename in ("bin/" + exe, "bin/" + probe)
                        or m.filename.endswith(("/bin/" + exe, "/bin/" + probe))
                    ]
                    if not members:
                        raise FileNotFoundError("no ffmpeg binary in archive")
                    for member in members:
                        zf.extract(member, new_install)
            except zipfile.BadZipFile:
                raise
            # Flatten <top>/bin/*.exe -> new-install/bin/*.exe. Eagerly
            # list first: rglob is lazy and would also yield the files
            # this loop just created under bin/ (SameFileError).
            for found in list(new_install.rglob(exe)):
                if found.parent != bin_dir:
                    dest = bin_dir / exe
                    shutil.copy2(found, dest)
                    # A zip that stored 0644 modes must still yield a
                    # runnable exe (zipfile only preserves the stored mode).
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            for found in list(new_install.rglob(probe)):
                if found.parent != bin_dir:
                    dest = bin_dir / probe
                    shutil.copy2(found, dest)
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            with tarfile.open(archive, "r:*") as tf:
                members = [
                    m
                    for m in tf.getmembers()
                    if m.name.endswith(("/" + exe, "/" + probe)) and m.isfile()
                ]
                if not members:
                    raise FileNotFoundError("no ffmpeg binary in archive")
                tf.extractall(new_install, members=members, filter="data")
            for found in list(new_install.rglob(exe)):
                if found.parent == bin_dir:
                    continue
                dest = bin_dir / exe
                shutil.copy2(found, dest)
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            for found in list(new_install.rglob(probe)):
                if found.parent == bin_dir:
                    continue
                dest = bin_dir / probe
                shutil.copy2(found, dest)
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # Drop the extracted tree; only bin/ moves into place.
        for child in new_install.iterdir():
            if child.name != "bin":
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(
                    missing_ok=True
                )
        # Guarantee runnable binaries whatever the archive layout was:
        # zipfile does not restore stored modes, so a member extracted
        # straight into bin/ (flat layout, no versioned top folder)
        # would otherwise stay non-executable and fail verification.
        for name in (exe, probe):
            target = bin_dir / name
            if target.is_file():
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._set_state(progress_pct=88.0)

    def _verify_binaries(self, new_install: Path, system: str) -> Path:
        """Run the freshly extracted binary before it moves into place."""
        ffmpeg_path = new_install / "bin" / _exe_name(system)
        if not ffmpeg_path.is_file():
            raise FileNotFoundError("no ffmpeg binary in archive")
        version = ffmpeg_version(ffmpeg_path)
        if not version:
            raise FileNotFoundError("extracted FFmpeg did not run")
        return ffmpeg_path

    def _swap_into_place(self, new_install: Path) -> None:
        """Atomically replace <data_dir>/ffmpeg; restore the old one on failure."""
        target = self.install_dir
        trash = self.data_dir / ".ffmpeg-install-old"
        shutil.rmtree(trash, ignore_errors=True)
        had_old = target.exists()
        try:
            if had_old:
                os.replace(target, trash)
            try:
                os.replace(new_install, target)
            except OSError:
                if had_old:
                    os.replace(trash, target)
                raise
        except OSError:
            raise
        else:
            shutil.rmtree(trash, ignore_errors=True)

    def _notify_installed(self, ffmpeg_path: str) -> None:
        if self._on_installed is None:
            return
        try:
            self._on_installed(ffmpeg_path)
        except Exception:  # noqa: BLE001  (callback must not fail the install)
            pass


__all__ = [
    "FFMPEG_DOWNLOAD_PAGE",
    "INSTALL_DIR_NAME",
    "LINUX_DOWNLOAD_URL",
    "LINUX_FALLBACK_URLS",
    "STATES",
    "WINDOWS_DOWNLOAD_URL",
    "WINDOWS_FALLBACK_URLS",
    "DownloadResult",
    "FfmpegInstaller",
    "InstallState",
    "add_to_user_path_windows",
    "bundled_ffmpeg_path",
    "bundled_ffprobe_path",
    "download_url_for_platform",
    "download_urls_for_platform",
    "ensure_bundled_on_path",
    "ffmpeg_version",
    "resolve_ffmpeg_path",
]

# Keep the module importable without a display / Windows-only deps.
if __name__ == "__main__":  # pragma: no cover - exercised via scripts/install-ffmpeg.py
    sys.exit("Use scripts/install-ffmpeg.py to install FFmpeg from the command line.")
