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
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def ingest_youtube(
    url: str,
    *,
    own_content: bool = False,
    out_dir: str | Path = "data/uploads",
    cookies_from_browser: str | None = None,
    data_dir: str | Path | None = None,
    control: JobControl | None = None,
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
    template = str(dest / "%(id)s.%(ext)s")
    cmd = [
        yt_dlp,
        "--no-playlist",
        "--format",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        template,
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd.append(url)
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
    if returncode != 0:
        raise IngestError(f"yt-dlp failed for {url!r}: {stderr.strip()[-500:]}")
    # Newest mp4 in out_dir from this run.
    candidates = sorted(dest.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise IngestError(f"yt-dlp reported success but produced no .mp4 in {dest}")
    info = validate_local_file(candidates[-1])
    info.source_kind = "youtube"
    info.source_url = url
    info.own_content = True
    return info
