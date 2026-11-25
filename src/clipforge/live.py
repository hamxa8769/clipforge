"""Live video clipping (v0.7.0, Worker C) — live discovery + bounded capture.

Two pieces, kept in one module so the router and the worker share them:

1. **Live discovery** — the server searches YouTube for streams that are
   live *right now* in the user's niche, reusing the trending search
   plumbing (:mod:`clipforge.trending` credentials, ``search.list`` +
   ``videos.list``):

   * ``search.list`` with ``eventType=live`` (+ ``videoLicense=
     creativeCommon`` when licensed-only mode is on);
   * ``videos.list`` (``part=snippet,statistics,status,
     liveStreamingDetails``) for the same ids — ``status.license``
     is the licence (``creativeCommon`` / ``youtube``),
     ``liveStreamingDetails.concurrentViewers`` the viewers and
     ``actualStartTime`` when the stream started.

   Licence reporting (advisory since v0.9.1): :func:`clip_decision`
   reports a live stream's licence with the rights notice — it never
   refuses a stream. The v0.7.0 hard block (standard-licence or
   unverifiable third-party streams refused) was removed at the user's
   explicit instruction after they checked YouTube's terms and
   confirmed clipping is officially allowed. The licence is still
   re-verified against the YouTube API at capture time (never trusted
   from the client) so the job record and any CC attribution are
   accurate; the verdict itself no longer blocks capture.

   Every result carries ``can_clip``: always True since v0.9.1, with
   ``clip_reason`` carrying the licence report / rights notice.

2. **Live capture** — :func:`capture_live_stream` records a *bounded*
   segment of a live stream with yt-dlp (never an unbounded recording:
   60–300 seconds, default 120s) into the existing ingest path. The
   captured file is validated with ffprobe exactly like any other
   source, and the worker then runs the EXISTING pipeline
   (transcribe -> score -> clip -> SEO -> approval queue) on it — the
   pipeline is reused, not duplicated. A capture that cannot happen
   (stream ended, geo/age restricted, DRM, not recordable) fails with a
   plain-language reason; a clip is never faked.

Honest limits (also in the module docstring of the router): yt-dlp
records from the live edge, not from the stream's start — a moment
that already passed cannot be captured. DRM-protected, age-restricted
(without a signed-in session) and geo-blocked streams cannot be
recorded at all. If the stream ends mid-capture, the segment recorded
up to that point is kept when it is a readable video, otherwise the
job fails plainly. Stopping a recording mid-way can leave a file
whose last seconds are unplayable; ffprobe validation rejects a file
that is not readable at all.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from clipforge.auth.secrets import SecretStore
from clipforge.ingest import SourceInfo
from clipforge.trending import (
    YOUTUBE_API_BASE,
    TrendingCredentials,
    TrendingError,
    _safe_json,
    _youtube_error_message,
    check_video_license,
    extract_youtube_video_id,
    own_channel_id,
)
from clipforge.trending import extract_youtube_video_id as _extract_id
from clipforge.ytdlp_install import resolve_yt_dlp_path, ytdlp_missing_message

# ------------------------------------------------------------------ limits
# A live capture is always bounded: the user picks 60–300 seconds.
LIVE_MIN_DURATION_S = 60
LIVE_MAX_DURATION_S = 300
LIVE_DEFAULT_DURATION_S = 120
# Seconds to wait for yt-dlp to finish writing after SIGTERM before SIGKILL.
LIVE_STOP_GRACE_S = 15.0
# How often the capture loop checks for cancel / timeout (seconds).
_CAPTURE_POLL_S = 0.5

LICENCE_CREATIVE_COMMONS = "creativeCommon"
LICENCE_STANDARD = "youtube"


class LiveError(ValueError):
    """Plain-language live failure — safe to show to the user as-is."""


class LiveCaptureError(LiveError):
    """A live capture failed (stream ended, restricted, not recordable)."""


class LiveCaptureCancelled(LiveCaptureError):
    """The user cancelled a live capture while it was recording."""


# ------------------------------------------------------------------ clip decision
def clip_decision(licence: str, is_own: bool) -> tuple[bool, str]:
    """Report whether a live stream may be clipped, and the reason.

    Always ``(True, reason)`` — the licence is reported as metadata,
    never enforced. The v0.7.0 hard block (standard-licence or
    unverifiable third-party streams refused) was removed at the
    user's explicit instruction after they checked YouTube's terms.
    v0.11.0 removed the rights notice from the reasons.
    """
    if is_own:
        return True, "Your own live stream — you can clip it."
    if licence == LICENCE_CREATIVE_COMMONS:
        return True, "Creative Commons licensed live stream — you can clip it."
    if licence == LICENCE_STANDARD:
        return True, "This live stream uses the standard YouTube licence."
    return True, "The licence of this live stream could not be confirmed."


def check_can_clip(licence: str, is_own: bool) -> None:
    """Never raises for a licence.

    Kept for backward compatibility: the licence is reported by
    :func:`clip_decision`, not enforced. (The worker's live-ingest
    re-check that used to fail jobs here was removed with it.)
    """
    return None


# ------------------------------------------------------------------ discovery
@dataclass
class LiveVideo:
    """One live stream, right now (from the live YouTube API)."""

    video_id: str
    title: str
    channel_title: str
    channel_id: str = ""
    started_at: str = ""
    viewers: int = 0
    licence: str = "unknown"  # "creativeCommon" | "youtube" | "unknown"
    is_own: bool = False
    can_clip: bool = False
    clip_reason: str = ""

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self) -> dict:
        return {**asdict(self), "url": self.url}


def _parse_live_video(item: dict, *, own_channel_id: str = "") -> LiveVideo | None:
    """One videos.list item -> :class:`LiveVideo` (None when unusable)."""
    if not isinstance(item, dict) or not item.get("id"):
        return None
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    details = item.get("liveStreamingDetails") or {}
    stats = item.get("statistics") or {}
    channel_id = str(snippet.get("channelId") or "")
    licence = str(status.get("license") or "unknown")
    is_own = bool(own_channel_id) and channel_id == own_channel_id
    can_clip, reason = clip_decision(licence, is_own)
    started = str(details.get("actualStartTime") or snippet.get("publishedAt") or "")
    try:
        viewers = int(details.get("concurrentViewers") or stats.get("viewCount") or 0)
    except (TypeError, ValueError):
        viewers = 0
    return LiveVideo(
        video_id=str(item.get("id")),
        title=str(snippet.get("title") or ""),
        channel_title=str(snippet.get("channelTitle") or ""),
        channel_id=channel_id,
        started_at=started,
        viewers=viewers,
        licence=licence,
        is_own=is_own,
        can_clip=can_clip,
        clip_reason=reason,
    )


def _videos_details(
    http: httpx.Client,
    credentials: TrendingCredentials,
    video_ids: list[str],
) -> list[dict]:
    """videos.list for live ids: licence + viewers + start time."""
    resp = http.get(
        f"{YOUTUBE_API_BASE}/videos",
        params={
            "part": "snippet,statistics,status,liveStreamingDetails",
            "id": ",".join(video_ids),
            **credentials.auth_params(),
        },
        headers=credentials.auth_headers(),
    )
    if resp.status_code != 200:
        raise LiveError(_youtube_error_message(_safe_json(resp), resp.status_code))
    return _safe_json(resp).get("items") or []


def _live_search_ids(
    http: httpx.Client,
    credentials: TrendingCredentials,
    *,
    query: str | None,
    channel_id: str | None,
    licensed_only: bool,
    max_results: int,
) -> list[str]:
    """search.list eventType=live -> video ids (raises LiveError)."""
    params: dict = {
        "part": "snippet",
        "type": "video",
        "eventType": "live",
        "maxResults": max(1, min(25, max_results)),
        **credentials.auth_params(),
    }
    if query:
        params["q"] = query
    if channel_id:
        params["channelId"] = channel_id
    if licensed_only:
        params["videoLicense"] = LICENCE_CREATIVE_COMMONS
    resp = http.get(
        f"{YOUTUBE_API_BASE}/search",
        params=params,
        headers=credentials.auth_headers(),
    )
    if resp.status_code != 200:
        raise LiveError(_youtube_error_message(_safe_json(resp), resp.status_code))
    ids: list[str] = []
    for item in _safe_json(resp).get("items") or []:
        video_id = (item.get("id") or {}).get("videoId")
        if video_id:
            ids.append(video_id)
    return ids


def search_live(
    niche: str,
    *,
    credentials: TrendingCredentials,
    licensed_only: bool = False,
    own_channel_id: str = "",
    max_results: int = 10,
    client: httpx.Client | None = None,
) -> list[LiveVideo]:
    """Live streams in a niche, right now.

    ``search.list`` (``eventType=live``, ``q`` = the niche keywords,
    ``videoLicense=creativeCommon`` when ``licensed_only``) finds the
    live streams; ``videos.list`` verifies each one's licence and
    reads viewers / start time. ``can_clip`` is always True (advisory
    since v0.9.1 — the licence is reported, never enforced). Raises
    :class:`LiveError` in plain language when YouTube rejects the call
    — never invented streams.
    """
    from clipforge.trending import niche_search_query  # noqa: PLC0415 (avoid cycle)

    own_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        ids = _live_search_ids(
            http,
            credentials,
            query=niche_search_query(niche),
            channel_id=None,
            licensed_only=licensed_only,
            max_results=max_results,
        )
        if not ids:
            return []
        return [
            video
            for video in (
                _parse_live_video(item, own_channel_id=own_channel_id)
                for item in _videos_details(http, credentials, ids)
            )
            if video is not None
        ]
    except LiveError:
        raise
    except httpx.HTTPError as exc:
        raise LiveError(
            "Could not reach the YouTube API — check your internet "
            f"connection and try again. ({exc})"
        ) from exc
    finally:
        if own_client:
            http.close()


def resolve_own_channel_id(
    secrets: SecretStore,
    *,
    credentials: TrendingCredentials | None = None,
    client: httpx.Client | None = None,
) -> str:
    """The connected YouTube channel's id ("" when unknown).

    First the stored OAuth record's ``account_ref`` (via Worker A's
    :func:`clipforge.trending.own_channel_id`, set from channels.list
    ``mine=true`` at connect time); otherwise ``channels.list`` with
    ``mine=true`` on an OAuth credential. An API-key credential cannot
    answer this — it knows no account — so it returns "".
    """
    stored = own_channel_id(secrets)
    if stored.startswith("UC"):
        return stored
    if credentials is None or credentials.kind != "oauth":
        return ""
    own_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(
            f"{YOUTUBE_API_BASE}/channels",
            params={"part": "id", "mine": "true"},
            headers=credentials.auth_headers(),
        )
        if resp.status_code != 200:
            return ""
        items = _safe_json(resp).get("items") or []
        return str((items[0].get("id") or "") if items else "")
    except httpx.HTTPError:
        return ""
    finally:
        if own_client:
            http.close()


def list_own_live(
    *,
    credentials: TrendingCredentials,
    own_channel_id: str = "",
    client: httpx.Client | None = None,
) -> list[LiveVideo]:
    """The connected channel's current live broadcasts.

    Prefers ``liveBroadcasts.list`` (``mine=true``,
    ``broadcastStatus=active``) on an OAuth credential — the
    broadcasts endpoint. When YouTube refuses it (e.g. the token lacks
    the scope) it falls back to ``search.list`` by channel id +
    ``eventType=live``. With an API-key credential and no known channel
    id there is nothing to list — raises :class:`LiveError` saying to
    connect the channel. Every result is the user's own: ``is_own``
    is True (and ``can_clip`` is True, as for every stream since the
    v0.9.1 advisory change).
    """
    own_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        if credentials.kind == "oauth":
            resp = http.get(
                f"{YOUTUBE_API_BASE}/liveBroadcasts",
                params={
                    "part": "snippet,status",
                    "broadcastStatus": "active",
                    "mine": "true",
                    "maxResults": 25,
                },
                headers=credentials.auth_headers(),
            )
            if resp.status_code == 200:
                ids = [
                    str(item.get("id") or "")
                    for item in _safe_json(resp).get("items") or []
                    if (item.get("id") or "")
                ]
                if not ids:
                    return []
                return [
                    video
                    for video in (
                        _parse_live_video(item, own_channel_id=own_channel_id)
                        for item in _videos_details(http, credentials, ids)
                    )
                    if video is not None
                ]
            # 403/400 (missing scope, not enabled) -> fall through to
            # the search fallback below; any other error is reported.
            if resp.status_code not in {400, 403}:
                raise LiveError(_youtube_error_message(_safe_json(resp), resp.status_code))
        if not own_channel_id:
            raise LiveError(
                "To list your own live streams, connect your YouTube "
                "channel first (Channels > Connect YouTube). An API key "
                "alone does not say which channel is yours."
            )
        ids = _live_search_ids(
            http,
            credentials,
            query=None,
            channel_id=own_channel_id,
            licensed_only=False,
            max_results=25,
        )
        if not ids:
            return []
        return [
            video
            for video in (
                _parse_live_video(item, own_channel_id=own_channel_id)
                for item in _videos_details(http, credentials, ids)
            )
            if video is not None
        ]
    except LiveError:
        raise
    except httpx.HTTPError as exc:
        raise LiveError(
            "Could not reach the YouTube API — check your internet "
            f"connection and try again. ({exc})"
        ) from exc
    finally:
        if own_client:
            http.close()


def verify_live_video(
    video_id: str,
    *,
    credentials: TrendingCredentials,
    own_channel_id: str = "",
    client: httpx.Client | None = None,
) -> LiveVideo:
    """Re-verify one live stream before capture: licence + ownership.

    Delegates to Worker A's :func:`clipforge.trending.check_video_license`
    (live ``videos.list`` ``status.license`` check) so the job record
    carries the true licence (never a faked ``licence`` field from the
    client). Advisory since v0.9.1: the verdict never blocks capture —
    the licence is reported with the rights notice. Raises
    :class:`LiveError` when the video cannot be found — it may have
    ended — or YouTube rejects the call.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        checked = check_video_license(
            url,
            credentials=credentials,
            own_channel_id=own_channel_id,
            client=client,
        )
    except TrendingError as exc:
        raise LiveError(str(exc)) from exc
    licence = str(checked.get("license") or "unknown")
    channel_id = str(checked.get("channel_id") or "")
    is_own = bool(own_channel_id) and channel_id == own_channel_id
    can_clip, reason = clip_decision(licence, is_own)
    return LiveVideo(
        video_id=str(checked.get("video_id") or video_id),
        title=str(checked.get("title") or ""),
        channel_title=str(checked.get("channel") or ""),
        channel_id=channel_id,
        started_at="",
        viewers=0,
        licence=licence,
        is_own=is_own,
        can_clip=bool(checked.get("can_clip")) and can_clip,
        clip_reason=str(checked.get("reason") or reason),
    )


def canonical_source_license(licence: str, is_own: bool) -> str | None:
    """Canonical licence key for a live job's licence record.

    YouTube's ``status.license`` value ``creativeCommon`` is CC BY
    (3.0 on YouTube) — stored as ``cc-by-3.0`` so
    :mod:`clipforge.licensing` recognises it as Creative Commons (the
    raw ``creativeCommon`` string does not start with ``cc``). Own
    live streams store ``own``. Anything else stores ``None``
    (unknown — never silently re-labelled as CC).
    """
    if is_own:
        return "own"
    if licence == LICENCE_CREATIVE_COMMONS:
        return "cc-by-3.0"
    return None


# ------------------------------------------------------------------ capture
# Module-level cancel registry: one event per running live job. The
# worker passes the job's event into the capture loop; the cancel
# endpoint sets it. A capture checks it every poll tick (~0.5s).
_CAPTURE_CANCEL: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def live_cancel_event(job_id: str) -> threading.Event:
    """The cancel event for a live job (created on first use)."""
    with _CANCEL_LOCK:
        return _CAPTURE_CANCEL.setdefault(job_id, threading.Event())


def request_live_cancel(job_id: str) -> None:
    """Ask a running live capture to stop (checked every ~0.5s)."""
    live_cancel_event(job_id).set()


def clear_live_cancel(job_id: str) -> None:
    """Drop a job's cancel event once its capture has finished."""
    with _CANCEL_LOCK:
        _CAPTURE_CANCEL.pop(job_id, None)


def validate_live_duration(duration_s: int | float) -> int:
    """Clamp-check a requested capture length (60–300s, default 120s).

    Raises :class:`LiveCaptureError` in plain language outside the
    range — a capture is never unbounded and never a 5-second sliver.
    """
    try:
        seconds = int(duration_s)
    except (TypeError, ValueError) as exc:
        raise LiveCaptureError(
            f"Capture length must be {LIVE_MIN_DURATION_S}–{LIVE_MAX_DURATION_S} "
            f"seconds (got {duration_s!r})."
        ) from exc
    if seconds < LIVE_MIN_DURATION_S or seconds > LIVE_MAX_DURATION_S:
        raise LiveCaptureError(
            "Capture length must be between 60 and 300 seconds — "
            f"you asked for {seconds} seconds. The default is 120 seconds."
        )
    return seconds


def _plain_capture_error(stderr: str, url: str) -> str:
    """Map yt-dlp's stderr to a plain-language reason (never a traceback)."""
    text = (stderr or "").strip()
    lowered = text.lower()
    last_line = text.splitlines()[-1].strip() if text.splitlines() else ""
    if any(
        phrase in lowered
        for phrase in (
            "sign in to confirm your age",
            "age-restricted",
            "age restricted",
            "confirm your age",
        )
    ):
        return (
            "This live stream is age-restricted and cannot be recorded "
            "without signing in — ClipForge cannot capture it."
        )
    if any(
        phrase in lowered
        for phrase in ("not available in your country", "geo", "blocked in your country")
    ):
        return (
            "This live stream is not available in your country "
            "(geo-blocked), so ClipForge cannot record it."
        )
    if any(
        phrase in lowered
        for phrase in (
            "live stream has ended",
            "has ended",
            "no longer available",
            "is no longer live",
            "this video is no longer",
            "stream is offline",
        )
    ):
        return (
            "This live stream has ended, so there is nothing left to "
            "record. Pick a stream that is live right now."
        )
    if "private video" in lowered or "private live" in lowered:
        return "This live stream is private, so ClipForge cannot record it."
    if "drm" in lowered or "encrypted" in lowered:
        return (
            "This live stream is DRM-protected, so ClipForge cannot "
            "record it. DRM-protected streams can never be captured."
        )
    if any(
        phrase in lowered
        for phrase in (
            "unsupported url",
            "no video formats found",
            "no formats found",
            "unable to download",
        )
    ):
        return (
            "This live stream cannot be recorded with yt-dlp "
            f"({last_line or 'no playable format was found'}). "
            "Try a different live stream."
        )
    if "this video is unavailable" in lowered or "video unavailable" in lowered:
        return (
            "This live stream is unavailable — it may have been removed "
            "or made private. Pick a stream that is live right now."
        )
    return (
        "Could not record this live stream"
        + (f": {last_line}" if last_line else ".")
        + " Check the stream is still live and try again."
    )


def _find_captured_file(out_dir: Path) -> Path | None:
    """The recorded segment (``live_capture.*``), if yt-dlp wrote one."""
    candidates = sorted(out_dir.glob("live_capture.*"))
    for path in candidates:
        if path.is_file() and path.suffix.lower() != ".part" and path.stat().st_size > 0:
            return path
    return None


def capture_live_stream(
    url: str,
    *,
    out_dir: str | Path,
    duration_s: int = LIVE_DEFAULT_DURATION_S,
    popen=None,
    which=None,
    sleep=None,
    monotonic=None,
    cancel_event: threading.Event | None = None,
    on_progress=None,
    validate=None,
) -> SourceInfo:
    """Record a bounded segment of a live stream with yt-dlp.

    yt-dlp records from the live edge for ``duration_s`` seconds, then
    is stopped — the recording is never unbounded. The partial file is
    validated with ffprobe (via ``validate``, default
    :func:`clipforge.ingest.validate_local_file`); only a readable
    video is returned. Raises :class:`LiveCaptureError` with a
    plain-language reason when the stream ends, is restricted, or
    cannot be recorded, and :class:`LiveCaptureCancelled` when the
    user cancels. Never returns a faked or empty clip.

    ``popen`` / ``which`` / ``sleep`` / ``monotonic`` / ``validate``
    are injectable so tests can drive the capture without yt-dlp, a
    clock, or FFmpeg.
    """
    from clipforge.ingest import validate_local_file  # noqa: PLC0415 (avoid cycle)

    seconds = validate_live_duration(duration_s)
    # ``which`` is injectable for tests; otherwise the bundled yt-dlp
    # (one-click install) is used first, then PATH.
    yt_dlp = which("yt-dlp") if which is not None else resolve_yt_dlp_path()
    if not yt_dlp:
        raise LiveCaptureError(ytdlp_missing_message() + " Live capture cannot record without it.")
    video_id = _extract_id(url)
    if not video_id and not url.startswith("http"):
        raise LiveCaptureError(f"Not a YouTube live URL: {url!r}.")
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if cancel_event is not None and cancel_event.is_set():
        raise LiveCaptureCancelled(
            "Live capture cancelled — the recording was stopped and no clip was made from it."
        )
    template = str(dest / "live_capture.%(ext)s")
    cmd = [
        yt_dlp,
        "--no-playlist",
        "--no-part",
        "--format",
        "best[ext=mp4]/best",
        "-o",
        template,
        url,
    ]
    spawn = popen or subprocess.Popen
    nap = sleep or time.sleep
    clock = monotonic or time.monotonic
    check = validate or validate_local_file
    started = clock()
    deadline = started + seconds
    try:
        proc = spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise LiveCaptureError(
            f"Could not start yt-dlp to record the live stream ({exc})."
        ) from exc
    timed_out = False
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=LIVE_STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                partial = _find_captured_file(dest)
                if partial is not None:
                    partial.unlink(missing_ok=True)
                raise LiveCaptureCancelled(
                    "Live capture cancelled — the recording was stopped "
                    "and no clip was made from it."
                )
            rc = proc.poll()
            if rc is not None:
                break
            now = clock()
            if now >= deadline:
                timed_out = True
                break
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, (now - started) / seconds)))
            nap(_CAPTURE_POLL_S)
        if timed_out:
            # Bounded recording: stop yt-dlp after the requested length
            # and keep what it recorded.
            proc.terminate()
            try:
                proc.wait(timeout=LIVE_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            _, stderr = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            stderr = ""
        if on_progress is not None:
            on_progress(1.0)
        if not timed_out and proc.returncode not in (0, None):
            partial = _find_captured_file(dest)
            if partial is not None:
                partial.unlink(missing_ok=True)
            raise LiveCaptureError(_plain_capture_error(stderr or "", url))
        captured = _find_captured_file(dest)
        if captured is None:
            if timed_out:
                raise LiveCaptureError(
                    "The live stream ended before anything could be "
                    "recorded — no video was captured. Pick a stream "
                    "that is live right now."
                )
            raise LiveCaptureError(_plain_capture_error(stderr or "", url))
        try:
            info = check(captured)
        except Exception as exc:  # noqa: BLE001 — unreadable file, plain reason
            captured.unlink(missing_ok=True)
            raise LiveCaptureError(
                "The live stream was recorded, but the captured file "
                f"could not be read as a video ({exc}). No clip was made."
            ) from exc
        info.source_kind = "youtube"
        info.source_url = url
        info.own_content = True
        return info
    except LiveCaptureError:
        raise
    except Exception as exc:  # noqa: BLE001 — never leak a traceback to the user
        partial = _find_captured_file(dest)
        if partial is not None:
            partial.unlink(missing_ok=True)
        raise LiveCaptureError(
            "Could not record this live stream "
            f"({type(exc).__name__}: {exc}). Check the stream is still "
            "live and try again."
        ) from exc


__all__ = [
    "LIVE_DEFAULT_DURATION_S",
    "LIVE_MAX_DURATION_S",
    "LIVE_MIN_DURATION_S",
    "LIVE_STOP_GRACE_S",
    "LICENCE_CREATIVE_COMMONS",
    "LICENCE_STANDARD",
    "LiveCaptureCancelled",
    "LiveCaptureError",
    "LiveError",
    "LiveVideo",
    "capture_live_stream",
    "canonical_source_license",
    "check_can_clip",
    "clear_live_cancel",
    "clip_decision",
    "extract_youtube_video_id",
    "list_own_live",
    "live_cancel_event",
    "request_live_cancel",
    "resolve_own_channel_id",
    "search_live",
    "validate_live_duration",
    "verify_live_video",
]
