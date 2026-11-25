"""Channel SEO (v0.11.0, Worker B) — what works on a channel, in plain words.

The user pastes a channel link (or uses their connected YouTube channel)
and gets, without any API key:

* the channel's top videos, by views;
* what titles and words work there (common words, common title shapes);
* suggested titles + tags for their next clip, built from those winners.

How it reads a channel: ``yt-dlp --flat-playlist -J`` on the channel's
``/videos`` tab — metadata only, nothing is downloaded. Flat entries
usually carry ``view_count``; when YouTube does not give views for a
video it is listed last and the page says so plainly instead of
inventing a rank. When the user has a YouTube Data API key saved
(Trending's key, same store), ``videos.list`` enriches the same videos
with exact view counts and the channel's real tags — that path is
optional, never required.

Nothing here calls a platform to publish or to read private data; it
only reads public channel pages the same way the Trending matcher
(``clipforge.trending.match_own_video``) already does.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforge.ytdlp_install import probe_yt_dlp, ytdlp_missing_message

YOUTUBE_API_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CACHE_FILENAME = "channel_seo_cache.json"
CACHE_MAX_AGE_HOURS = 24
DEFAULT_LIMIT = 30

_CHANNEL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/"
    r"(?:@(?P<handle>[^/?#]+)"
    r"|channel/(?P<channel_id>UC[0-9A-Za-z_-]{22})"
    r"|c/(?P<custom>[^/?#]+)"
    r"|user/(?P<user>[^/?#]+))"
    r"(?:/(?:videos|shorts|streams|playlists|about|featured)?)?/?$"
)
_HANDLE_RE = re.compile(r"^@[\w.-]+$")
_CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_VIDEO_URL_HINT_RE = re.compile(r"watch\?|/shorts/|/live/|youtu\.be|/playlist\?|/embed/")

_STOPWORDS = set(
    """a an and are as at be by for from has have in is it its of on or that
    the this to was will with you your he she they we our their his her not
    no do does did can could should would then than so such very just really
    how why what when where which who whom get gets got make makes made take
    takes took see saw know knew think thought say says said go goes went come
    came way ways one two three new best top vs versus part episode""".split()
)


class ChannelSeoError(RuntimeError):
    """A channel could not be read — message is plain language for the UI."""


def normalize_channel_url(raw: str) -> str:
    """Turn whatever the user pasted into a canonical YouTube channel URL.

    Accepts a full channel link (``@handle``, ``/channel/UC…``,
    ``/c/name``, ``/user/name``, with or without a ``/videos`` tail), a
    bare ``@handle``, or a bare ``UC…`` channel id. A video link
    (watch / Shorts / youtu.be) is refused with a plain message — the
    page needs the *channel*, not one video.
    """
    text = (raw or "").strip()
    if not text:
        raise ChannelSeoError("Paste a YouTube channel link first.")
    if _VIDEO_URL_HINT_RE.search(text):
        raise ChannelSeoError(
            "That looks like a video link, not a channel link — paste the "
            "channel link instead (the one with @name or /channel/ in it)."
        )
    if _HANDLE_RE.match(text):
        return f"https://www.youtube.com/{text}"
    if _CHANNEL_ID_RE.match(text):
        return f"https://www.youtube.com/channel/{text}"
    match = _CHANNEL_RE.match(text)
    if not match:
        raise ChannelSeoError(
            "That does not look like a YouTube channel link. Paste the "
            "channel link from YouTube — it has @name or /channel/ in it."
        )
    if match.group("handle"):
        return f"https://www.youtube.com/@{match.group('handle')}"
    if match.group("channel_id"):
        return f"https://www.youtube.com/channel/{match.group('channel_id')}"
    if match.group("custom"):
        return f"https://www.youtube.com/c/{match.group('custom')}"
    return f"https://www.youtube.com/user/{match.group('user')}"


def _ytdlp_command() -> list[str] | None:
    """yt-dlp to run: a binary that actually runs, else ``python -m yt_dlp``.

    The resolved path is *probed* (``--version`` must run), not just
    checked for existence: a shim whose shebang points at a Python
    without yt-dlp installed exists but does not run, and must not
    shadow the working ``python -m yt_dlp`` fallback.
    """
    probe = probe_yt_dlp()
    if probe.get("found") and probe.get("path"):
        return [probe["path"]]
    try:
        import yt_dlp  # noqa: F401  (presence check only)

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def fetch_channel(
    channel_url: str,
    limit: int = DEFAULT_LIMIT,
    timeout: int = 120,
    _run: Callable[..., Any] | None = None,
    _ytdlp: list[str] | None = None,
) -> dict:
    """List a channel's videos with yt-dlp (flat playlist, no download).

    Returns ``{channel_id, channel_name, channel_url, subscriber_count,
    videos: [{video_id, title, url, views, duration_s, published_at}]}``.
    ``views`` is ``None`` when YouTube did not give a count for that
    video. Raises :class:`ChannelSeoError` in plain language.
    """
    url = normalize_channel_url(channel_url)
    command = _ytdlp if _ytdlp is not None else _ytdlp_command()
    if not command:
        raise ChannelSeoError(
            ytdlp_missing_message() + " Install yt-dlp from Settings, then try again."
        )
    run = _run or subprocess.run
    try:
        proc = run(
            command
            + [
                "-J",
                "--flat-playlist",
                "--playlist-end",
                str(max(1, limit)),
                "--no-warnings",
                f"{url}/videos",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChannelSeoError(
            f"Could not read the channel ({exc}). Check the link and try again."
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise ChannelSeoError(
            "Could not read the channel — "
            f"{detail[-1] if detail else 'yt-dlp failed'}. "
            "Check the channel link and try again."
        )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ChannelSeoError(
            "YouTube answered in a way ClipForge could not read. Try again."
        ) from exc
    entries = data.get("entries") or []
    videos = [_video_from_entry(e) for e in entries if isinstance(e, dict)]
    videos = [v for v in videos if v["video_id"] and v["title"]]
    if not videos:
        raise ChannelSeoError(
            "The channel has no videos ClipForge could read. Check the link — "
            "a private or empty channel has nothing to learn from."
        )
    return {
        "channel_id": data.get("channel_id") or data.get("id") or "",
        "channel_name": data.get("uploader") or data.get("channel") or data.get("title") or "",
        "channel_url": url,
        "subscriber_count": data.get("subscriber_count"),
        "videos": videos,
    }


def _video_from_entry(entry: dict) -> dict:
    published = None
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        published = datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat()
    views = entry.get("view_count")
    return {
        "video_id": str(entry.get("id") or ""),
        "title": (entry.get("title") or "").strip(),
        "url": entry.get("url")
        or (f"https://www.youtube.com/watch?v={entry.get('id')}" if entry.get("id") else ""),
        "views": int(views) if isinstance(views, (int, float)) else None,
        "duration_s": entry.get("duration"),
        "published_at": published,
    }


def enrich_with_youtube_api(
    videos: list[dict],
    api_key: str,
    _http_get: Callable[..., Any] | None = None,
) -> tuple[list[dict], list[str]]:
    """Add exact views + real tags via YouTube Data API ``videos.list``.

    Optional enrichment only: a failure raises :class:`ChannelSeoError`
    and the caller keeps the yt-dlp numbers instead. Returns
    ``(videos, top_tags)`` — ``top_tags`` are the tags the channel's own
    top videos actually use, most frequent first.
    """
    import httpx

    ids = [v["video_id"] for v in videos if v.get("video_id")][:50]
    if not ids or not api_key:
        return videos, []
    get = _http_get or httpx.get
    try:
        resp = get(
            YOUTUBE_API_VIDEOS_URL,
            params={
                "part": "statistics,snippet",
                "id": ",".join(ids),
                "key": api_key,
                "maxResults": 50,
            },
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 (network errors are all "try again")
        raise ChannelSeoError(f"Could not reach the YouTube API ({exc}).") from exc
    if resp.status_code in (400, 403):
        raise ChannelSeoError(
            "The YouTube API key did not work (YouTube said no). The "
            "channel view below still uses the public page numbers."
        )
    if resp.status_code != 200:
        raise ChannelSeoError(
            f"The YouTube API answered {resp.status_code}. The channel view "
            "below still uses the public page numbers."
        )
    by_id: dict[str, dict] = {}
    tag_counts: Counter[str] = Counter()
    for item in (resp.json() or {}).get("items", []):
        stats = item.get("statistics") or {}
        snippet = item.get("snippet") or {}
        views = stats.get("viewCount")
        by_id[str(item.get("id"))] = {
            "views": int(views) if views is not None else None,
            "tags": [str(t) for t in (snippet.get("tags") or []) if t],
        }
        for tag in by_id[str(item.get("id"))]["tags"]:
            tag_counts[tag.lower()] += 1
    enriched: list[dict] = []
    for video in videos:
        extra = by_id.get(video["video_id"])
        merged = dict(video)
        if extra:
            if extra["views"] is not None:
                merged["views"] = extra["views"]
            merged["tags"] = extra["tags"]
        enriched.append(merged)
    return enriched, [tag for tag, _ in tag_counts.most_common(15)]


def _title_words(titles: list[str]) -> list[dict]:
    """Most common meaningful words across titles, with counts."""
    counts: Counter[str] = Counter()
    for title in titles:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9']{2,19}", title.lower()):
            if word not in _STOPWORDS:
                counts[word] += 1
    return [{"word": w, "count": n} for w, n in counts.most_common(10)]


def _title_patterns(top_titles: list[str]) -> dict:
    """Common title shapes in the channel's top videos (counts + examples)."""
    n = max(1, len(top_titles))
    lowered = [t.lower() for t in top_titles]
    starts_how = sum(1 for t in lowered if t.startswith("how to"))
    starts_why = sum(1 for t in lowered if t.startswith("why"))
    starts_number = sum(1 for t in top_titles if re.match(r"^\s*\d+", t))
    has_number = sum(1 for t in top_titles if re.search(r"\d", t))
    has_vs = sum(1 for t in lowered if " vs " in f" {t} " or " vs. " in t)
    has_question = sum(1 for t in top_titles if "?" in t)
    avg_len = round(sum(len(t) for t in top_titles) / n) if top_titles else 0
    return {
        "videos_counted": len(top_titles),
        "avg_title_chars": avg_len,
        "starts_with_how_to": starts_how,
        "starts_with_why": starts_why,
        "starts_with_number": starts_number,
        "contains_number": has_number,
        "contains_vs": has_vs,
        "contains_question": has_question,
    }


def _suggest_titles(topic: str, patterns: dict, top_words: list[dict]) -> list[dict]:
    """Three title ideas for the next clip, from the channel's own shapes.

    Each idea says *why* it was suggested — either “N of the channel's
    top videos do this” or “general”. ``topic`` is the user's words for
    the next clip; with no topic the channel's most common title word
    stands in, and the idea says so.
    """
    topic = (topic or "").strip()
    fallback_word = top_words[0]["word"] if top_words else "your topic"
    subject = topic or fallback_word
    subject_note = (
        ""
        if topic
        else f" (using “{fallback_word}”, the word this channel uses most — "
        "type your own topic to replace it)"
    )
    counted = max(1, patterns["videos_counted"])
    shapes: list[tuple[str, str]] = []
    if patterns["starts_with_how_to"]:
        shapes.append(
            (
                f"How to {subject}",
                f"{patterns['starts_with_how_to']} of the top {counted} start with “How to”.",
            )
        )
    if patterns["contains_number"]:
        shapes.append(
            (
                f"{subject}: 3 mistakes to avoid",
                f"{patterns['contains_number']} of the top {counted} have a number in the title.",
            )
        )
    if patterns["starts_with_why"]:
        shapes.append(
            (
                f"Why {subject} works",
                f"{patterns['starts_with_why']} of the top {counted} videos start with “Why”.",
            )
        )
    shapes.append(
        (
            f"{subject} in 60 seconds",
            "General — short, one-promise titles do well as Shorts.",
        )
    )
    shapes.append(
        (
            f"{subject} — what actually works",
            "General — a plain promise the first seconds of the clip must deliver on.",
        )
    )
    if patterns["contains_vs"]:
        shapes.append(
            (
                f"{subject} vs the old way",
                f"{patterns['contains_vs']} of the top {counted} videos use a “vs” comparison.",
            )
        )
    seen: set[str] = set()
    suggestions: list[dict] = []
    for title, why in shapes:
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({"title": title, "why": why + subject_note})
        if len(suggestions) == 3:
            break
    return suggestions


def analyze_channel(data: dict, topic: str = "", api_tags: list[str] | None = None) -> dict:
    """Turn fetched channel videos into the Channel SEO view model."""
    videos = list(data.get("videos") or [])
    with_views = [v for v in videos if isinstance(v.get("views"), int)]
    without_views = [v for v in videos if not isinstance(v.get("views"), int)]
    top = sorted(with_views, key=lambda v: v["views"], reverse=True) + without_views
    top_titles = [v["title"] for v in top[:10]]
    words = _title_words(top_titles)
    patterns = _title_patterns(top_titles)
    topic_words = [
        w
        for w in re.findall(r"[A-Za-z][A-Za-z0-9']{2,19}", (topic or "").lower())
        if w not in _STOPWORDS
    ]
    suggested_tags: list[str] = []
    for word in [w["word"] for w in words[:8]] + topic_words:
        if word and word not in suggested_tags:
            suggested_tags.append(word)
    if api_tags:
        for tag in api_tags:
            if tag.lower() not in [t.lower() for t in suggested_tags]:
                suggested_tags.append(tag)
    suggested_tags = suggested_tags[:15]
    views_source = "yt-dlp"
    if with_views and len(with_views) == len(videos):
        views_note = f"Views from the public channel page for all {len(videos)} videos."
    elif with_views:
        views_note = (
            f"Views from the public channel page for {len(with_views)} of "
            f"{len(videos)} videos — {len(without_views)} had no view count "
            "shown, so they are listed last."
        )
    else:
        views_note = (
            "YouTube did not show view counts on this page, so the videos "
            "are in channel order, not by views."
        )
        views_source = "unavailable"
    return {
        "channel_id": data.get("channel_id") or "",
        "channel_name": data.get("channel_name") or "",
        "channel_url": data.get("channel_url") or "",
        "subscriber_count": data.get("subscriber_count"),
        "videos_fetched": len(videos),
        "views_source": views_source,
        "views_note": views_note,
        "top_videos": top[:10],
        "what_works": {
            "common_words": words,
            "title_patterns": patterns,
            "basis": f"From the channel's top {min(10, len(top))} videos by views.",
        },
        "suggested_titles": _suggest_titles(topic, patterns, words),
        "suggested_tags": suggested_tags,
        "suggested_tags_basis": (
            "Words from the channel's top titles"
            + (" + the channel's own YouTube tags" if api_tags else "")
            + (f" + your topic “{topic.strip()}”" if (topic or "").strip() else "")
            + "."
        ),
    }


# ------------------------------------------------------------------ cache
# One small file, last analysis per channel id, 24 h TTL — same pattern
# as the Growth snapshots: the page loads instantly on revisit and only
# re-reads YouTube when the user asks for a fresh look.


def _cache_file(data_dir: str | Path) -> Path:
    return Path(data_dir) / CACHE_FILENAME


def load_cached(data_dir: str | Path, channel_id: str) -> dict | None:
    """Fresh cached analysis for a channel id, else None."""
    try:
        cache = json.loads(_cache_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = (cache or {}).get(channel_id or "")
    if not isinstance(entry, dict):
        return None
    try:
        fetched = datetime.fromisoformat(entry.get("fetched_at") or "")
    except ValueError:
        return None
    age_hours = (datetime.now(tz=UTC) - fetched).total_seconds() / 3600
    if age_hours > CACHE_MAX_AGE_HOURS:
        return None
    analysis = entry.get("analysis") or {}
    analysis["cached"] = True
    analysis["fetched_at"] = entry.get("fetched_at")
    return analysis


def save_cached(data_dir: str | Path, channel_id: str, analysis: dict) -> None:
    """Best-effort cache write — a failure here never fails the request."""
    try:
        path = _cache_file(data_dir)
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
        cache[channel_id or ""] = {
            "fetched_at": datetime.now(tz=UTC).isoformat(),
            "analysis": analysis,
        }
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def analyze_live(
    channel_url: str,
    topic: str = "",
    limit: int = DEFAULT_LIMIT,
    data_dir: str | Path | None = None,
    youtube_api_key: str | None = None,
    use_cache: bool = True,
) -> dict:
    """Full flow: read the channel, optionally enrich, analyse, cache.

    Returns the analysis with ``cached`` / ``fetched_at`` set. The
    YouTube API step is best-effort: when it fails the yt-dlp numbers
    stand and ``api_enrichment`` says why it was skipped.
    """
    url = normalize_channel_url(channel_url)
    data = fetch_channel(url, limit=limit)
    api_tags: list[str] = []
    api_note: str | None = None
    api_used = False
    if youtube_api_key:
        try:
            data["videos"], api_tags = enrich_with_youtube_api(data["videos"], youtube_api_key)
            api_used = True
        except ChannelSeoError as exc:
            api_note = str(exc)
    analysis = analyze_channel(data, topic=topic, api_tags=api_tags or None)
    if api_used:
        analysis["views_source"] = "youtube_api"
        analysis["views_note"] = "Exact views from the YouTube API."
    analysis["api_enrichment"] = {
        "used": api_used,
        "note": api_note
        or (
            "Exact views + real tags from the YouTube API."
            if api_used
            else "No YouTube API key saved — using the public page numbers."
        ),
    }
    analysis["cached"] = False
    analysis["fetched_at"] = datetime.now(tz=UTC).isoformat()
    if use_cache and data_dir is not None and data.get("channel_id"):
        save_cached(data_dir, data["channel_id"], analysis)
    return analysis


__all__ = [
    "CACHE_MAX_AGE_HOURS",
    "ChannelSeoError",
    "analyze_channel",
    "analyze_live",
    "enrich_with_youtube_api",
    "fetch_channel",
    "load_cached",
    "normalize_channel_url",
    "save_cached",
]
