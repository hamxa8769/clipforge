"""Encrypted OAuth token storage for ClipForge v0.3.0.

Tokens live in the :class:`SecretStore` (OS keyring where available, else
Fernet-encrypted file) as one JSON blob per platform — never in the SQLite
database, never in ``settings.json``, never in the repo. The database
(:class:`clipforge.db.SocialAccount`) keeps only the display facts: label,
account ref, scopes, status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from clipforge.auth.providers import get_provider
from clipforge.auth.secrets import SecretStore

_EXPIRY_SKEW_S = 60  # treat a token as expired a minute early


@dataclass
class TokenRecord:
    """One platform's OAuth tokens + the account they belong to."""

    platform: str
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)
    account_name: str = ""
    avatar_url: str = ""
    account_ref: str = ""
    obtained_at: datetime | None = None

    def is_expired(self) -> bool:
        """True when the token is past expiry (or expiry is unknown and old).

        A record with no expiry at all (Meta long-lived tokens report it in
        the exchange response, but not always) is treated as valid — the
        health check, not the clock, decides then.
        """
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=_EXPIRY_SKEW_S)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": self.scopes,
            "account_name": self.account_name,
            "avatar_url": self.avatar_url,
            "account_ref": self.account_ref,
            "obtained_at": self.obtained_at.isoformat() if self.obtained_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TokenRecord:
        def _parse(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

        return cls(
            platform=data["platform"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=_parse(data.get("expires_at")),
            scopes=list(data.get("scopes") or []),
            account_name=data.get("account_name") or "",
            avatar_url=data.get("avatar_url") or "",
            account_ref=data.get("account_ref") or "",
            obtained_at=_parse(data.get("obtained_at")),
        )

    @classmethod
    def from_token_response(
        cls, platform: str, token: dict, *, scopes: list[str] | None = None
    ) -> TokenRecord:
        """Build a record from a provider token-exchange response."""
        spec = get_provider(platform)
        expires_in = token.get("expires_in")
        try:
            ttl = int(expires_in) if expires_in is not None else spec.default_token_ttl_s
        except (TypeError, ValueError):
            ttl = spec.default_token_ttl_s
        return cls(
            platform=platform,
            access_token=token["access_token"],
            refresh_token=token.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            scopes=scopes if scopes is not None else list(spec.scopes),
            obtained_at=datetime.now(UTC),
        )


class TokenStore:
    """Per-platform token records, encrypted at rest via SecretStore."""

    def __init__(self, secrets: SecretStore):
        self._secrets = secrets

    @staticmethod
    def _name(platform: str) -> str:
        return f"oauth_token:{platform}"

    def save(self, record: TokenRecord) -> None:
        self._secrets.set(self._name(record.platform), json.dumps(record.to_dict()))

    def load(self, platform: str) -> TokenRecord | None:
        raw = self._secrets.get(self._name(platform))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        try:
            return TokenRecord.from_dict(data)
        except (KeyError, TypeError):
            return None

    def delete(self, platform: str) -> bool:
        return self._secrets.delete(self._name(platform))


__all__ = ["TokenRecord", "TokenStore"]
