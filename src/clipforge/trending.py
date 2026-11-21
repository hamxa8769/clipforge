"""Live niche trending search + trending-to-agent loop (v0.6.0, Worker B).

Two pieces, kept in one module so the router and the agent share them:

1. **Live trending search** — the server searches YouTube for videos that
   are trending *in the user's niche right now*, via YouTube Data API v3:

   * ``search.list`` (``q`` = niche keywords, ``type=video``,
     ``order=viewCount``, ``publishedAfter`` = 7 days ago) to find recent
     videos with the most views in the niche;
   * ``videos.list`` (``part=snippet,statistics``) for the same ids to
     get views / likes / comments / published date.

   Credentials, in order: the connected YouTube OAuth access token
   (refreshed when expired, exactly like the channels code does), else
   a saved YouTube Data API key (OS keyring / encrypted file, same
   SecretStore as every other credential, or the ``YOUTUBE_API_KEY``
   env var). When neither exists the search refuses with a plain
   message saying exactly what to connect — it never invents videos.

2. **Trending -> agent loop** — a trending video is a *topic*, never a
   clip source. The user picks a trending video, the agent matches it
   to the user's OWN source video on that topic, and the normal agent
   pipeline (transcribe -> score -> clip -> SEO -> approval queue ->
   schedule / publish) runs with its SEO tuned to the trending topic.

Rights guardrail (non-negotiable): the agent clips ONLY the user's own
videos or videos they have rights / a licence to use. It must NOT
download and re-upload other creators' videos. :func:`is_trending_source`
refuses a trending video URL as a clip source, and the router + agent
both enforce it.

Creative Commons discovery (v0.7.0, Worker A): ``search_trending``
takes ``licensed_only=True`` (``videoLicense=creativeCommon`` + a
``status.license`` verification pass), and :func:`check_video_license`
answers whether one video may be clipped — CC or the user's own video
only, never a standard-licence third-party video, never a guess.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from clipforge.auth.secrets import SecretStore, mask_secret
from clipforge.auth.settings_store import SettingsStore
from clipforge.auth.token_store import TokenRecord, TokenStore
from clipforge.niches import niche_keywords, niche_label, normalize_niche

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
# SecretStore name for the saved YouTube Data API key (never logged,
# never returned — only the masked view leaves the server).
YOUTUBE_API_KEY_SECRET = "app:youtube:api_key"
YOUTUBE_API_KEY_ENV = "YOUTUBE_API_KEY"

# How far back the trending search looks: videos published in the last
# 7 days, ordered by view count. Older videos are not "trending now".
TRENDING_WINDOW_DAYS = 7
# search.list costs 100 quota units per call; videos.list costs 1.
# 25 ids per search keeps the search cheap while giving the scorer
# enough candidates to rank.
SEARCH_MAX_RESULTS = 25

TRENDING_NOTICE = (
    "Trending shows what topics are working — your clips are made from "
    "YOUR videos. The agent never downloads or re-uploads another "
    "creator's video."
)

RIGHTS_NOTICE = (
    "The ClipForge Agent only works with videos you own or have the "
    "rights / licence to use. It is not a tool to re-upload other "
    "creators' videos."
)

# Creative Commons discovery (v0.7.0, Worker A). YouTube's
# ``creativeCommon`` licence is CC BY 3.0 — the licence page below is
# what the API reports for such videos. The only other licence value
# the YouTube Data API returns is ``youtube`` (the standard licence).
CC_LICENSE = "creativeCommon"
STANDARD_LICENSE = "youtube"
CC_LICENSE_URL = "https://creativecommons.org/licenses/by/3.0/"

# Plain-language refusal for a standard-licence third-party video.
# Shown verbatim by the licence-check endpoint and the /run guardrail.
STANDARD_LICENSE_REASON = (
    "This video uses YouTube's standard licence — it cannot be "
    "re-uploaded. Only Creative Commons, public-domain, or your own "
    "videos can be clipped."
)

UNVERIFIABLE_LICENSE_REASON = (
    "Could not verify this video's licence — YouTube did not report "
    "one, so ClipForge cannot confirm it is safe to clip. Only "
    "Creative Commons, public-domain, or your own videos can be clipped."
)


def license_url_for(license_value: str | None) -> str:
    """The licence page for a YouTube licence value ("" when there is
    none — a standard-licence video has no licence page)."""
    return CC_LICENSE_URL if license_value == CC_LICENSE else ""


_URL_RE = re.compile(r"^https?://\S+$")
_YT_VIDEO_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|shorts/|embed/|live/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "how",
        "why",
        "what",
        "this",
        "that",
        "it",
        "its",
        "you",
        "your",
        "my",
        "we",
        "best",
        "top",
        "new",
        "2026",
        "2025",
        "video",
        "watch",
        "official",
        "full",
        "part",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")


class TrendingError(ValueError):
    """Plain-language trending failure — safe to show to the user as-is."""


# ------------------------------------------------------------------ trend score
def trend_score(
    view_count: int,
    like_count: int,
    comment_count: int,
    published_at: datetime,
    *,
    now: datetime | None = None,
) -> float:
    """Trend score, 0–100. Documented formula — no hidden weighting:

    * ``velocity`` = views per hour since publish (age floored at 1h, so
      a video published minutes ago is not divided by ~0).
    * ``engagement`` = (likes + comments) / views.
    * ``v_norm`` = log10(1 + velocity) / log10(1 + 100_000): 100k
      views/hour saturates the velocity term (log scale, so a video
      with 10x the views does not score 10x).
    * ``e_norm`` = min(1, engagement / 0.05): a 5% like+comment rate
      saturates the engagement term.
    * ``score`` = 100 * (0.65 * v_norm + 0.35 * e_norm).

    Velocity dominates (a trend is about speed), engagement breaks
    ties between videos with similar velocity.
    """
    moment = now or datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    age_hours = max(1.0, (moment - published_at).total_seconds() / 3600.0)
    velocity = max(0, view_count) / age_hours
    engagement = (max(0, like_count) + max(0, comment_count)) / max(1, view_count)
    import math

    v_norm = min(1.0, math.log10(1 + velocity) / math.log10(1 + 100_000))
    e_norm = min(1.0, engagement / 0.05)
    return round(100.0 * (0.65 * v_norm + 0.35 * e_norm), 1)


# ------------------------------------------------------------------ video ids
def extract_youtube_video_id(url: str | None) -> str | None:
    """The 11-char video id of a YouTube watch / Shorts / youtu.be URL."""
    if not url:
        return None
    match = _YT_VIDEO_RE.search(url)
    if match:
        return match.group(1)
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    values = query.get("v") or []
    return values[0] if values and len(values[0]) == 11 else None


def is_trending_source(source_url: str | None, trending_video_id: str | None) -> bool:
    """True when ``source_url`` IS the trending video.

    The guardrail: a trending video URL may never be used as a clip
    source. The user's own video on the same topic is a different URL
    (different video id) — that is the only accepted ingest input.
    """
    if not source_url or not trending_video_id:
        return False
    return extract_youtube_video_id(source_url) == trending_video_id


def refuse_trending_source(source_url: str | None, trending_video_id: str | None) -> None:
    """Raise :class:`TrendingError` when the source is the trending video."""
    if is_trending_source(source_url, trending_video_id):
        raise TrendingError(
            "That is the trending video itself — the agent cannot clip "
            "another creator's video. Pick YOUR OWN video on this topic "
            "as the source instead. " + TRENDING_NOTICE
        )


# ------------------------------------------------------------------ API key
def load_youtube_api_key(secrets: SecretStore) -> str | None:
    """Saved YouTube Data API key, else the YOUTUBE_API_KEY env var."""
    stored = secrets.get(YOUTUBE_API_KEY_SECRET)
    if stored:
        return stored
    return os.environ.get(YOUTUBE_API_KEY_ENV) or None


def save_youtube_api_key(secrets: SecretStore, api_key: str | None) -> None:
    """Save (or clear, with \"\" / None) the YouTube Data API key."""
    if api_key and api_key.strip():
        secrets.set(YOUTUBE_API_KEY_SECRET, api_key.strip())
    else:
        secrets.delete(YOUTUBE_API_KEY_SECRET)


def youtube_api_key_view(secrets: SecretStore) -> dict:
    """Masked view of the saved key (configured + last4 only)."""
    value = load_youtube_api_key(secrets)
    source = "stored" if secrets.get(YOUTUBE_API_KEY_SECRET) else ("env" if value else "none")
    return {
        "configured": bool(value),
        "last4": value[-4:] if value else "",
        "masked": mask_secret(value),
        "source": source,
    }


# ------------------------------------------------------------------ credentials
@dataclass
class TrendingCredentials:
    """How the next YouTube Data API call authenticates."""

    kind: str  # "oauth" | "api_key"
    token: str
    account_name: str = ""

    def auth_params(self) -> dict:
        """Extra query params for this credential kind (API key only)."""
        return {"key": self.token} if self.kind == "api_key" else {}

    def auth_headers(self) -> dict:
        """Extra headers for this credential kind (OAuth only)."""
        return {"Authorization": f"Bearer {self.token}"} if self.kind == "oauth" else {}


def resolve_credentials(
    secrets: SecretStore,
    settings_store: SettingsStore | None = None,
    *,
    client: httpx.Client | None = None,
) -> TrendingCredentials:
    """YouTube credentials for trending search: OAuth first, API key next.

    OAuth: the connected channel's access token; when expired and a
    refresh token + app credentials exist, it is refreshed in place
    (same flow as the channels refresh endpoint). API key: the saved
    key (or YOUTUBE_API_KEY). Raises :class:`TrendingError` with the
    exact next step when neither exists.
    """
    record = TokenStore(secrets).load("youtube")
    if record is not None:
        if record.is_expired() and record.refresh_token:
            refreshed = _try_refresh(record, secrets, settings_store, client=client)
            if refreshed is not None:
                record = refreshed
        if not record.is_expired():
            return TrendingCredentials(
                kind="oauth", token=record.access_token, account_name=record.account_name
            )
    api_key = load_youtube_api_key(secrets)
    if api_key:
        return TrendingCredentials(kind="api_key", token=api_key)
    if record is not None:
        raise TrendingError(
            "Your YouTube connection has expired and could not be "
            "refreshed — reconnect the channel (Channels > YouTube), or "
            "save a YouTube Data API key on the Trending page instead."
        )
    raise TrendingError(
        "No YouTube connection yet — connect your YouTube channel "
        "(Channels > Connect YouTube) or save a YouTube Data API key on "
        "the Trending page. Trending search needs one of the two; it "
        "never shows invented videos."
    )


def _try_refresh(
    record: TokenRecord,
    secrets: SecretStore,
    settings_store: SettingsStore | None,
    *,
    client: httpx.Client | None = None,
) -> TokenRecord | None:
    """Refresh an expired YouTube token in place. None when not possible."""
    store = settings_store or SettingsStore(Path("."))
    client_id = store.get_credential("youtube", "client_id")
    client_secret = store.get_credential("youtube", "client_secret")
    if not client_id or not client_secret or not record.refresh_token:
        return None
    try:
        from clipforge.auth.oauth import refresh_access_token  # noqa: PLC0415

        response = refresh_access_token(
            "youtube",
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=record.refresh_token,
            client=client,
        )
    except Exception:  # noqa: BLE001 — refresh failure falls through to API key
        return None
    try:
        refreshed = TokenRecord.from_token_response("youtube", response)
    except (KeyError, TypeError, ValueError):
        return None
    refreshed.refresh_token = response.get("refresh_token") or record.refresh_token
    refreshed.account_name = record.account_name
    refreshed.account_ref = record.account_ref
    TokenStore(secrets).save(refreshed)
    return refreshed


def credentials_status(secrets: SecretStore) -> dict:
    """What the Trending page shows about credentials (masked, honest)."""
    record = TokenStore(secrets).load("youtube")
    key_view = youtube_api_key_view(secrets)
    oauth = {
        "connected": record is not None and not record.is_expired(),
        "expired": bool(record is not None and record.is_expired()),
        "account_name": (record.account_name if record else "") or "",
        "can_refresh": bool(record and record.refresh_token),
    }
    usable = bool(oauth["connected"] or key_view["configured"])
    if usable:
        message = (
            f"Trending search will use your connected YouTube channel"
            f"{' (' + oauth['account_name'] + ')' if oauth['account_name'] else ''}."
            if oauth["connected"]
            else "Trending search will use your saved YouTube Data API key."
        )
    else:
        message = (
            "Trending search is not set up yet — connect your YouTube "
            "channel (Channels > Connect YouTube) or save a YouTube Data "
            "API key below."
        )
    return {"oauth": oauth, "api_key": key_view, "usable": usable, "message": message}


# ------------------------------------------------------------------ search
def niche_search_query(niche: str) -> str:
    """The ``q`` for search.list: preset label + top keywords, else the
    custom niche's own words."""
    name = normalize_niche(niche)
    label = niche_label(niche)
    keywords = niche_keywords(niche)[:3]
    parts = [label] + [k for k in keywords if k.lower() != label.lower()]
    return " ".join(parts) if parts else name


@dataclass
class TrendingVideo:
    """One trending video in a niche (from the live YouTube API)."""

    video_id: str
    title: str
    channel_title: str
    channel_id: str = ""
    published_at: str = ""
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    trend_score: float = 0.0
    # Licence (v0.7.0): only populated when the search verified it
    # (licensed_only=True) — "" means "not checked", never a guess.
    license: str = ""
    license_url: str = ""

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self) -> dict:
        return {**asdict(self), "url": self.url}


def _parse_published(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _youtube_error_message(payload: dict, status_code: int) -> str:
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        detail = (error.get("message") or "").strip()
    if detail:
        return f"YouTube API error ({status_code}): {detail}"
    return f"YouTube API returned HTTP {status_code}."


def search_trending(
    niche: str,
    *,
    credentials: TrendingCredentials,
    max_results: int = 10,
    licensed_only: bool = False,
    client: httpx.Client | None = None,
) -> list[TrendingVideo]:
    """Live trending search for a niche. Two YouTube Data API v3 calls:

    1. ``search.list`` — recent videos in the niche, most views first.
    2. ``videos.list`` — statistics for those ids (views / likes /
       comments / published date).

    With ``licensed_only=True`` (v0.7.0, Creative Commons discovery)
    the search also passes ``videoLicense=creativeCommon`` to
    search.list, asks videos.list for the ``status`` part, and VERIFIES
    each result: only videos whose ``status.license`` is
    ``creativeCommon`` are kept, each carrying ``license`` +
    ``license_url``. Anything else is dropped — the search never
    claims a video is CC-licensed on the search filter alone.
    With ``licensed_only=False`` (the default) the search behaves
    exactly as in v0.6.0: all trending videos, no licence filtering.

    Results are ranked by :func:`trend_score` (velocity + engagement),
    highest first. Raises :class:`TrendingError` (plain language) when
    YouTube rejects the call — the caller turns that into the error
    state, never into invented videos.
    """
    own_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        published_after = (datetime.now(UTC) - timedelta(days=TRENDING_WINDOW_DAYS)).isoformat()
        search_params: dict = {
            "part": "snippet",
            "q": niche_search_query(niche),
            "type": "video",
            "order": "viewCount",
            "publishedAfter": published_after,
            "maxResults": min(max(1, max_results), SEARCH_MAX_RESULTS),
            **credentials.auth_params(),
        }
        if licensed_only:
            search_params["videoLicense"] = CC_LICENSE
        search_resp = http.get(
            f"{YOUTUBE_API_BASE}/search",
            params=search_params,
            headers=credentials.auth_headers(),
        )
        if search_resp.status_code != 200:
            raise TrendingError(
                _youtube_error_message(_safe_json(search_resp), search_resp.status_code)
            )
        items = _safe_json(search_resp).get("items") or []
        ids = [
            (item.get("id") or {}).get("videoId")
            for item in items
            if isinstance(item, dict) and (item.get("id") or {}).get("videoId")
        ]
        if not ids:
            return []
        stats_resp = http.get(
            f"{YOUTUBE_API_BASE}/videos",
            params={
                "part": "snippet,statistics,status" if licensed_only else "snippet,statistics",
                "id": ",".join(ids),
                **credentials.auth_params(),
            },
            headers=credentials.auth_headers(),
        )
        if stats_resp.status_code != 200:
            raise TrendingError(
                _youtube_error_message(_safe_json(stats_resp), stats_resp.status_code)
            )
        now = datetime.now(UTC)
        videos: list[TrendingVideo] = []
        for item in _safe_json(stats_resp).get("items") or []:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            status = item.get("status") or {}
            # Licence verification (licensed_only): the search filter
            # alone is not proof — drop anything YouTube does not
            # confirm as Creative Commons.
            license_value = str(status.get("license") or "")
            if licensed_only and license_value != CC_LICENSE:
                continue
            published = _parse_published(snippet.get("publishedAt"))
            video = TrendingVideo(
                video_id=str(item.get("id") or ""),
                title=str(snippet.get("title") or ""),
                channel_title=str(snippet.get("channelTitle") or ""),
                channel_id=str(snippet.get("channelId") or ""),
                published_at=published.isoformat(),
                view_count=int(stats.get("viewCount") or 0),
                like_count=int(stats.get("likeCount") or 0),
                comment_count=int(stats.get("commentCount") or 0),
                license=license_value if licensed_only else "",
                license_url=license_url_for(license_value) if licensed_only else "",
            )
            video.trend_score = trend_score(
                video.view_count, video.like_count, video.comment_count, published, now=now
            )
            videos.append(video)
        videos.sort(key=lambda v: v.trend_score, reverse=True)
        return videos
    except TrendingError:
        raise
    except httpx.HTTPError as exc:
        raise TrendingError(
            "Could not reach the YouTube API — check your internet "
            f"connection and try again. ({exc})"
        ) from exc
    finally:
        if own_client:
            http.close()


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ licence check
def own_channel_id(secrets: SecretStore) -> str:
    """The connected YouTube channel's id (``account_ref`` from the
    OAuth token, set from channels.list ``mine=true`` at connect
    time). "" when no channel is connected — ownership then cannot be
    established and the caller must say so honestly."""
    record = TokenStore(secrets).load("youtube")
    return (record.account_ref or "") if record is not None else ""


def check_video_license(
    url: str,
    *,
    credentials: TrendingCredentials,
    own_channel_id: str = "",
    client: httpx.Client | None = None,
) -> dict:
    """Check whether one YouTube video may be clipped, live from the
    YouTube Data API (``videos.list`` ``part=status,snippet,
    contentDetails``).

    Returns ``{video_id, title, channel, channel_id, license,
    license_url, can_clip, reason}``. ``can_clip`` is True ONLY when
    the video is Creative Commons licensed OR it is the user's own
    video (its channel id matches ``own_channel_id``). A standard
    licence (``youtube``) is refused with
    :data:`STANDARD_LICENSE_REASON`. A video whose licence cannot be
    verified at all (no ``status.license`` in the response) is also
    refused — the licence is never guessed or faked.

    Raises :class:`TrendingError` (plain language) when the URL is not
    a YouTube video URL, YouTube has no such video, or the API call
    fails — the caller turns that into the honest error state.
    """
    video_id = extract_youtube_video_id(url)
    if not video_id:
        raise TrendingError(
            "That doesn't look like a YouTube video URL — paste a "
            "youtube.com/watch, youtu.be, or Shorts link."
        )
    own_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(
            f"{YOUTUBE_API_BASE}/videos",
            params={
                "part": "status,snippet,contentDetails",
                "id": video_id,
                **credentials.auth_params(),
            },
            headers=credentials.auth_headers(),
        )
        if resp.status_code != 200:
            raise TrendingError(_youtube_error_message(_safe_json(resp), resp.status_code))
        items = _safe_json(resp).get("items") or []
        if not items or not isinstance(items[0], dict):
            raise TrendingError(
                "YouTube could not find that video — it may be private, "
                "deleted, or the link is wrong. Check the URL and try again."
            )
        item = items[0]
        snippet = item.get("snippet") or {}
        status = item.get("status") or {}
        license_value = str(status.get("license") or "")
        channel_id = str(snippet.get("channelId") or "")
        is_own = bool(own_channel_id) and channel_id == own_channel_id
        if license_value == CC_LICENSE:
            can_clip, reason = (
                True,
                (
                    "This video is licensed under Creative Commons (CC BY) — "
                    "it can be clipped and re-uploaded with attribution to "
                    "the original creator."
                ),
            )
        elif is_own:
            can_clip, reason = True, ("This is your own video — you can clip it.")
        elif license_value == STANDARD_LICENSE:
            can_clip, reason = False, STANDARD_LICENSE_REASON
        elif not license_value:
            can_clip, reason = False, UNVERIFIABLE_LICENSE_REASON
        else:
            can_clip, reason = (
                False,
                (
                    f"This video's licence ({license_value}) is not one "
                    "ClipForge can clip. Only Creative Commons, "
                    "public-domain, or your own videos can be clipped."
                ),
            )
        return {
            "video_id": video_id,
            "title": str(snippet.get("title") or ""),
            "channel": str(snippet.get("channelTitle") or ""),
            "channel_id": channel_id,
            "license": license_value,
            "license_url": license_url_for(license_value),
            "can_clip": can_clip,
            "reason": reason,
        }
    except TrendingError:
        raise
    except httpx.HTTPError as exc:
        raise TrendingError(
            "Could not reach the YouTube API — check your internet "
            f"connection and try again. ({exc})"
        ) from exc
    finally:
        if own_client:
            http.close()


# ------------------------------------------------------------------ topic match
def topic_match_score(trending_title: str, candidate_title: str) -> float:
    """Word-overlap between a trending title and a candidate own-video
    title (0–1): |intersection| / |trending words|, stopwords removed.
    A score near 1 means the own video is about the same topic."""

    def _words(text: str) -> set[str]:
        return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}

    trending = _words(trending_title or "")
    candidate = _words(candidate_title or "")
    if not trending:
        return 0.0
    return round(len(trending & candidate) / len(trending), 2)


def match_own_video(channel_url: str, trending_title: str, *, playlist_end: int = 10) -> dict:
    """Pick the user's OWN channel video that best matches a trending topic.

    Uses yt-dlp (flat playlist, no download) to list the channel's
    recent videos with titles, then scores each title against the
    trending title with :func:`topic_match_score`. Returns
    ``{own_video_url, own_title, match_score}`` — the best match, even
    when the score is low (the user confirms it before the run starts).
    Raises :class:`TrendingError` in plain language when yt-dlp is
    missing or the channel cannot be read.
    """
    if not channel_url or not _URL_RE.match(channel_url.strip()):
        raise TrendingError("Give a YouTube channel URL to match against the trending topic.")
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        raise TrendingError(
            "Matching your channel needs yt-dlp, which is not installed "
            "here. Install yt-dlp (pip install yt-dlp) or paste your own "
            "video URL directly instead."
        )
    try:
        proc = subprocess.run(
            [
                yt_dlp,
                "--flat-playlist",
                "--playlist-end",
                str(max(1, playlist_end)),
                "--get-id",
                "--get-title",
                channel_url.strip(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrendingError(
            f"Could not read the channel ({exc}). Check the URL and try again."
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise TrendingError(
            "Could not read the channel — "
            f"{detail[-1] if detail else 'yt-dlp failed'}. "
            "Check the channel URL and try again."
        )
    lines = [line.strip() for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    # --get-id + --get-title print alternating id / title lines.
    best: dict | None = None
    for index in range(0, len(lines) - 1, 2):
        video_id, title = lines[index], lines[index + 1]
        score = topic_match_score(trending_title, title)
        if best is None or score > best["match_score"]:
            best = {
                "own_video_url": f"https://www.youtube.com/watch?v={video_id}",
                "own_title": title,
                "match_score": score,
            }
    if best is None:
        raise TrendingError("The channel has no videos the agent could read.")
    return best


__all__ = [
    "CC_LICENSE",
    "CC_LICENSE_URL",
    "SEARCH_MAX_RESULTS",
    "STANDARD_LICENSE",
    "STANDARD_LICENSE_REASON",
    "TRENDING_NOTICE",
    "TRENDING_WINDOW_DAYS",
    "TrendingCredentials",
    "TrendingError",
    "TrendingVideo",
    "UNVERIFIABLE_LICENSE_REASON",
    "YOUTUBE_API_BASE",
    "YOUTUBE_API_KEY_ENV",
    "YOUTUBE_API_KEY_SECRET",
    "check_video_license",
    "credentials_status",
    "extract_youtube_video_id",
    "is_trending_source",
    "license_url_for",
    "load_youtube_api_key",
    "match_own_video",
    "niche_search_query",
    "own_channel_id",
    "refuse_trending_source",
    "resolve_credentials",
    "save_youtube_api_key",
    "search_trending",
    "topic_match_score",
    "trend_score",
    "youtube_api_key_view",
]
