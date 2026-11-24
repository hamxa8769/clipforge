"""Local-file and own-content YouTube ingest for ClipForge.

Rules enforced here (see research report §7 — approval-first, own-content
only):

* Local files must exist, be mp4/mov (by extension *and* ffprobe format),
  and pass an ffprobe probe for duration / size / codec.
* A YouTube URL is downloaded **only** when the caller explicitly declares
  ``own_content=True``. The declaration is recorded on the returned
  :class:`SourceInfo`. Downloading another creator's content is refused
  by design — there is no code path that downloads without the flag.
* yt-dlp is an external dependency: the one-click installer puts it in
  ``<data_dir>/yt-dlp/`` and ingest uses that bundled binary first
  (then PATH). If it is not installed anywhere, a clear error is
  raised that points at the in-app Install button instead of failing
  obscurely.
* Every source file is SHA-256 hashed so transcripts can be cached by
  source hash and never re-transcribed (report §2, stage 2).
* Downloads are robust on Windows (v0.9.2): each run downloads into
  its own fresh ``<uploads>/.dl-<job>-<rand>/`` directory, so two runs
  (a retry, a re-queued job after a restart, an orphaned yt-dlp from
  a dead server process) can never share — and lock — the same
  fragment files; stale ``.part`` leftovers from dead runs are swept
  before a new download instead of being resumed into blindly; and
  yt-dlp is given real file-access retries (its default is 3 renames
  ~10 ms apart, which a moment of antivirus scanning defeats) with a
  1 s sleep between them. A lock that still persists is reported in
  plain language naming antivirus real-time scanning and the fix
  (exclude the ClipForge data folder), not as a raw traceback.
* Fail-fast ingest (v0.10.3): before any download, the link is
  checked — YouTube oEmbed (404 in <1 s for a removed / private
  video) then a metadata-only ``yt-dlp --simulate --dump-json`` probe
  — so a dead link fails in seconds with a plain "this video is no
  longer available, try a different link" instead of burning an hour
  (the user's v0.10.2 job e01b83e18de34ada ran 3601 s on a removed
  Comedy link and died with the generic "took too long"). The download
  itself is bounded by ``--socket-timeout 20`` (a stalled connection
  can no longer hang forever — yt-dlp's default socket timeout is
  unset) and a stall watchdog that kills yt-dlp when it stops
  producing output; the 3600 s cap survives only as a last-resort
  guard. Every yt-dlp failure is classified (removed / private /
  age-gated / geo-blocked / bot-check / live stream / unreachable)
  and reported in plain language with the right next step.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from clipforge.job_control import JobControl, _terminate_process, popen_kwargs
from clipforge.workflow import ffmpeg_missing_message
from clipforge.ytdlp_install import resolve_yt_dlp_path, ytdlp_missing_message

ALLOWED_EXTENSIONS = {".mp4", ".mov"}
ALLOWED_FORMATS = {"mov,mp4,m4a,3gp,3g2,mj2", "mp4", "mov"}
MAX_SOURCE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB sanity cap


class IngestError(ValueError):
    """Raised when a source cannot be ingested (bad file, bad URL, ...).

    ``code`` (v0.10.3) is a short machine-readable reason —
    ``youtube_unavailable``, ``youtube_bot_check``, ``youtube_live``,
    ``youtube_unreachable``, ``download_stalled``, ``download_timeout``,
    ``youtube_age_gated``, ``youtube_geo_blocked``,
    ``youtube_playlist``, ``download_file_lock`` — so
    :func:`clipforge.errors.plain_error` can give the failure a
    specific title and next step instead of the generic ingest one.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class OwnContentRequiredError(IngestError):
    """Raised when a YouTube ingest is attempted without own_content=True."""


@dataclass
class SourceInfo:
    """Validated description of an ingested video source."""

    path: Path
    sha256: str
    duration_s: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    size_bytes: int
    format_name: str
    source_kind: str = "local"  # "local" | "youtube"
    source_url: str | None = None
    own_content: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 hex digest of a file, streamed so large files stay cheap."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe_info(path: Path) -> dict:
    """Run ffprobe and return its parsed JSON (raises IngestError on failure)."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=width,height,codec_name,codec_type",
        "-show_entries",
        "format=duration,format_name,size",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise IngestError(ffmpeg_missing_message("ffprobe")) from None
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"ffprobe timed out probing {path}") from exc
    if proc.returncode != 0:
        raise IngestError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise IngestError(f"ffprobe returned unparsable output for {path}") from exc


def _local_path(path: str | Path) -> Path:
    """Normalise a local source reference to a filesystem path.

    Plain paths pass through untouched. A ``file://`` URL (what API
    clients and copy-pasted paths often produce — ``POST /jobs`` accepts
    a ``source_url`` for local jobs) is unwrapped to its path, percent
    decoded, so ``Path`` never sees the literal ``file:/...`` string:
    before this, such a job failed ingest with the confusing
    ``source file does not exist: file:/...``. ``file://host/...`` with
    a non-localhost host is left alone so it still fails loudly instead
    of silently resolving to a wrong local path.
    """
    text = str(path)
    parsed = urlparse(text)
    if parsed.scheme.lower() != "file":
        return Path(text)
    if parsed.netloc not in ("", "localhost"):
        return Path(text)
    decoded = unquote(parsed.path)
    # Windows drive URL: file:///C:/clip.mp4 -> C:/clip.mp4
    if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":" and decoded[1].isalpha():
        decoded = decoded[1:]
    return Path(decoded)


def validate_local_file(path: str | Path) -> SourceInfo:
    """Validate a local mp4/mov file and return its :class:`SourceInfo`."""
    p = _local_path(path)
    if not p.exists():
        raise IngestError(f"source file does not exist: {p}")
    if not p.is_file():
        raise IngestError(f"source path is not a file: {p}")
    if p.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise IngestError(f"unsupported extension {p.suffix!r}: only .mp4 / .mov are accepted")
    size = p.stat().st_size
    if size == 0:
        raise IngestError(f"source file is empty: {p}")
    if size > MAX_SOURCE_BYTES:
        raise IngestError(f"source file exceeds {MAX_SOURCE_BYTES} bytes: {p}")

    info = ffprobe_info(p)
    fmt = info.get("format", {})
    try:
        duration = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"ffprobe reported no usable duration for {p}") from exc
    if duration <= 0:
        raise IngestError(f"ffprobe reported non-positive duration for {p}")
    format_name = str(fmt.get("format_name", ""))
    if (
        format_name
        and format_name not in ALLOWED_FORMATS
        and "mp4" not in format_name
        and "mov" not in format_name
    ):
        raise IngestError(f"unsupported container {format_name!r} for {p}")

    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise IngestError(f"no video stream found in {p}")
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    return SourceInfo(
        path=p.resolve(),
        sha256=sha256_file(p),
        duration_s=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        video_codec=str(video.get("codec_name", "unknown")),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        size_bytes=size,
        format_name=format_name,
        source_kind="local",
    )


def ingest_local(path: str | Path) -> SourceInfo:
    """Ingest a local file (alias of :func:`validate_local_file`)."""
    return validate_local_file(path)


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in {"www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com"} or host.endswith(
        ".youtube.com"
    )


# ---------------------------------------------------------------------------
# Download robustness (Windows file-locking, v0.9.2)
#
# The user's v0.9.1 failure was yt-dlp dying on a DASH fragment rename:
# ``[Error 32] ... .part-Frag3.part -> .part-Frag3`` then "Giving up
# after 3 retries". Reproduced locally with real yt-dlp against a local
# DASH manifest (see tests/unit/test_ingest_download_lock.py): the
# rename is yt-dlp's own ``try_rename`` (``wrap_file_access``), whose
# default is ``file_access_retries=3`` with ~10 ms between attempts —
# any lock lasting longer than ~30 ms (e.g. Windows Defender /
# antivirus real-time scanning opening the just-written fragment)
# kills the download. ClipForge previously passed no retry options at
# all, downloaded every job into the same shared ``uploads/`` dir with
# the same ``%(id)s.%(ext)s`` template (so a retry, a re-queued job
# after a restart, or an orphaned yt-dlp from a dead server process
# resumed into — and could lock — the same fragment files), and then
# picked the finished file as "newest .mp4 in the shared dir".
#
# The fixes below are deliberately boring:
#
# * one fresh download dir per run (``.dl-<job>-<rand>/`` under
#   uploads) — no two runs ever share a fragment file;
# * stale ``.part`` leftovers for the same video are swept before a
#   new download (old ones only — a file an orphan is still writing
#   has a fresh mtime and is left alone);
# * yt-dlp gets explicit retries: 10 file-access retries with a 1 s
#   sleep, so a transient antivirus lock is ridden out instead of
#   failing the job (verified locally: a 2 s lock now succeeds);
# * ``--no-part`` is deliberately NOT used: it would throw away
#   resume inside a run. The trade-off is that a *job retry* starts a
#   fresh download in a fresh dir instead of resuming the previous
#   run's fragments — a few seconds of re-download for a clip source
#   is cheaper than another locked-fragment failure;
# * the finished file is picked from the job's own dir only (never a
#   ``.part`` file — ``*.mp4`` cannot match ``*.mp4.part`` — and never
#   another job's file), moved into ``uploads/`` with a retrying
#   rename, and only then ffprobe-validated. Nothing in ClipForge
#   opens or stats a ``.part`` file while yt-dlp is writing it.
# ---------------------------------------------------------------------------

# yt-dlp retry options passed on every download (explicit, so a user
# yt-dlp config file cannot silently shrink them back to the defaults
# that lost the v0.9.1 job).
YTDLP_RETRIES = 10
YTDLP_FRAGMENT_RETRIES = 10
YTDLP_FILE_ACCESS_RETRIES = 10
YTDLP_FILE_ACCESS_RETRY_SLEEP = "file_access:1"  # 1 s between rename retries

# A ``.part`` leftover older than this is a dead run's, not a live
# orphan's: an orphaned yt-dlp still writing touches its files
# constantly, so its mtime stays fresh. Only older files are swept.
STALE_PART_MAX_AGE_S = 3600

# Attempts / backoff for moving the finished file into uploads/.
# A just-finished file can still be held open briefly by antivirus
# scanning on Windows.
_MOVE_ATTEMPTS = 5
_MOVE_DELAY_S = 0.5


def _extract_youtube_id(url: str) -> str | None:
    """The video id of a YouTube watch / Shorts / youtu.be URL, or None.

    Only used to name the download dir's stale-cleanup sweep and to
    prefer the exact ``<id>.mp4`` after a download — a miss just means
    no sweep / no preference, never a failure.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        return video_id or None
    query = parse_qs(parsed.query)
    if query.get("v"):
        return query["v"][0] or None
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if parsed.path.startswith(prefix):
            video_id = parsed.path[len(prefix) :].split("/")[0]
            return video_id or None
    return None


def _new_download_dir(out_dir: Path, job_id: str | None) -> Path:
    """A fresh, per-run download dir under ``out_dir``.

    ``.dl-<job>-<rand>/`` (or ``.dl-<rand>/`` when no job id is known):
    dot-prefixed so it never shows up in uploads listings, unique per
    run so a job retry never resumes into — or collides with — a
    previous run's fragments, not even its own.
    """
    tag = f"{job_id}-{uuid.uuid4().hex[:8]}" if job_id else uuid.uuid4().hex[:12]
    return out_dir / f".dl-{tag}"


def _cleanup_stale_downloads(out_dir: Path, video_id: str | None) -> list[str]:
    """Sweep dead-run ``.part`` leftovers for this video from ``out_dir``.

    Only files named ``<video_id>.*`` with a yt-dlp temp suffix
    (``.part``, ``.part-Frag*``, ``.ytdl``) whose mtime is older than
    :data:`STALE_PART_MAX_AGE_S` are removed. A file a live (orphaned)
    yt-dlp is still writing has a fresh mtime and is left alone, and
    a file another process holds open simply fails to unlink on
    Windows — that is swallowed, never fatal. Returns the names
    removed, for logging / tests.
    """
    if not video_id:
        return []
    removed: list[str] = []
    now = time.time()
    try:
        entries = list(out_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if not name.startswith(f"{video_id}."):
            continue
        if not (name.endswith(".part") or name.endswith(".ytdl") or ".part-Frag" in name):
            continue
        try:
            if now - entry.stat().st_mtime < STALE_PART_MAX_AGE_S:
                continue
            entry.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed


def looks_like_download_lock(stderr: str) -> bool:
    """True when text shows a Windows file-lock failure.

    Matches yt-dlp stderr signatures (WinError 32, a fragment rename
    refused, the download failing because the ``.part`` file it was
    writing is suddenly gone) and the plain-language message built
    from one by :func:`download_lock_message`.
    """
    text = (stderr or "").lower()
    if "winerror 32" in text or "[error 32]" in text:
        return True
    if "being used by another process" in text:
        return True
    # The plain-language message :func:`download_lock_message` builds
    # from a lock also matches, so ``plain_error`` maps an IngestError
    # raised with that message to the lock title / action instead of
    # the generic ingest one.
    if "another program had the file open" in text:
        return True
    if "unable to rename file" in text and (
        "permission denied" in text or "errno 13" in text or "error 32" in text
    ):
        return True
    if "unable to remove file" in text and (
        "permission denied" in text or "errno 13" in text or "error 32" in text
    ):
        return True
    return (
        "unable to download video" in text
        and "no such file or directory" in text
        and ".part" in text
    )


def download_lock_message(data_dir: Path) -> str:
    """Plain-language explanation of a persistent download file lock.

    Names the likely locker (antivirus / Windows Defender real-time
    scanning) and the fix (exclude the ClipForge data folder), instead
    of the raw ``[Error 32]`` traceback the user saw in v0.9.1.
    """
    return (
        "The download stopped because Windows would not let yt-dlp finish "
        "writing a piece of the video — another program had the file open. "
        "This is usually your antivirus (Windows Defender) scanning the "
        "file as it downloads. Add this folder to your antivirus "
        f"exclusions (Windows Security > Virus & threat protection > "
        f"Exclusions): {data_dir}. Then retry the job — the video itself "
        "is fine and nothing was published."
    )


def _move_finished_file(src: Path, dest: Path) -> None:
    """Move a finished download into place, riding out brief locks.

    ``os.replace`` on a just-finished file can still hit a momentary
    lock from antivirus scanning on Windows; retry with backoff
    before giving up. Raises the last :class:`OSError` when the lock
    persists.
    """
    last: OSError | None = None
    for attempt in range(_MOVE_ATTEMPTS):
        try:
            os.replace(src, dest)
            return
        except OSError as exc:
            last = exc
            if attempt + 1 < _MOVE_ATTEMPTS:
                time.sleep(_MOVE_DELAY_S * (attempt + 1))
    assert last is not None  # _MOVE_ATTEMPTS >= 1
    raise last


def _pick_finished_file(dl_dir: Path, video_id: str | None) -> Path | None:
    """The finished mp4 of this run — never a ``.part`` fragment.

    Only files directly in the job's own download dir are considered
    (nothing else can be in there), and only final ``.mp4`` files:
    ``*.mp4`` never matches ``*.mp4.part``. Prefers the exact
    ``<video_id>.mp4``; otherwise takes the newest by mtime.
    """
    candidates = sorted(dl_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return None
    if video_id:
        exact = dl_dir / f"{video_id}.mp4"
        if exact in candidates:
            return exact
    return candidates[-1]


# ---------------------------------------------------------------------------
# Fail-fast ingest (v0.10.3)
#
# The user's v0.10.2 job e01b83e18de34ada (Comedy link CMzctRZQlo0)
# ran 3601 s and died with the generic "This step took too long".
# The video itself had been removed from YouTube — oEmbed answers 404
# for it in ~0.5 s and yt-dlp's extractor fails on it in ~2 s — so the
# hour was not the video being slow. It was three missing guards:
#
# * no availability check before downloading: ingest went straight to
#   a full download of a link that could never produce a file;
# * no socket timeout: yt-dlp was invoked with no ``--socket-timeout``
#   (its default is unset, so a stalled connection can hang
#   indefinitely), and no progress watchdog watched it;
# * the only guard was the 3600 s outer timeout in ``wait_for_process``,
#   whose ``TimeoutError`` the worker mapped to the generic
#   "took too long" message — the real stage error never surfaced.
#
# The fix below is the shape ClipNova-AI already uses (see
# ``studio/media.py`` there): check the link with a metadata-only
# probe *before* any download (``info()`` / ``preview()``), pass a
# bounded socket timeout (ClipNova: ``socket_timeout: 20``), and
# report progress. What ClipForge adds on top of ClipNova: the oEmbed
# check first (cheapest possible — a plain HTTPS GET, <1 s for a dead
# link), per-failure classification with plain-language next steps,
# a stall watchdog that kills a download producing no output, and a
# one-time retry on YouTube's Android player client when the web
# client is bot-blocked (verified while building this: the Android
# client answers where the web client gets "Sign in to confirm
# you're not a bot" from a flagged IP).
# ---------------------------------------------------------------------------

#: oEmbed is one HTTPS GET; 10 s is generous (a dead link answers 404
#: in ~0.5 s, a live one 200 in ~0.4 s).
PREFLIGHT_OEMBED_TIMEOUT_S = 10
#: Hard cap on one metadata-only yt-dlp probe. A dead link's extractor
#: fails in ~2 s; 30 s only ever binds on a stalled connection.
PREFLIGHT_YTDLP_TIMEOUT_S = 30
#: yt-dlp socket timeout for every call (probe and download): a stalled
#: connection is retried / fails instead of hanging forever.
YTDLP_SOCKET_TIMEOUT_S = 20
#: Extractor retries per probe / download (yt-dlp default is 3; the
#: pre-flight already retried once on the Android client, so 2 is
#: enough and keeps a dead link fast).
YTDLP_EXTRACTOR_RETRIES = 2
#: Last-resort guard on the whole download (was the *only* guard in
#: v0.10.2). The stall watchdog below fires first in practice.
YTDLP_DOWNLOAD_TIMEOUT_S = 3600
#: Kill the download when yt-dlp produces no output at all for this
#: long — a download that is making progress prints ``[download]``
#: lines constantly, so silence means it is stuck, not slow.
YTDLP_STALL_TIMEOUT_S = 180
#: Keep only the tail of yt-dlp's stderr (error classification only
#: needs the last lines; an hour of progress lines must not pile up).
_YTDLP_STDERR_KEEP_BYTES = 65536


def youtube_unavailable_message(url: str) -> str:
    """A removed / private / not-found video: no app can download it."""
    return (
        "This video is no longer available on YouTube — it was removed, "
        "made private, or the link is wrong. No app can download a "
        "removed or private video, so please try a different link."
        f" (Video: {_extract_youtube_id(url) or url})"
    )


def youtube_bot_check_message() -> str:
    """YouTube refused with "Sign in to confirm you're not a bot"."""
    return (
        "YouTube refused the download and asked to sign in to confirm "
        "you're not a bot. The video itself may be fine — YouTube is "
        "blocking automated downloads from this connection right now. "
        "Wait a little while and retry the job."
    )


def youtube_live_message() -> str:
    """A live-stream URL can never finish downloading as a normal job."""
    return (
        "This link is a live stream, not a finished video, so it can't "
        "be downloaded as a normal job — a live download would never "
        "finish. Use Live capture for live streams instead."
    )


def youtube_unreachable_message() -> str:
    """YouTube could not be reached at all (timeout / dropped link)."""
    return (
        "ClipForge couldn't reach YouTube to check this link — the "
        "request timed out or the connection dropped. Check your "
        "internet connection and retry the job."
    )


def download_stalled_message(stalled_s: float) -> str:
    """yt-dlp produced no output for a long time: stuck, not slow."""
    return (
        f"The download stalled — no data arrived from YouTube for "
        f"{int(stalled_s)}s, so it was stopped instead of waiting "
        "indefinitely. Check your internet connection and retry the job."
    )


def classify_ytdlp_error(stderr: str) -> str | None:
    """Classify yt-dlp stderr into a short reason code (or None).

    Only yt-dlp's own wording is matched — a video that is genuinely
    gone says so ("Video unavailable"), a bot-check says so, and a
    dropped connection says so. Anything unrecognised stays None and
    the caller falls back to the raw tail. Curly quotes are normalised
    first: yt-dlp's bot-check reads "you’re" (U+2019), not "you're".
    """
    text = (stderr or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    if "confirm you're not a bot" in text or "confirm you are not a bot" in text:
        return "youtube_bot_check"
    if "private video" in text or "this video is private" in text:
        return "youtube_unavailable"
    if (
        "video unavailable" in text
        or "this video is unavailable" in text
        or "video has been removed" in text
        or "removed by the uploader" in text
        or "account associated with this video has been terminated" in text
        or "video not found" in text
    ):
        return "youtube_unavailable"
    if "confirm your age" in text or "age-gated" in text or "age restricted" in text:
        return "youtube_age_gated"
    if (
        "not available in your country" in text
        or "blocked in your country" in text
        or "not available in your region" in text
    ):
        return "youtube_geo_blocked"
    if (
        "unable to download webpage" in text
        or "remote end closed" in text
        or "connection reset" in text
        or "connection timed out" in text
        or "timed out" in text
        or "name or service not known" in text
        or "temporary failure in name resolution" in text
        or "network is unreachable" in text
        or "failed to establish a new connection" in text
        # A MITM proxy / captive portal presenting a certificate
        # Python cannot verify (seen on this VM's egress proxy:
        # "self-signed certificate in certificate chain" on the
        # YouTube API page) is a network problem, not a bad video.
        or "certificate verify failed" in text
        or "self-signed certificate" in text
    ):
        return "youtube_unreachable"
    return None


def _ytdlp_error(url: str, stderr: str) -> IngestError:
    """Build the IngestError for a yt-dlp failure, classified.

    The file-lock case is handled by the caller (it needs ``data_dir``
    for the plain-language message); everything else maps here.
    """
    code = classify_ytdlp_error(stderr)
    if code == "youtube_bot_check":
        return IngestError(youtube_bot_check_message(), code=code)
    if code == "youtube_unavailable":
        return IngestError(youtube_unavailable_message(url), code=code)
    if code == "youtube_age_gated":
        return IngestError(
            "This video is age-restricted on YouTube, so it can't be "
            "downloaded without signing in. Try a different link.",
            code=code,
        )
    if code == "youtube_geo_blocked":
        return IngestError(
            "This video isn't available in your country on YouTube, so "
            "it can't be downloaded from here. Try a different link.",
            code=code,
        )
    if code == "youtube_unreachable":
        return IngestError(youtube_unreachable_message(), code=code)
    return IngestError(f"yt-dlp failed for {url!r}: {stderr.strip()[-500:]}")


def _oembed_status(url: str) -> str:
    """``'available'`` | ``'unavailable'`` | ``'unknown'`` via oEmbed.

    oEmbed answers 404 for a removed / private video and 200 for a
    live one, in well under a second — the cheapest availability
    check there is. 401 (age-gated) and any transport error are
    ``'unknown'``: the yt-dlp probe below classifies those properly
    instead of mis-reporting an age-gated video as removed.
    """
    endpoint = (
        "https://www.youtube.com/oembed?url=" + urllib.parse.quote(url, safe="") + "&format=json"
    )
    req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=PREFLIGHT_OEMBED_TIMEOUT_S) as resp:
            return "available" if resp.status == 200 else "unknown"
    except urllib.error.HTTPError as exc:
        return "unavailable" if exc.code == 404 else "unknown"
    except Exception:
        return "unknown"


def _ytdlp_simulate(
    yt_dlp: str,
    url: str,
    *,
    android_client: bool = False,
    timeout: float = PREFLIGHT_YTDLP_TIMEOUT_S,
) -> dict:
    """Metadata-only yt-dlp probe: the video's info dict, no download.

    Raises :class:`IngestError` (classified) when the video cannot be
    inspected — removed / private / bot-check / unreachable. A probe
    that itself times out is "unreachable", never "unavailable": a
    stalled network must not be reported as a removed video.
    """
    cmd = [
        yt_dlp,
        "--no-playlist",
        "--simulate",
        "--dump-json",
        "--socket-timeout",
        "15",
        "--extractor-retries",
        "1",
        "--no-warnings",
    ]
    if android_client:
        cmd += ["--extractor-args", "youtube:player_client=android"]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise IngestError(youtube_unreachable_message(), code="youtube_unreachable") from exc
    except OSError as exc:
        raise IngestError(youtube_unreachable_message(), code="youtube_unreachable") from exc
    if proc.returncode == 0:
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise IngestError(youtube_unreachable_message(), code="youtube_unreachable") from exc
        if not isinstance(info, dict):
            raise IngestError(youtube_unreachable_message(), code="youtube_unreachable")
        return info
    raise _ytdlp_error(url, proc.stderr)


def check_youtube_available(url: str, yt_dlp: str) -> dict:
    """Fail-fast pre-flight: is this YouTube link a downloadable video?

    oEmbed first (a dead link answers 404 in <1 s); then a
    metadata-only yt-dlp probe, which catches what oEmbed cannot
    (age-gate, geo-block, live stream, bot-check). A bot-check on the
    web client gets one retry on YouTube's Android player client,
    which answers where the web client is refused from a flagged IP.
    Returns the probe's info dict (title / duration / ...) on success;
    raises :class:`IngestError` with a plain-language, classified
    message otherwise. A dead link fails here in seconds — it never
    reaches the download.
    """
    if _oembed_status(url) == "unavailable":
        raise IngestError(youtube_unavailable_message(url), code="youtube_unavailable")
    try:
        info = _ytdlp_simulate(yt_dlp, url)
    except IngestError as exc:
        if exc.code == "youtube_bot_check":
            info = _ytdlp_simulate(yt_dlp, url, android_client=True)
        else:
            raise
    if info.get("_type") == "playlist":
        raise IngestError(
            "That link is a playlist or channel, not a single video. "
            "Open the video itself and paste its link instead.",
            code="youtube_playlist",
        )
    if info.get("is_live"):
        raise IngestError(youtube_live_message(), code="youtube_live")
    return info


def _run_ytdlp_bounded(
    cmd: list[str],
    control: JobControl | None,
    *,
    timeout: float = YTDLP_DOWNLOAD_TIMEOUT_S,
    stall_timeout: float = YTDLP_STALL_TIMEOUT_S,
) -> tuple[int, str]:
    """Run yt-dlp, killing it on cancel, total timeout, or a stall.

    A reader thread drains stderr (yt-dlp prints ``[download]``
    progress lines constantly while it is actually downloading) and
    stamps the last output time; the main loop kills the process when
    no output arrives for ``stall_timeout`` — the v0.10.2 hang, where
    a stalled connection sat silent until the 3600 s outer timeout,
    now ends in minutes with a "download stalled" error instead.
    Returns ``(returncode, stderr_text)``. Raises :class:`IngestError`
    (``download_stalled`` / ``download_timeout``) instead of
    ``TimeoutError``, so the worker reports the real stage error;
    raises ``JobCancelled`` on a user cancel.
    """
    from clipforge.errors import JobCancelled  # noqa: PLC0415 (avoid cycle)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **popen_kwargs()
    )
    assert proc.stderr is not None
    if control is not None:
        # A user cancel kills yt-dlp through the registered process
        # (JobControl.request_cancel → kill_process), as before; the
        # poll loop below is the second path that notices the flag.
        control.register_process(proc)
    chunks: list[bytes] = []
    kept = {"n": 0}
    last_output = {"t": time.monotonic()}
    lock = threading.Lock()

    def _reader() -> None:
        try:
            while True:
                data = proc.stderr.read1(4096)
                if not data:
                    return
                with lock:
                    chunks.append(data)
                    kept["n"] += len(data)
                    while kept["n"] > _YTDLP_STDERR_KEEP_BYTES and len(chunks) > 1:
                        kept["n"] -= len(chunks.pop(0))
                last_output["t"] = time.monotonic()
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    started = time.monotonic()
    code: int | None = None
    try:
        while True:
            if control is not None and control.is_cancelled:
                _terminate_process(proc)
                raise JobCancelled("Job cancelled by the user.")
            code = proc.poll()
            if code is not None:
                break
            now = time.monotonic()
            if now - started > timeout:
                _terminate_process(proc)
                raise IngestError(
                    f"The download ran longer than {int(timeout)}s without "
                    "finishing, so it was stopped. The video may be very "
                    "long or your connection may be too slow — try a "
                    "shorter video, or retry when your connection is better.",
                    code="download_timeout",
                )
            if now - last_output["t"] > stall_timeout:
                _terminate_process(proc)
                raise IngestError(
                    download_stalled_message(now - last_output["t"]),
                    code="download_stalled",
                )
            time.sleep(0.1)
    finally:
        if control is not None:
            control.register_process(None)
        reader.join(timeout=5)
    assert code is not None
    with lock:
        stderr = b"".join(chunks).decode("utf-8", "replace")
    return code, stderr


def ingest_youtube(
    url: str,
    *,
    own_content: bool = False,
    out_dir: str | Path = "data/uploads",
    cookies_from_browser: str | None = None,
    data_dir: str | Path | None = None,
    control: JobControl | None = None,
    job_id: str | None = None,
) -> SourceInfo:
    """Download a YouTube video **only for the caller's own content**.

    The caller must pass ``own_content=True`` — an explicit declaration
    that the video is theirs / they hold the rights to it. Without it,
    :class:`OwnContentRequiredError` is raised and nothing is downloaded.
    Requires ``yt-dlp``: the one-click installer's bundled binary
    (``<data_dir>/yt-dlp/``, ``data_dir`` falling back to
    ``CLIPFORGE_DATA_DIR`` / ``./data``) is tried first, then PATH.
    When it is missing altogether a clear error is raised that points
    at the in-app Install button instead of failing obscurely.

    Download robustness (v0.9.2 — see the module header): the video is
    downloaded into a fresh per-run directory under ``out_dir``
    (``.dl-<job>-<rand>/``), never straight into the shared uploads
    dir, so no two runs can share — or lock — the same fragment files;
    stale ``.part`` leftovers for the same video are swept first; and
    yt-dlp is given explicit fragment / file-access retries with a
    1 s sleep, so a transient antivirus lock is ridden out. The
    finished file is then moved into ``out_dir`` as ``<id>.mp4`` and
    only that file is ffprobe-validated — no ``.part`` file is ever
    opened or probed while yt-dlp is writing it. ``job_id`` (when the
    caller knows it) names the download dir for debugging; a retry of
    the same job still gets a fresh dir and re-downloads.

    Fail-fast (v0.10.3): :func:`check_youtube_available` runs first —
    oEmbed, then a metadata-only yt-dlp probe — so a removed / private
    link fails in seconds with a classified plain-language error
    instead of burning the download timeout. The download itself runs
    under :func:`_run_ytdlp_bounded` (socket timeout + stall watchdog;
    3600 s only as a last resort) with one Android-client retry when
    the web client is bot-blocked.
    """
    if not own_content:
        raise OwnContentRequiredError(
            "YouTube ingest requires own_content=True: ClipForge only "
            "downloads content you own or hold the rights to. Refusing to "
            f"download {url!r} without that declaration."
        )
    if not _is_youtube_url(url):
        raise IngestError(f"not a YouTube URL: {url!r}")
    yt_dlp = resolve_yt_dlp_path(data_dir)
    if yt_dlp is None:
        raise IngestError(ytdlp_missing_message())
    # v0.10.3 fail-fast: check the link is a downloadable video BEFORE
    # starting a download. A removed / private link fails here in
    # seconds with a plain-language reason; it never reaches yt-dlp.
    check_youtube_available(url, yt_dlp)
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    video_id = _extract_youtube_id(url)
    _cleanup_stale_downloads(dest, video_id)
    dl_dir = _new_download_dir(dest, job_id)
    dl_dir.mkdir(parents=True, exist_ok=True)
    template = str(dl_dir / "%(id)s.%(ext)s")
    cmd = [
        yt_dlp,
        "--no-playlist",
        "--format",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        # Retries with backoff (v0.9.2): yt-dlp's defaults — 3
        # file-access retries ~10 ms apart — lose to a moment of
        # antivirus scanning on Windows (the v0.9.1 Error 32 failure).
        # Explicit values ride out a transient lock; a lock that still
        # persists is reported in plain language below.
        "--retries",
        str(YTDLP_RETRIES),
        "--fragment-retries",
        str(YTDLP_FRAGMENT_RETRIES),
        "--file-access-retries",
        str(YTDLP_FILE_ACCESS_RETRIES),
        "--retry-sleep",
        YTDLP_FILE_ACCESS_RETRY_SLEEP,
        # v0.10.3: bound every network wait. Without --socket-timeout
        # a stalled connection hangs yt-dlp indefinitely (the 3601 s
        # job); with it, a stall fails fast and the retries above (or
        # the watchdog in _run_ytdlp_bounded) take over.
        "--socket-timeout",
        str(YTDLP_SOCKET_TIMEOUT_S),
        "--extractor-retries",
        str(YTDLP_EXTRACTOR_RETRIES),
        "-o",
        template,
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd.append(url)
    try:
        # One bounded runner for both paths (v0.10.3): the old
        # subprocess.run(timeout=3600) / wait_for_process(timeout=3600)
        # pair had no stall detection, so a silent yt-dlp burned the
        # whole hour and surfaced only as a generic TimeoutError.
        returncode, stderr = _run_ytdlp_bounded(cmd, control)
        if returncode != 0 and classify_ytdlp_error(stderr) == "youtube_bot_check":
            # One retry on YouTube's Android player client, which
            # answers where the web client is bot-blocked from a
            # flagged IP (verified while building v0.10.3). The
            # extractor-args go before the URL (the last element).
            retry_cmd = cmd[:-1] + [
                "--extractor-args",
                "youtube:player_client=android",
                url,
            ]
            returncode, stderr = _run_ytdlp_bounded(retry_cmd, control)
    except BaseException:
        # Cancel / timeout / stall (or anything else) mid-download:
        # drop the per-run scratch dir so a cancelled run leaves no
        # litter. A retry starts in a fresh dir anyway — nothing
        # resumes from here.
        shutil.rmtree(dl_dir, ignore_errors=True)
        raise
    if returncode != 0:
        shutil.rmtree(dl_dir, ignore_errors=True)
        if looks_like_download_lock(stderr):
            raise IngestError(download_lock_message(dest.resolve()), code="download_file_lock")
        raise _ytdlp_error(url, stderr)
    finished = _pick_finished_file(dl_dir, video_id)
    if finished is None:
        shutil.rmtree(dl_dir, ignore_errors=True)
        raise IngestError(f"yt-dlp reported success but produced no .mp4 in {dl_dir}")
    # Move the finished file into the shared uploads dir (same volume,
    # so this is a rename) and validate only that file. The move retries
    # brief locks; a lock that persists is the same antivirus story.
    target = dest / finished.name
    try:
        _move_finished_file(finished, target)
    except OSError:
        raise IngestError(
            download_lock_message(dest.resolve()), code="download_file_lock"
        ) from None
    finally:
        # Per-run scratch dir: remove it once the finished file has
        # been moved out (or the move failed). Best-effort — a file
        # another process still holds open simply stays behind.
        shutil.rmtree(dl_dir, ignore_errors=True)
    info = validate_local_file(target)
    info.source_kind = "youtube"
    info.source_url = url
    info.own_content = True
    return info
