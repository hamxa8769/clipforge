"""Provider specs for ClipForge v0.4.0 — one spec per platform.

The shape mirrors the provider contract from gitroomhq/postiz-app (AGPL-3.0)
— ``generateAuthUrl`` / ``authenticate`` / ``refreshToken`` / ``post`` plus
error mapping — as a *design reference only*: no Postiz code is used here,
and nothing in this module is derived from it. The per-platform facts
(endpoints, scopes, review gates) come from the ClipForge research report
(§3) and the official platform docs it cites.

Each spec carries the honest review/audit notice the Channels page must
show, and the working fallback that exists while review is pending.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from clipforge.publishing.limits import DAILY_CAPS

PLATFORMS = ("youtube", "instagram", "facebook", "tiktok", "x")


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the OAuth flow needs for one platform."""

    platform: str
    display_name: str
    auth_url: str
    token_url: str
    scopes: list[str]
    profile_url: str
    # Extra authorize-URL params (e.g. Google offline access).
    auth_params: dict[str, str] = field(default_factory=dict)
    # Extra token-exchange params.
    token_params: dict[str, str] = field(default_factory=dict)
    # How long a fresh access token lasts when the provider does not say.
    default_token_ttl_s: int = 3600
    # Shown on the Channels page until the platform review/audit passes.
    review_notice: str = ""
    # What still works while review is pending.
    fallback: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "youtube": ProviderSpec(
        platform="youtube",
        display_name="YouTube",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
        profile_url="https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
        auth_params={"access_type": "offline", "prompt": "consent"},
        default_token_ttl_s=3600,
        review_notice=(
            "Google OAuth verification is required before anyone other than the "
            "app owner can connect; until it passes, only test users added in "
            "Google Cloud Console can connect."
        ),
        fallback="Manual-post pack: download the clip + SEO copy and upload in YouTube Studio.",
    ),
    "instagram": ProviderSpec(
        platform="instagram",
        display_name="Instagram",
        auth_url="https://www.facebook.com/v19.0/dialog/oauth",
        token_url="https://graph.facebook.com/v19.0/oauth/access_token",
        scopes=[
            "instagram_basic",
            "instagram_content_publish",
            "pages_show_list",
            "pages_read_engagement",
        ],
        profile_url="https://graph.facebook.com/v19.0/me?fields=id,name,picture",
        default_token_ttl_s=3600,
        review_notice=(
            "Meta App Review + Business Verification are required for "
            "instagram_content_publish; until they pass, only test users and "
            "the app's own Business/Creator account can connect, and only a "
            "professional (Business/Creator) Instagram account linked to a "
            "Facebook Page can publish."
        ),
        fallback="Manual-post pack: download the clip + SEO copy and post in the Instagram app.",
    ),
    "facebook": ProviderSpec(
        platform="facebook",
        display_name="Facebook",
        auth_url="https://www.facebook.com/v19.0/dialog/oauth",
        token_url="https://graph.facebook.com/v19.0/oauth/access_token",
        scopes=[
            "pages_show_list",
            "pages_read_engagement",
            "pages_manage_posts",
        ],
        profile_url="https://graph.facebook.com/v19.0/me?fields=id,name,picture",
        default_token_ttl_s=3600,
        review_notice=(
            "Meta App Review + Business Verification are required for "
            "pages_manage_posts; until they pass, only test users can connect."
        ),
        fallback="Manual-post pack: download the clip + SEO copy and post the Reel on the Page.",
    ),
    "tiktok": ProviderSpec(
        platform="tiktok",
        display_name="TikTok",
        auth_url="https://www.tiktok.com/v2/auth/authorize/",
        token_url="https://open.tiktokapis.com/v2/oauth/token/",
        scopes=["user.info.basic", "video.upload"],
        profile_url="https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name,avatar_url",
        default_token_ttl_s=86400,
        review_notice=(
            "TikTok Content Posting API audit is pending: unaudited apps can "
            "only create private / inbox drafts (max 5 pending drafts per "
            "user per 24h). Public Direct Post needs the passed audit."
        ),
        fallback=(
            "Inbox draft: the clip is uploaded as a private draft to your "
            "TikTok inbox — open TikTok and post it from there."
        ),
    ),
    "x": ProviderSpec(
        platform="x",
        display_name="X (Twitter)",
        auth_url="https://x.com/i/oauth2/authorize",
        token_url="https://api.x.com/2/oauth2/token",
        scopes=["tweet.read", "tweet.write", "users.read", "offline.access"],
        profile_url="https://api.x.com/2/users/me",
        default_token_ttl_s=7200,
        review_notice=(
            "X posting needs a paid API plan (Basic or higher): the free "
            "tier is read-only, so posting fails with HTTP 403 until the "
            "app is on a paid plan."
        ),
        fallback=("Manual-post pack: download the clip + post text and publish it in the X app."),
    ),
}


def get_provider(platform: str) -> ProviderSpec:
    """Return the spec for a platform, or raise ValueError for an unknown one."""
    try:
        return PROVIDERS[platform]
    except KeyError:
        raise ValueError(f"unknown platform: {platform}") from None


def daily_cap(platform: str) -> int:
    """Conservative daily publish cap for a platform (see publishing/limits.py)."""
    return DAILY_CAPS[platform]


def redirect_base() -> str:
    """Base URL the platform redirects back to after OAuth.

    Loopback by default (desktop app). Override with
    ``CLIPFORGE_REDIRECT_BASE`` when the server runs behind a tunnel or on
    another host — the value shown on the Settings page must match what is
    registered in the platform's developer app, or the callback is refused.
    """
    from clipforge.config import settings  # noqa: PLC0415  (avoids import cycle)

    return os.environ.get("CLIPFORGE_REDIRECT_BASE") or (f"http://127.0.0.1:{settings.api_port}")


def redirect_uri(platform: str) -> str:
    """The exact redirect URI to register for a platform's OAuth app."""
    return f"{redirect_base()}/auth/callback/{platform}"


__all__ = [
    "PLATFORMS",
    "PROVIDERS",
    "ProviderSpec",
    "daily_cap",
    "get_provider",
    "redirect_base",
    "redirect_uri",
]
