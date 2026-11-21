"""Guided-connect data for ClipForge v0.4.0 — one plain-language guide per platform.

The web Connect wizard shows this instead of raw JSON: the official
developer console to open, the exact steps in order, what to copy where,
the exact redirect URI this server expects (passed in from the running
server — never a hardcoded guess), the scopes requested, and the honest
review/audit note for the platform.

Facts are the official platform flows (Google Cloud Console, Meta for
Developers, TikTok for Developers). No credentials are read or returned
here — only ``saved`` flags, so the wizard can tick off completed steps.
"""

from __future__ import annotations

from clipforge.auth.providers import PLATFORMS, daily_cap, get_provider

GUIDE_VERSION = "0.4.0"


def _steps(*steps: tuple[str, str]) -> list[dict]:
    return [
        {"step": i + 1, "title": title, "detail": detail} for i, (title, detail) in enumerate(steps)
    ]


_GUIDES: dict[str, dict] = {
    "youtube": {
        "console_name": "Google Cloud Console",
        "console_url": "https://console.cloud.google.com/apis/credentials",
        "api_library_url": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
        "steps": _steps(
            (
                "Create a Google Cloud project",
                "Open Google Cloud Console and create a new project (or pick an existing one).",
            ),
            (
                "Enable the YouTube Data API v3",
                "Open the API Library, search for 'YouTube Data API v3', and click Enable.",
            ),
            (
                "Create an OAuth client ID",
                "Go to APIs & Services → Credentials → Create Credentials → "
                "OAuth client ID, choose application type 'Desktop app', "
                "and create it. Copy the Client ID and Client secret.",
            ),
            (
                "Save the credentials in ClipForge",
                "Paste the Client ID and Client secret into Settings "
                "(the wizard's Step 3). They are stored encrypted on "
                "this machine and never shown back in full.",
            ),
            (
                "Register the redirect URI",
                "In the same OAuth client, under Authorized redirect URIs, "
                "add the exact redirect URI shown below. It must match "
                "character for character, or Google refuses the connection.",
            ),
            (
                "Add yourself as a test user",
                "On the OAuth consent screen, add your Google account under "
                "Test users. Until Google verifies the app, only test users "
                "can connect.",
            ),
        ),
        "credentials": [
            {
                "key": "client_id",
                "label": "Client ID",
                "where": "Google Cloud Console → Credentials → your OAuth client",
            },
            {
                "key": "client_secret",
                "label": "Client secret",
                "where": "Google Cloud Console → Credentials → your OAuth client",
            },
        ],
    },
    "instagram": {
        "console_name": "Meta for Developers",
        "console_url": "https://developers.facebook.com/apps",
        "steps": _steps(
            (
                "Create a Meta app",
                "Open Meta for Developers and create an app of type "
                "'Business'. Note the App ID — that is the Client ID.",
            ),
            (
                "Add the Instagram product",
                "In the app dashboard, add the Instagram product "
                "(Instagram Graph API) and Facebook Login.",
            ),
            (
                "Link a professional Instagram account",
                "Link a Business or Creator Instagram account to a "
                "Facebook Page you manage. A personal Instagram account "
                "cannot publish through the API.",
            ),
            (
                "Save the credentials in ClipForge",
                "Copy the App ID and App Secret (App Settings → Basic) "
                "into Settings. Instagram and Facebook share this one "
                "Meta app, so the same pair works for both.",
            ),
            (
                "Register the redirect URI",
                "In Facebook Login → Settings, add the exact redirect URI "
                "shown below under Valid OAuth Redirect URIs.",
            ),
            (
                "Pass Meta App Review to publish for everyone",
                "The instagram_content_publish permission needs Meta App "
                "Review + Business Verification. Until they pass, only "
                "test users and the app's own account can connect.",
            ),
        ),
        "credentials": [
            {
                "key": "client_id",
                "label": "App ID",
                "where": "Meta for Developers → your app → Settings → Basic",
            },
            {
                "key": "client_secret",
                "label": "App Secret",
                "where": "Meta for Developers → your app → Settings → Basic",
            },
        ],
    },
    "facebook": {
        "console_name": "Meta for Developers",
        "console_url": "https://developers.facebook.com/apps",
        "steps": _steps(
            (
                "Create a Meta app",
                "Open Meta for Developers and create an app of type "
                "'Business'. Note the App ID — that is the Client ID.",
            ),
            (
                "Add the Facebook Login product",
                "In the app dashboard, add Facebook Login and the "
                "permissions for managing Page posts.",
            ),
            (
                "Use a Facebook Page you manage",
                "Publishing goes to a Facebook Page, not a personal "
                "profile. You must be an admin or editor of the Page.",
            ),
            (
                "Save the credentials in ClipForge",
                "Copy the App ID and App Secret (App Settings → Basic) "
                "into Settings. Instagram and Facebook share this one "
                "Meta app, so the same pair works for both.",
            ),
            (
                "Register the redirect URI",
                "In Facebook Login → Settings, add the exact redirect URI "
                "shown below under Valid OAuth Redirect URIs.",
            ),
            (
                "Pass Meta App Review to publish for everyone",
                "The pages_manage_posts permission needs Meta App Review "
                "+ Business Verification. Until they pass, only test "
                "users can connect.",
            ),
        ),
        "credentials": [
            {
                "key": "client_id",
                "label": "App ID",
                "where": "Meta for Developers → your app → Settings → Basic",
            },
            {
                "key": "client_secret",
                "label": "App Secret",
                "where": "Meta for Developers → your app → Settings → Basic",
            },
        ],
    },
    "tiktok": {
        "console_name": "TikTok for Developers",
        "console_url": "https://developers.tiktok.com/apps",
        "steps": _steps(
            (
                "Create a TikTok developer app",
                "Open TikTok for Developers, sign in, and create an app. "
                "Note the Client key — that is the Client ID.",
            ),
            (
                "Add Login Kit and Content Posting API",
                "In the app dashboard, add the Login Kit and Content "
                "Posting API products and request the video.upload scope.",
            ),
            (
                "Save the credentials in ClipForge",
                "Copy the Client key and Client secret into Settings. "
                "Keep them private — anyone with them can act as the app.",
            ),
            (
                "Register the redirect URI",
                "In the app settings, add the exact redirect URI shown "
                "below. It must match character for character, or TikTok "
                "refuses the connection.",
            ),
            (
                "Start in the sandbox, then request production",
                "New apps start in the sandbox. Request production access "
                "for the Content Posting API when you are ready to post "
                "from a real account.",
            ),
            (
                "Pass the Content Posting API audit for public posting",
                "Until the audit passes, ClipForge can only create private "
                "inbox drafts (max 5 pending drafts per user per 24h). "
                "Public Direct Post needs the passed audit.",
            ),
        ),
        "credentials": [
            {
                "key": "client_id",
                "label": "Client key",
                "where": "TikTok for Developers → your app → Basic information",
            },
            {
                "key": "client_secret",
                "label": "Client secret",
                "where": "TikTok for Developers → your app → Basic information",
            },
        ],
    },
    "x": {
        "console_name": "X Developer Portal",
        "console_url": "https://developer.x.com/en/portal/dashboard",
        "steps": _steps(
            (
                "Create an X developer app",
                "Open the X Developer Portal, sign in, and create a project "
                "and app. Note the Client ID — X also gives a Client secret.",
            ),
            (
                "Enable OAuth 2.0 with PKCE",
                "In the app settings, turn on OAuth 2.0, choose 'Web App' "
                "as the type, and keep PKCE on — ClipForge uses PKCE.",
            ),
            (
                "Save the credentials in ClipForge",
                "Copy the Client ID and Client secret into Settings. "
                "Keep them private — anyone with them can act as the app.",
            ),
            (
                "Register the redirect URI",
                "In the app settings, add the exact redirect URI shown "
                "below as a callback URL. It must match character for "
                "character, or X refuses the connection.",
            ),
            (
                "Use a paid API plan for posting",
                "The free X API tier is read-only. Posting needs a paid "
                "Basic plan or higher, otherwise X answers 403.",
            ),
        ),
        "credentials": [
            {
                "key": "client_id",
                "label": "Client ID",
                "where": "X Developer Portal → your app → Keys and tokens",
            },
            {
                "key": "client_secret",
                "label": "Client secret",
                "where": "X Developer Portal → your app → Keys and tokens",
            },
        ],
    },
}


def connect_guide(
    platform: str,
    *,
    redirect_uri: str,
    client_id_saved: bool = False,
    client_secret_saved: bool = False,
) -> dict:
    """Plain-language setup guide for one platform.

    ``redirect_uri`` is the exact URI the running server expects
    (``clipforge.auth.providers.redirect_uri``) — the wizard must show
    this value, not a guessed one. ``client_id_saved`` /
    ``client_secret_saved`` are presence flags only, never values.
    Raises ValueError for an unknown platform.
    """
    spec = get_provider(platform)
    base = _GUIDES[platform]
    saved = {"client_id": client_id_saved, "client_secret": client_secret_saved}
    return {
        "platform": platform,
        "display_name": spec.display_name,
        "console_name": base["console_name"],
        "console_url": base["console_url"],
        "steps": list(base["steps"]),
        "credentials": [{**cred, "saved": saved[cred["key"]]} for cred in base["credentials"]],
        "redirect_uri": redirect_uri,
        "scopes": list(spec.scopes),
        "review_notice": spec.review_notice,
        "fallback": spec.fallback,
        "daily_cap": daily_cap(platform),
    }


__all__ = ["GUIDE_VERSION", "PLATFORMS", "connect_guide"]
