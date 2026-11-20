"""Common publishing base: result type, dry-run helper, rate-limit guards.

Every adapter must:
- support ``dry_run=True`` (the default) — validate the payload and return
  exactly what WOULD be sent, without any network call;
- refuse live publishing without credentials AND an explicit live opt-in;
- never log tokens, never hard-code limits (import from limits.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime


class NotConfiguredError(RuntimeError):
    """Raised when a live publish is attempted without the needed SDK/credentials."""


class RateLimitError(RuntimeError):
    """Raised when a per-account / per-token rate guard would be exceeded."""


class NotApprovedError(RuntimeError):
    """Raised when publishing is attempted without an approved Post record."""


@dataclass
class PublishResult:
    platform: str
    status: str  # "dry_run" | "published" | "draft_created" | "failed" | "skipped"
    dry_run: bool = True
    provider_post_id: str | None = None
    payload: dict = field(default_factory=dict)  # what was / would be sent (no tokens)
    message: str = ""
    error: str | None = None
    published_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "status": self.status,
            "dry_run": self.dry_run,
            "provider_post_id": self.provider_post_id,
            "payload": self.payload,
            "message": self.message,
            "error": self.error,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


def utcnow() -> datetime:
    return datetime.now(UTC)


class RateLimitGuard:
    """Tiny in-memory per-key rate guard (process-local).

    This is a safety net, not the enforcement layer: the scheduler also
    enforces per-account daily caps from the DB before calling an adapter.
    For multi-process deployments replace with Redis / DB counters.
    """

    def __init__(self) -> None:
        self._calls: dict[str, list[float]] = {}

    def check(self, key: str, max_calls: int, window_s: int) -> None:
        now = time.monotonic()
        history = [t for t in self._calls.get(key, []) if now - t < window_s]
        if len(history) >= max_calls:
            raise RateLimitError(
                f"rate limit for {key}: {max_calls} calls per {window_s}s exceeded"
            )
        history.append(now)
        self._calls[key] = history


# One shared guard per process; adapters key it by account/provider.
rate_guard = RateLimitGuard()


def require_live(dry_run: bool, configured: bool, what: str) -> None:
    """Refuse a live call that is not explicitly enabled and credentialed."""
    if dry_run:
        return
    if not configured:
        raise NotConfiguredError(
            f"live {what} requires credentials from the environment "
            f"(see config.py env list); DRY_RUN is off but no credential is set. "
            "Set the credential env var or run in dry-run mode."
        )


def dry_run_result(platform: str, payload: dict, message: str) -> PublishResult:
    return PublishResult(
        platform=platform,
        status="dry_run",
        dry_run=True,
        payload=payload,
        message=f"DRY-RUN — nothing sent. {message}",
    )


__all__ = [
    "PublishResult",
    "NotConfiguredError",
    "RateLimitError",
    "NotApprovedError",
    "RateLimitGuard",
    "rate_guard",
    "require_live",
    "dry_run_result",
    "utcnow",
]
