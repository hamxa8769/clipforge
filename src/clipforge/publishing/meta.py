"""Meta publishers — Instagram Graph API v26 + Facebook Reels Publishing API.

Instagram Reels (Graph API v26): 2-step container flow, professional
(Business/Creator) accounts only, personal accounts cannot use the API.
  1. POST /{ig-user-id}/media  (media_type=REELS, public video_url)
  2. Poll the container's status_code until FINISHED
  3. POST /{ig-user-id}/media_publish  (creation_id = container id)
Limits: 100 API posts / rolling 24h (Reels+feed+Stories), 200 calls/hr/user,
file 3s-15 min, <=300 MB, <=1920px wide. Hashtag cap in-app is 5 (Dec 2025)
even though API docs still say 30. Read ``content_publishing_limit`` live.

Facebook Reels: POST /{page-id}/video_reels, 30 Reels / rolling 24h per
Page, 3-90 seconds, >=540x960, MP4 recommended. No personal-profile Reels
via API. The API cap (90s) is stricter than in-app Reels (no cap).

Both need a PUBLIC video_url (Meta fetches the file) — a local path is
not enough for live publishing; dry-run flags that explicitly.
"""

from __future__ import annotations

import os

from clipforge.publishing.base import (
    PublishResult,
    dry_run_result,
    rate_guard,
    require_live,
    utcnow,
)
from clipforge.publishing.limits import PLATFORM_LIMITS

GRAPH_BASE = "https://graph.facebook.com/v26.0"


def _configured() -> bool:
    return bool(os.environ.get("META_ACCESS_TOKEN"))


def _public_url_required(video_url: str | None, video_path: str | None) -> str:
    if video_url:
        if not video_url.startswith(("https://", "http://")):
            raise ValueError("video_url must be a public http(s) URL for Meta to fetch")
        return video_url
    raise ValueError(
        "Meta publishing needs a PUBLIC video_url (Meta fetches the file). "
        f"A local path ({video_path!r}) cannot be published live — upload the "
        "file to public storage (e.g. S3/R2 presigned URL) first."
    )


# ---------------------------------------------------------------- Instagram
def publish_instagram(
    *,
    caption: str,
    video_url: str | None = None,
    video_path: str | None = None,
    ig_user_id: str | None = None,
    hashtags: list[str] | None = None,
    dry_run: bool = True,
) -> PublishResult:
    platform = "instagram"
    if len(hashtags or []) > PLATFORM_LIMITS[platform]["max_hashtags"]:
        raise ValueError(f"Instagram hashtag cap is 5 (Dec 2025); got {len(hashtags or [])}.")
    url = video_url or (
        None if dry_run and video_path else _public_url_required(video_url, video_path)
    )
    steps = [
        {
            "step": 1,
            "method": "POST",
            "url": f"{GRAPH_BASE}/{{ig-user-id}}/media",
            "params": {
                "media_type": "REELS",
                "video_url": url,
                "caption": caption,
                "share_to_feed": True,
            },
        },
        {
            "step": 2,
            "method": "GET",
            "url": f"{GRAPH_BASE}/{{container-id}}?fields=status_code",
            "note": "Poll until status_code == FINISHED",
        },
        {
            "step": 3,
            "method": "POST",
            "url": f"{GRAPH_BASE}/{{ig-user-id}}/media_publish",
            "params": {"creation_id": "{container-id}"},
        },
    ]
    payload = {
        "ig_user_id": ig_user_id or os.environ.get("INSTAGRAM_USER_ID", "{ig-user-id}"),
        "video_url": url,
        "caption": caption,
        "steps": steps,
    }
    if dry_run:
        return dry_run_result(
            platform,
            payload,
            "Instagram container -> poll -> media_publish. Live needs a "
            "professional account, Advanced Access + Business Verification, "
            "and a public video_url.",
        )
    require_live(dry_run, _configured(), "Instagram publish")
    rate_guard.check(f"instagram:{ig_user_id}", 200, 3600)
    import requests  # local import: only needed for live calls

    token = os.environ["META_ACCESS_TOKEN"]
    user_id = ig_user_id or os.environ.get("INSTAGRAM_USER_ID")
    if not user_id:
        raise ValueError("INSTAGRAM_USER_ID (or ig_user_id) is required for live publish")
    public_url = _public_url_required(video_url, video_path)
    container = requests.post(
        f"{GRAPH_BASE}/{user_id}/media",
        params={
            "media_type": "REELS",
            "video_url": public_url,
            "caption": caption,
            "access_token": token,
        },
        timeout=60,
    ).json()
    container_id = container.get("id")
    if not container_id:
        return PublishResult(
            platform=platform,
            status="failed",
            dry_run=False,
            payload=payload,
            error=f"container creation failed: {container}",
        )
    published = requests.post(
        f"{GRAPH_BASE}/{user_id}/media_publish",
        params={"creation_id": container_id, "access_token": token},
        timeout=60,
    ).json()
    return PublishResult(
        platform=platform,
        status="published",
        dry_run=False,
        provider_post_id=str(published.get("id")),
        payload=payload,
        message="Published to Instagram Reels.",
        published_at=utcnow(),
    )


# ---------------------------------------------------------------- Facebook
def publish_facebook(
    *,
    caption: str,
    video_url: str | None = None,
    video_path: str | None = None,
    page_id: str | None = None,
    duration_s: float | None = None,
    dry_run: bool = True,
) -> PublishResult:
    platform = "facebook"
    if duration_s is not None and duration_s > PLATFORM_LIMITS[platform]["max_duration_s"]:
        raise ValueError(
            f"Facebook Reels API caps at 90s; clip is {duration_s:.0f}s. "
            "Trim it (in-app Reels have no cap, the API does)."
        )
    url = video_url or (
        None if dry_run and video_path else _public_url_required(video_url, video_path)
    )
    payload = {
        "method": "POST",
        "url": f"{GRAPH_BASE}/{{page-id}}/video_reels",
        "page_id": page_id or os.environ.get("META_PAGE_ID", "{page-id}"),
        "params": {"video_url": url, "description": caption},
    }
    if dry_run:
        return dry_run_result(
            platform,
            payload,
            "POST /{page-id}/video_reels. Live needs a Page + "
            "pages_manage_posts, App Review, and a public video_url. "
            "30 Reels / rolling 24h per Page.",
        )
    require_live(dry_run, _configured(), "Facebook Reels publish")
    import requests

    token = os.environ["META_ACCESS_TOKEN"]
    pid = page_id or os.environ.get("META_PAGE_ID")
    if not pid:
        raise ValueError("META_PAGE_ID (or page_id) is required for live publish")
    public_url = _public_url_required(video_url, video_path)
    resp = requests.post(
        f"{GRAPH_BASE}/{pid}/video_reels",
        params={"video_url": public_url, "description": caption, "access_token": token},
        timeout=120,
    ).json()
    if "id" not in resp:
        return PublishResult(
            platform=platform,
            status="failed",
            dry_run=False,
            payload=payload,
            error=f"video_reels failed: {resp}",
        )
    return PublishResult(
        platform=platform,
        status="published",
        dry_run=False,
        provider_post_id=str(resp["id"]),
        payload=payload,
        message="Published to Facebook Reels.",
        published_at=utcnow(),
    )


__all__ = ["publish_instagram", "publish_facebook", "GRAPH_BASE"]
