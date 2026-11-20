"""Environment configuration for ClipForge v0.1.0.

All credentials come from the environment — never from code, never logged.
``DRY_RUN`` defaults to True: nothing publishes live unless explicitly
opted in *and* credentials are present (see publishing adapters).

Env vars read by this module (see also the per-adapter vars in
``clipforge.publishing`` — listed in full in the delivery report):

- ``DATABASE_URL`` (alias ``CLIPFORGE_DATABASE_URL``) — SQLAlchemy URL.
  Default ``sqlite:///./clipforge.db`` (relative to the process CWD;
  set an absolute path in production). Postgres URLs work unchanged.
- ``CLIPFORGE_DRY_RUN`` — default ``true``. ``false`` only enables live
  publishing when the relevant credentials are also present.
- ``CLIPFORGE_ENV`` — ``dev`` / ``test`` / ``prod`` (default ``dev``).
- ``CLIPFORGE_API_HOST`` / ``CLIPFORGE_API_PORT`` — uvicorn bind (default
  ``127.0.0.1`` / ``8000``).
- ``CLIPFORGE_CORS_ORIGINS`` — comma-separated origins, default
  ``http://localhost:3000,http://127.0.0.1:3000``.
- ``CLIPFORGE_DATA_DIR`` — where the SQLite file / uploads live if a
  relative ``DATABASE_URL`` is used (default ``./data`` is *not* forced;
  the SQLite path is taken verbatim from ``DATABASE_URL``).
- ``YOUTUBE_CLIENT_ID`` / ``YOUTUBE_CLIENT_SECRET`` / ``YOUTUBE_REFRESH_TOKEN``
  — YouTube Data API v3 OAuth (live only).
- ``META_ACCESS_TOKEN`` / ``META_PAGE_ID`` / ``INSTAGRAM_USER_ID``
  — Meta Graph API (live only).
- ``TIKTOK_ACCESS_TOKEN`` / ``TIKTOK_CLIENT_KEY`` / ``TIKTOK_CLIENT_SECRET``
  — TikTok Content Posting API (live only).
- ``TIKTOK_DIRECT_POST_OPT_IN`` — ``true`` only after a passed TikTok
  audit; Direct Post stays gated otherwise (see publishing/tiktok.py).
- ``CLIPFORGE_LLM_API_KEY`` — optional, only for LLM polish of SEO copy.
  Without it, SEO is purely deterministic templates (default).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of environment config (read once at import)."""

    database_url: str = field(
        default_factory=lambda: (
            os.environ.get("DATABASE_URL")
            or os.environ.get("CLIPFORGE_DATABASE_URL")
            or "sqlite:///./clipforge.db"
        )
    )
    dry_run: bool = field(default_factory=lambda: _env_bool("CLIPFORGE_DRY_RUN", True))
    env: str = field(default_factory=lambda: os.environ.get("CLIPFORGE_ENV", "dev"))
    api_host: str = field(default_factory=lambda: os.environ.get("CLIPFORGE_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: int(os.environ.get("CLIPFORGE_API_PORT", "8000")))
    cors_origins: list[str] = field(
        default_factory=lambda: _env_list(
            "CLIPFORGE_CORS_ORIGINS",
            ["http://localhost:3000", "http://127.0.0.1:3000"],
        )
    )
    data_dir: str = field(default_factory=lambda: os.environ.get("CLIPFORGE_DATA_DIR", "./data"))

    # Credential *presence* only — values are read by the adapters at call
    # time from os.environ and are never stored on this object / logged.
    youtube_configured: bool = field(
        default_factory=lambda: bool(os.environ.get("YOUTUBE_REFRESH_TOKEN"))
    )
    meta_configured: bool = field(default_factory=lambda: bool(os.environ.get("META_ACCESS_TOKEN")))
    tiktok_configured: bool = field(
        default_factory=lambda: bool(os.environ.get("TIKTOK_ACCESS_TOKEN"))
    )
    tiktok_direct_post_opt_in: bool = field(
        default_factory=lambda: _env_bool("TIKTOK_DIRECT_POST_OPT_IN", False)
    )
    llm_configured: bool = field(
        default_factory=lambda: bool(os.environ.get("CLIPFORGE_LLM_API_KEY"))
    )

    def live_publishing_allowed(self) -> bool:
        """Live mode needs BOTH dry_run off AND at least one credential set."""
        return (not self.dry_run) and (
            self.youtube_configured or self.meta_configured or self.tiktok_configured
        )


# Pydantic-settings is the preferred loader when installed; the dataclass
# above is the dependency-free equivalent so the package imports without it.
try:  # pragma: no cover - optional dependency
    from pydantic_settings import BaseSettings as _BaseSettings  # type: ignore

    class PydanticSettings(_BaseSettings):  # type: ignore[no-redef]
        """Same config via pydantic-settings (used when available)."""

        model_config = {"env_file": ".env", "extra": "ignore"}  # type: ignore[attr-defined]

        database_url: str = "sqlite:///./clipforge.db"
        dry_run: bool = True

    _HAS_PYDANTIC_SETTINGS = True
except Exception:  # pragma: no cover
    _HAS_PYDANTIC_SETTINGS = False


settings = Settings()

__all__ = ["Settings", "settings"]
