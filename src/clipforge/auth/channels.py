"""Channel status aggregation for the Channels page.

One card per platform, built from the *real* backend state — never
hardcoded: the encrypted OAuth token (expiry-aware), the SocialAccount
row (label / scopes / daily-cap override), and the legacy env-var
accounts that predate OAuth. The per-platform review notice and fallback
come from the provider spec, so the UI always says plainly what is
blocked and what still works.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from clipforge.auth.providers import PLATFORMS, daily_cap, get_provider
from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenStore
from clipforge.db import SocialAccount

# Env-var accounts (pre-OAuth): a configured env token counts as connected,
# but the backend knows no account name / avatar / expiry for it.
_ENV_TOKEN_VARS = {
    "youtube": "YOUTUBE_REFRESH_TOKEN",
    "instagram": "META_ACCESS_TOKEN",
    "facebook": "META_ACCESS_TOKEN",
    "tiktok": "TIKTOK_ACCESS_TOKEN",
}


def channel_status(platform: str, *, secrets: SecretStore, db: Session) -> dict:
    """Full status card for one platform."""
    spec = get_provider(platform)
    record = TokenStore(secrets).load(platform)
    account = (
        db.query(SocialAccount)
        .filter_by(provider=platform)
        .order_by(SocialAccount.created_at.desc())
        .first()
    )
    card: dict = {
        "platform": platform,
        "display_name": spec.display_name,
        "status": "not_connected",
        "account_name": "",
        "avatar_url": "",
        "account_ref": "",
        "scopes": list(spec.scopes),
        "daily_cap": daily_cap(platform),
        "expires_at": None,
        "source": "none",
        "review_notice": spec.review_notice,
        "fallback": spec.fallback,
    }
    if account is not None:
        card["scopes"] = list(account.scopes or spec.scopes)
        if account.daily_cap_override is not None:
            card["daily_cap"] = account.daily_cap_override
        card["account_name"] = account.label or ""
        card["account_ref"] = account.account_ref or ""

    if record is not None:
        card["source"] = "oauth"
        card["account_name"] = record.account_name or card["account_name"]
        card["avatar_url"] = record.avatar_url
        card["account_ref"] = record.account_ref or card["account_ref"]
        card["scopes"] = record.scopes or card["scopes"]
        card["expires_at"] = record.expires_at.isoformat() if record.expires_at else None
        card["status"] = "expired" if record.is_expired() else "connected"
        card["can_refresh"] = bool(record.refresh_token) or platform in ("instagram", "facebook")
        return card

    # No OAuth token — fall back to a legacy env-var account, if configured.
    env_var = _ENV_TOKEN_VARS.get(platform, "")
    if env_var and os.environ.get(env_var):
        card["status"] = "connected"
        card["source"] = "env"
        card["account_name"] = card["account_name"] or "Connected via environment"
        card["can_refresh"] = False
        return card

    # Not connected: drop the stale label/ref from a previously connected
    # account row — the card must not claim an account it has no token for.
    card["account_name"] = ""
    card["avatar_url"] = ""
    card["account_ref"] = ""
    card["can_refresh"] = False
    return card


def all_channels(*, secrets: SecretStore, db: Session) -> list[dict]:
    """Status cards for every platform, in display order."""
    return [channel_status(p, secrets=secrets, db=db) for p in PLATFORMS]


__all__ = ["all_channels", "channel_status"]
