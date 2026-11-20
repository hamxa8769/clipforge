"""YouTube publisher — YouTube Data API v3, ``videos.insert``.

Official API only. A Short is defined by the FILE (vertical, <=3 min) —
there is no ``#Shorts`` flag. Scheduling uses ``publishAt`` + private
status. Custom Shorts thumbnails are YPP-only in Studio; the API
``thumbnails.set`` does not mention Shorts.

Quota conflict (VERIFY_LIVE, see limits.py): older docs 1,600 units/insert
(~6/day from the 10,000-unit pool); newer sources say 1 unit/insert with a
dedicated 100-uploads/day bucket from 1 Jun 2026. Cap enforcement defaults
to the conservative 6/day.

Live path needs ``google-api-python-client`` + ``google-auth`` and the
``YOUTUBE_REFRESH_TOKEN`` (+ client id/secret) env vars. If the SDK is not
installed, live mode raises a clear :class:`NotConfiguredError` — dry-run
always works.
"""

from __future__ import annotations

import os
from datetime import datetime

from clipforge.publishing.base import (
    NotConfiguredError,
    PublishResult,
    dry_run_result,
    require_live,
    utcnow,
)
from clipforge.publishing.limits import PLATFORM_LIMITS

PLATFORM = "youtube"
MAX_DURATION_S = PLATFORM_LIMITS[PLATFORM]["max_duration_s"]


def _configured() -> bool:
    return bool(os.environ.get("YOUTUBE_REFRESH_TOKEN"))


def build_payload(
    *,
    title: str,
    description: str,
    video_path: str,
    publish_at: datetime | None = None,
    privacy: str = "private",
    is_ai_generated: bool = False,
    tags: list[str] | None = None,
) -> dict:
    """The exact videos.insert request body that would be sent (no secrets)."""
    if len(title) > 100:
        raise ValueError(f"YouTube title is {len(title)} chars, max 100")
    snippet: dict = {
        "title": title,
        "description": description,
        "tags": tags or [],
        "categoryId": "22",  # People & Blogs — caller may override via tags/title
    }
    status: dict = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at is not None:
        status["publishAt"] = publish_at.isoformat()
        status["privacyStatus"] = "private"  # scheduled uploads must start private
    body: dict = {"snippet": snippet, "status": status}
    if is_ai_generated:
        # YouTube requires disclosure when AI meaningfully alters/generates
        # photorealistic content (report §7). The Data API surfaces this via
        # the upload's content disclosure; record the flag so the UI / live
        # path sets it — never publish AI-generated realistic content unlabeled.
        body["ai_content_disclosure"] = True
    return {"method": "videos.insert", "part": "snippet,status", "body": body, "file": video_path}


def publish(
    *,
    title: str,
    description: str,
    video_path: str,
    publish_at: datetime | None = None,
    duration_s: float | None = None,
    is_ai_generated: bool = False,
    tags: list[str] | None = None,
    dry_run: bool = True,
) -> PublishResult:
    if duration_s is not None and duration_s > MAX_DURATION_S:
        raise ValueError(
            f"Not a Short: {duration_s:.0f}s exceeds the 180s Shorts limit. "
            "Trim it or publish as a long-form video (out of scope for v0.1)."
        )
    if dry_run and not video_path:
        raise ValueError("video_path is required even for a dry-run (payload validation)")
    payload = build_payload(
        title=title,
        description=description,
        video_path=video_path,
        publish_at=publish_at,
        is_ai_generated=is_ai_generated,
        tags=tags,
    )
    if dry_run:
        return dry_run_result(
            PLATFORM,
            payload,
            "videos.insert would upload the vertical file as a Short; "
            "publishAt schedules it (private until then). Quota cost: "
            "VERIFY_LIVE — 1,600 units (old) vs 1 unit (new bucket).",
        )
    require_live(dry_run, _configured(), "YouTube upload")
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError as exc:
        raise NotConfiguredError(
            "google-api-python-client / google-auth not installed. "
            "Install: pip install google-api-python-client google-auth"
        ) from exc
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ.get("YOUTUBE_CLIENT_ID", ""),
        client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    youtube = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(video_path, resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=payload["body"], media_body=media)
    response = request.execute()  # resumable upload handled by the client lib
    return PublishResult(
        platform=PLATFORM,
        status="published",
        dry_run=False,
        provider_post_id=str(response.get("id")),
        payload={**payload, "response_id": response.get("id")},
        message="Uploaded to YouTube (Short if vertical and <=3 min).",
        published_at=utcnow(),
    )


__all__ = ["publish", "build_payload", "PLATFORM"]
