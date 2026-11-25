"""Resolve a pasted video / channel URL into something clippable (v0.11.0).

The Dashboard one-box (``POST /resolve``) is ClipNova's simple flow,
re-implemented for ClipForge: paste one link, the app finds the video(s),
clipping starts in one click — no wizard, no licence-check round trip
first. Design borrowed from ClipNova-AI (MIT — see ``docs/REUSE.md``);
no ClipNova code is used, only the flow shape.

* A **video** link (``watch?v=``, ``youtu.be/``, ``/shorts/``,
  ``/embed/``, ``/live/``) resolves to one video via the same
  metadata-only yt-dlp probe ingest already uses
  (:func:`clipforge.ingest._ytdlp_simulate`), so a dead link fails
  here in seconds with a classified plain-language reason instead of
  starting a job that can never download.
* A **channel** link (``/channel/``, ``/c/``, ``/user/``, ``/@handle``)
  or a **playlist** link lists that channel's videos, newest first,
  via yt-dlp flat-playlist extraction (``--flat-playlist
  --dump-single-json``) — no API key needed. The user picks one (or
  several) videos in the Dashboard picker and each starts a normal
  clip job through the existing pipeline.
* A saved **YouTube Data API key** (the same key the Trending page
  already stores — :func:`clipforge.trending.load_youtube_api_key`,
  SecretStore / ``YOUTUBE_API_KEY`` env; no new key store is invented)
  is used when present for channel listings: it returns richer data
  (view counts, exact publish dates) than the flat playlist. Any API
  failure falls back to the yt-dlp listing, so a bad / revoked key
  never blocks resolving — the key is an enhancement, never a
  requirement.

Only YouTube links resolve: anything else is ``unknown_url`` and the
UI says so instead of starting a job that cannot work.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlparse

from clipforge.ingest import IngestError, _is_youtube_url, _ytdlp_simulate

# A channel listing is bounded: the picker shows the newest videos,
# and a huge channel must not make the resolve call hang.
DEFAULT_MAX_VIDEOS = 25
#: Hard cap on one flat-playlist extraction (a big channel's index
#: can take a while; yt-dlp prints the entries as it goes, but the
#: JSON only arrives at the end).
FLAT_PLAYLIST_TIMEOUT_S = 120
#: Hard cap on one YouTube Data API call.
YOUTUBE_API_TIMEOUT_S = 15


class ResolveError(ValueError):
    """A URL could not be resolved into clippable video(s).

    ``code`` is short and machine-readable — ``unknown_url``,
    ``youtube_unavailable``, ``youtube_bot_check``,
    ``youtube_unreachable``, ``youtube_live``, ``channel_empty``,
    ``channel_unavailable`` — so the API can answer 422 with a
    plain-language message and the UI can show the right next step.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ResolvedVideo:
    """One video the picker / one-box can start a clip job from."""

    video_id: str
    title: str
    channel_title: str = ""
    channel_id: str = ""
    duration_s: float | None = None
    thumbnail_url: str | None = None
    published_at: str = ""
    view_count: int = 0

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["url"] = self.url
        return d


def classify_url(url: str) -> str:
    """``'video'`` | ``'channel'`` | ``'playlist'`` | ``'unknown'``.

    A ``watch`` link with a ``list=`` param is still a video — the
    user pasted one video, not the playlist. A bare ``/playlist``
    link is a playlist. Channel shapes: ``/channel/<id>``,
    ``/c/<name>``, ``/user/<name>``, ``/@handle`` (with an optional
    ``/videos`` / ``/shorts`` / ``/streams`` / ``/playlists`` tail).
    Anything that is not a YouTube link is ``'unknown'``.
    """
    text = (url or "").strip()
    if not text or not _is_youtube_url(text):
        return "unknown"
    try:
        parsed = urlparse(text)
    except ValueError:
        return "unknown"
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query)
    if host == "youtu.be":
        return "video" if path.strip("/").split("/")[0] else "unknown"
    if query.get("v"):
        return "video"
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if path.startswith(prefix) and path[len(prefix) :].strip("/"):
            return "video"
    if path.startswith("/playlist") and query.get("list"):
        return "playlist"
    if path.startswith("/channel/") or path.startswith("/c/") or path.startswith("/user/"):
        return "channel"
    first = path.strip("/").split("/")[0] if path.strip("/") else ""
    if first.startswith("@"):
        return "channel"
    return "unknown"


def _channel_videos_url(url: str) -> str:
    """The URL yt-dlp should flat-list for a channel / playlist link.

    A channel root (``/@handle``, ``/channel/UC…``, ``/c/name``,
    ``/user/name``) lists its videos tab; a link that already names a
    tab (``/videos``, ``/shorts``, ``/streams``, ``/playlists``) is
    used as-is, so "latest Shorts" keeps meaning Shorts. Playlist
    links are used as-is.
    """
    parsed = urlparse(url.strip())
    path = parsed.path or ""
    segments = [s for s in path.split("/") if s]
    if segments and segments[0] == "playlist":
        return url.strip()
    tabs = {"videos", "shorts", "streams", "playlists", "featured"}
    if segments and segments[-1] in tabs:
        return url.strip()
    base = url.strip().rstrip("/")
    return f"{base}/videos"


def _pick_thumbnail(info: dict) -> str | None:
    """Best thumbnail URL from a yt-dlp info dict (flat or full)."""
    if isinstance(info.get("thumbnail"), str) and info["thumbnail"]:
        return info["thumbnail"]
    thumbs = info.get("thumbnails")
    if isinstance(thumbs, list):
        urls = [t.get("url") for t in thumbs if isinstance(t, dict) and t.get("url")]
        if urls:
            return urls[-1]
    return None


def _video_from_ytdlp_info(info: dict) -> ResolvedVideo:
    """A full probe info dict → :class:`ResolvedVideo`."""
    duration = info.get("duration")
    try:
        duration_s = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_s = None
    return ResolvedVideo(
        video_id=str(info.get("id") or ""),
        title=str(info.get("title") or ""),
        channel_title=str(info.get("uploader") or info.get("channel") or ""),
        channel_id=str(info.get("channel_id") or info.get("uploader_id") or ""),
        duration_s=duration_s,
        thumbnail_url=_pick_thumbnail(info),
        published_at=str(info.get("upload_date") or ""),
    )


def _video_from_flat_entry(entry: dict) -> ResolvedVideo | None:
    """One flat-playlist entry → :class:`ResolvedVideo` (None if unusable).

    Flat entries carry only id / title / uploader / duration /
    thumbnails — no file, no formats. An entry without an id cannot
    be clipped and is skipped, never fatal.
    """
    video_id = str(entry.get("id") or "")
    if not video_id:
        return None
    duration = entry.get("duration")
    try:
        duration_s = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_s = None
    return ResolvedVideo(
        video_id=video_id,
        title=str(entry.get("title") or ""),
        channel_title=str(entry.get("uploader") or entry.get("channel") or ""),
        channel_id=str(entry.get("channel_id") or entry.get("uploader_id") or ""),
        duration_s=duration_s,
        thumbnail_url=_pick_thumbnail(entry),
        published_at=str(entry.get("upload_date") or ""),
    )


def _flat_playlist(yt_dlp: str, url: str, *, max_results: int = DEFAULT_MAX_VIDEOS) -> dict:
    """Run yt-dlp flat-playlist extraction and return its JSON.

    ``--playlist-end`` bounds the work (newest first, so the first N
    are the ones the picker shows). A bot-check on the web client gets
    one retry on YouTube's Android player client — the same retry
    ingest already uses (v0.10.3): from a flagged IP the web client is
    refused while the Android client answers. Raises
    :class:`ResolveError` (``channel_unavailable`` /
    ``youtube_unreachable`` / bot-check) when the extraction fails —
    the caller turns it into a 422.
    """
    return _flat_playlist_run(yt_dlp, url, max_results=max_results, android_client=False)


def _flat_playlist_run(yt_dlp: str, url: str, *, max_results: int, android_client: bool) -> dict:
    cmd = [
        yt_dlp,
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        str(max_results),
        "--socket-timeout",
        "20",
        "--extractor-retries",
        "2",
        "--no-warnings",
    ]
    if android_client:
        cmd += ["--extractor-args", "youtube:player_client=android"]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FLAT_PLAYLIST_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ResolveError(
            "YouTube took too long to list this channel's videos — "
            "check your internet connection and try again.",
            code="youtube_unreachable",
        ) from exc
    except OSError as exc:
        raise ResolveError(
            "ClipForge couldn't reach YouTube to list this channel's "
            "videos — check your internet connection and try again.",
            code="youtube_unreachable",
        ) from exc
    if proc.returncode != 0:
        from clipforge.ingest import classify_ytdlp_error  # noqa: PLC0415 (avoid cycle at import)

        code = classify_ytdlp_error(proc.stderr)
        if code == "youtube_bot_check" and not android_client:
            # One retry on the Android player client (v0.10.3 pattern).
            return _flat_playlist_run(yt_dlp, url, max_results=max_results, android_client=True)
        if code == "youtube_bot_check":
            raise ResolveError(
                "YouTube refused the lookup and asked to sign in to "
                "confirm you're not a bot. The channel itself may be "
                "fine — YouTube is blocking automated lookups from this "
                "connection right now. Wait a little while and try again.",
                code="youtube_bot_check",
            )
        if code == "youtube_unreachable":
            raise ResolveError(
                "ClipForge couldn't reach YouTube to list this "
                "channel's videos — check your internet connection and "
                "try again.",
                code="youtube_unreachable",
            )
        raise ResolveError(
            "That channel or playlist could not be opened on YouTube — "
            "it may be private, removed, or the link may be wrong. "
            "Check the link and try again.",
            code="channel_unavailable",
        )
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ResolveError(
            "YouTube's answer for this channel could not be read — try again in a moment.",
            code="channel_unavailable",
        ) from exc
    if not isinstance(info, dict):
        raise ResolveError(
            "YouTube's answer for this channel could not be read — try again in a moment.",
            code="channel_unavailable",
        )
    return info


def resolve_video(url: str, yt_dlp: str) -> ResolvedVideo:
    """Resolve one video link to its title / channel / duration.

    Reuses the ingest pre-flight probe, so a removed / private /
    age-gated / bot-blocked link fails here in seconds with the same
    classified plain-language reason ingest would give — the job is
    never created for a video that cannot download. A bot-check on the
    web client gets one retry on YouTube's Android player client, the
    same retry ingest uses (v0.10.3).
    """
    try:
        info = _ytdlp_simulate(yt_dlp, url)
    except IngestError as exc:
        if exc.code == "youtube_bot_check":
            try:
                info = _ytdlp_simulate(yt_dlp, url, android_client=True)
            except IngestError as android_exc:
                raise ResolveError(
                    str(android_exc), code=android_exc.code or "youtube_unavailable"
                ) from android_exc
        else:
            raise ResolveError(str(exc), code=exc.code or "youtube_unavailable") from exc
    if info.get("_type") == "playlist":
        raise ResolveError(
            "That link is a playlist or channel, not a single video. "
            "Paste a channel link to pick a video from it instead.",
            code="youtube_playlist",
        )
    if info.get("is_live"):
        raise ResolveError(
            "This link is a live stream, not a finished video, so it "
            "can't be clipped as a normal job — a live download would "
            "never finish. Use Live capture for live streams instead.",
            code="youtube_live",
        )
    video = _video_from_ytdlp_info(info)
    if not video.video_id:
        raise ResolveError(
            "YouTube's answer for this video could not be read — try again in a moment.",
            code="youtube_unavailable",
        )
    return video


def list_channel_videos_ytdlp(
    url: str, yt_dlp: str, *, max_results: int = DEFAULT_MAX_VIDEOS
) -> tuple[dict, list[ResolvedVideo]]:
    """List a channel / playlist's videos via yt-dlp flat extraction.

    Returns ``(channel, videos)`` — channel is ``{id, title, url}``,
    videos are newest first, capped at ``max_results``. An empty
    listing is ``channel_empty``: the channel exists but has no
    (public) videos on this tab, so the picker would be blank.
    """
    listing_url = _channel_videos_url(url)
    info = _flat_playlist(yt_dlp, listing_url, max_results=max_results)
    entries = info.get("entries")
    if not isinstance(entries, list):
        raise ResolveError(
            "That channel or playlist could not be opened on YouTube — "
            "it may be private, removed, or the link may be wrong.",
            code="channel_unavailable",
        )
    videos = [v for e in entries if isinstance(e, dict) and (v := _video_from_flat_entry(e))]
    channel = {
        "id": str(info.get("channel_id") or info.get("id") or ""),
        "title": str(info.get("channel") or info.get("uploader") or info.get("title") or ""),
        "url": url.strip(),
    }
    if not videos:
        raise ResolveError(
            "This channel has no public videos to pick from on this "
            "page — it may be empty, or its videos may be private.",
            code="channel_empty",
        )
    return channel, videos


# ---------------------------------------------------------------------------
# YouTube Data API listing (optional, richer — key already stored)
#
# Only used when a key is saved (Trending's SecretStore key or the
# YOUTUBE_API_KEY env var). It adds view counts and exact publish
# dates the flat playlist cannot give. Any failure — bad key, quota,
# unknown handle shape — falls back to the yt-dlp listing, so the key
# is never a requirement. ``/c/`` and ``/user/`` links have no direct
# API lookup (the API needs a channel id or an @handle), so those go
# straight to yt-dlp.
# ---------------------------------------------------------------------------

_YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def _api_get(api_key: str, path: str, params: dict) -> dict:
    """One YouTube Data API GET, parsed JSON (raises on any failure)."""
    query = urllib.parse.urlencode({"key": api_key, **params})
    req = urllib.request.Request(
        f"{_YOUTUBE_API_BASE}/{path}?{query}",
        headers={"User-Agent": "ClipForge/0.11.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=YOUTUBE_API_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        ValueError,
        OSError,
    ) as exc:
        raise ResolveError(
            f"YouTube Data API request failed: {exc}", code="youtube_api_failed"
        ) from exc


def _channel_ref_for_api(url: str) -> tuple[str, str] | None:
    """``(param, value)`` for channels.list, or None (no API lookup)."""
    parsed = urlparse(url.strip())
    segments = [s for s in (parsed.path or "").split("/") if s]
    if len(segments) >= 2 and segments[0] == "channel":
        return ("id", segments[1])
    if segments and segments[0].startswith("@"):
        return ("forHandle", segments[0])
    return None


def list_channel_videos_api(
    url: str, api_key: str, *, max_results: int = DEFAULT_MAX_VIDEOS
) -> tuple[dict, list[ResolvedVideo]] | None:
    """List a channel's videos via the YouTube Data API, or None.

    None means "this link has no API lookup" (``/c/`` / ``/user/``) —
    the caller falls back to yt-dlp. A :class:`ResolveError` means the
    API itself failed (bad key, quota, network) — also a fallback
    signal for the caller, never a user-facing failure on its own.
    """
    ref = _channel_ref_for_api(url)
    if ref is None:
        return None
    param, value = ref
    channels = _api_get(api_key, "channels", {"part": "snippet,contentDetails", param: value})
    items = channels.get("items")
    if not items:
        raise ResolveError(
            "That channel could not be found on YouTube — check the link.",
            code="channel_unavailable",
        )
    item = items[0]
    uploads = (item.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads:
        raise ResolveError(
            "This channel has no public uploads list to read.",
            code="channel_empty",
        )
    channel = {
        "id": str(item.get("id") or ""),
        "title": str((item.get("snippet") or {}).get("title") or ""),
        "url": url.strip(),
    }
    playlist = _api_get(
        api_key,
        "playlistItems",
        {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": str(max_results)},
    )
    videos: list[ResolvedVideo] = []
    for entry in playlist.get("items") or []:
        snippet = entry.get("snippet") or {}
        resource = snippet.get("resourceId") or {}
        video_id = str(resource.get("videoId") or "")
        if not video_id:
            continue
        thumbs = snippet.get("thumbnails") or {}
        thumb = (
            (thumbs.get("high") or {}).get("url")
            or (thumbs.get("medium") or {}).get("url")
            or (thumbs.get("default") or {}).get("url")
        )
        videos.append(
            ResolvedVideo(
                video_id=video_id,
                title=str(snippet.get("title") or ""),
                channel_title=str(snippet.get("channelTitle") or channel["title"]),
                channel_id=channel["id"],
                thumbnail_url=thumb,
                published_at=str(snippet.get("publishedAt") or ""),
            )
        )
    if not videos:
        raise ResolveError(
            "This channel has no public videos to pick from — it may "
            "be empty, or its videos may be private.",
            code="channel_empty",
        )
    return channel, videos


def resolve_url(
    url: str,
    yt_dlp: str | None,
    *,
    api_key: str | None = None,
    max_results: int = DEFAULT_MAX_VIDEOS,
) -> dict:
    """Resolve a pasted link into the one-box answer.

    ``{"kind": "video", "video": {...}}`` for a video link, or
    ``{"kind": "channel" | "playlist", "channel": {...}, "videos":
    [...], "source": "api" | "ytdlp"}`` for a channel / playlist link.
    Raises :class:`ResolveError` for anything unresolvable. ``yt_dlp``
    is None when yt-dlp is not installed — a video link then cannot
    be probed and a channel link cannot be listed, so the error says
    to install it from Settings instead of failing obscurely.
    """
    from clipforge.ytdlp_install import ytdlp_missing_message  # noqa: PLC0415 (avoid cycle)

    text = (url or "").strip()
    kind = classify_url(text)
    if kind == "unknown":
        raise ResolveError(
            "That doesn't look like a YouTube video or channel link — "
            "paste a youtube.com or youtu.be link.",
            code="unknown_url",
        )
    if yt_dlp is None:
        raise ResolveError(ytdlp_missing_message(), code="yt_dlp_missing")
    if kind == "video":
        return {"kind": "video", "video": resolve_video(text, yt_dlp).to_dict()}
    # Channel / playlist: richer API listing when a key is saved and
    # the link has an API lookup; any API failure falls back to the
    # yt-dlp flat listing, which needs no key at all.
    if api_key:
        try:
            api_result = list_channel_videos_api(text, api_key, max_results=max_results)
        except ResolveError:
            api_result = None
        if api_result is not None:
            channel, videos = api_result
            return {
                "kind": kind,
                "channel": channel,
                "videos": [v.to_dict() for v in videos],
                "source": "api",
            }
    channel, videos = list_channel_videos_ytdlp(text, yt_dlp, max_results=max_results)
    return {
        "kind": kind,
        "channel": channel,
        "videos": [v.to_dict() for v in videos],
        "source": "ytdlp",
    }


__all__ = [
    "DEFAULT_MAX_VIDEOS",
    "ResolveError",
    "ResolvedVideo",
    "classify_url",
    "list_channel_videos_api",
    "list_channel_videos_ytdlp",
    "resolve_url",
    "resolve_video",
]
