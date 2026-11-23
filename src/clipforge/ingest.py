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
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from clipforge.job_control import JobControl, popen_kwargs, wait_for_process
from clipforge.workflow import ffmpeg_missing_message
from clipforge.ytdlp_install import resolve_yt_dlp_path, ytdlp_missing_message

ALLOWED_EXTENSIONS = {".mp4", ".mov"}
ALLOWED_FORMATS = {"mov,mp4,m4a,3gp,3g2,mj2", "mp4", "mov"}
MAX_SOURCE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB sanity cap


class IngestError(ValueError):
    """Raised when a source cannot be ingested (bad file, bad URL, ...)."""


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
        "-o",
        template,
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd.append(url)
    try:
        if control is None:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            returncode, stderr = proc.returncode, proc.stderr
        else:
            # v0.9.0: cancellable download — a user cancel kills yt-dlp
            # mid-download and raises JobCancelled instead of waiting.
            popen = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **popen_kwargs()
            )
            control.register_process(popen)
            try:
                returncode = wait_for_process(popen, control, timeout=3600)
            finally:
                control.register_process(None)
            _, stderr = popen.communicate()
    except BaseException:
        # Cancel / timeout (or anything else) mid-download: drop the
        # per-run scratch dir so a cancelled run leaves no litter. A
        # retry starts in a fresh dir anyway — nothing resumes from
        # here.
        shutil.rmtree(dl_dir, ignore_errors=True)
        raise
    if returncode != 0:
        shutil.rmtree(dl_dir, ignore_errors=True)
        if looks_like_download_lock(stderr):
            raise IngestError(download_lock_message(dest.resolve()))
        raise IngestError(f"yt-dlp failed for {url!r}: {stderr.strip()[-500:]}")
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
        raise IngestError(download_lock_message(dest.resolve())) from None
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
