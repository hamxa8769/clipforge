"""App settings for ClipForge v0.3.0 — masked credentials + safe preferences.

Two stores, kept strictly apart:

* **Non-secret preferences** live in ``<data_dir>/settings.json`` (plain
  JSON): dry-run override, default privacy per platform, LLM base URL /
  model, default platforms. No secret ever lands here.
* **Secrets** (per-platform OAuth client id/secret, LLM API key) live in
  the :class:`SecretStore` (OS keyring / encrypted file). The API only ever
  returns ``configured`` + ``last4`` for them — the masked display the
  Settings page shows.

Dry-run: the ``dry_run`` field here is an *override*. ``null`` (default)
means "follow the ``CLIPFORGE_DRY_RUN`` environment variable" (the v0.2.x
behaviour); setting it to true/false from the Settings page takes
precedence for the running server, so the toggle works without a restart.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from clipforge.auth.providers import PLATFORMS, get_provider
from clipforge.auth.secrets import SecretStore, mask_secret
from clipforge.monetization import THRESHOLDS

# Secret names in the SecretStore (never shown, never logged).
SECRET_NAMES = {
    "youtube": {"client_id": "app:youtube:client_id", "client_secret": "app:youtube:client_secret"},
    "instagram": {"client_id": "app:meta:client_id", "client_secret": "app:meta:client_secret"},
    "facebook": {"client_id": "app:meta:client_id", "client_secret": "app:meta:client_secret"},
    "tiktok": {"client_id": "app:tiktok:client_id", "client_secret": "app:tiktok:client_secret"},
    "llm": {"api_key": "app:llm:api_key"},
}

# Env vars the pre-OAuth accounts used — still honoured as a fallback so
# existing setups keep working; the Settings page shows them as "env".
LEGACY_ENV_VARS = {
    "youtube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
    "instagram": ["META_ACCESS_TOKEN"],
    "facebook": ["META_ACCESS_TOKEN"],
    "tiktok": ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN"],
    "llm": ["CLIPFORGE_LLM_API_KEY"],
}

DEFAULT_PRIVACY = {
    "youtube": "private",
    "instagram": "public",
    "facebook": "public",
    "tiktok": "private",
}


@dataclass
class AppSettings:
    """Non-secret preferences (stored in settings.json)."""

    dry_run: bool | None = None  # None = follow CLIPFORGE_DRY_RUN env
    default_privacy: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PRIVACY))
    default_platforms: list[str] = field(default_factory=lambda: list(PLATFORMS))
    llm_provider: str = "openai-compatible"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AppSettings:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        settings = cls(**known)
        # Drop unknown platforms / bad privacy values instead of trusting them.
        kept = {
            p: v
            for p, v in settings.default_privacy.items()
            if p in PLATFORMS and v in ("public", "private", "unlisted")
        }
        settings.default_privacy = {p: kept.get(p, DEFAULT_PRIVACY[p]) for p in PLATFORMS}
        settings.default_platforms = [p for p in settings.default_platforms if p in PLATFORMS]
        return settings


class SettingsStore:
    """Load/save preferences + masked credential views + connection tests."""

    def __init__(self, data_dir: str | Path, secrets: SecretStore | None = None):
        self.data_dir = Path(data_dir)
        self.secrets = secrets or SecretStore(self.data_dir)
        self._path = self.data_dir / "settings.json"

    # ------------------------------------------------------------ preferences
    def load(self) -> AppSettings:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AppSettings()
        if not isinstance(data, dict):
            return AppSettings()
        return AppSettings.from_dict(data)

    def save(self, settings: AppSettings) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(settings.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

    def effective_dry_run(self, env_dry_run: bool) -> bool:
        """Settings override wins; otherwise the environment decides."""
        stored = self.load().dry_run
        return env_dry_run if stored is None else stored

    # ------------------------------------------------------------ secrets
    def get_credential(self, service: str, kind: str) -> str | None:
        """A stored credential value, or the legacy env var as fallback.

        Values are only ever used server-side (OAuth exchange, API test);
        they are never returned by any endpoint.
        """
        name = SECRET_NAMES.get(service, {}).get(kind)
        if name:
            value = self.secrets.get(name)
            if value:
                return value
        return None

    def set_credential(self, service: str, kind: str, value: str | None) -> None:
        """Store a credential; empty/None clears it."""
        name = SECRET_NAMES.get(service, {}).get(kind)
        if not name:
            raise ValueError(f"unknown credential: {service}/{kind}")
        if value:
            self.secrets.set(name, value)
        else:
            self.secrets.delete(name)

    def credential_view(self, service: str, kind: str) -> dict:
        """Masked view for the UI: configured flag + last 4 chars only."""
        import os  # noqa: PLC0415  (legacy env fallback, read at call time)

        value = self.get_credential(service, kind)
        source = "stored"
        if value is None:
            env_vars = LEGACY_ENV_VARS.get(service, [])
            env_name = {
                "client_id": next(
                    (v for v in env_vars if "CLIENT_ID" in v or "CLIENT_KEY" in v), ""
                ),
                "client_secret": next((v for v in env_vars if "SECRET" in v), ""),
                "api_key": next((v for v in env_vars if "API_KEY" in v), ""),
            }.get(kind, "")
            if env_name and os.environ.get(env_name):
                value = os.environ[env_name]
                source = "env"
        return {
            "configured": bool(value),
            "last4": value[-4:] if value else "",
            "masked": mask_secret(value),
            "source": source if value else "none",
        }

    # ------------------------------------------------------------ public view
    def public_view(self, *, env_dry_run: bool) -> dict:
        """Everything GET /settings returns: masked credentials + status."""
        settings = self.load()
        ffmpeg = shutil.which("ffmpeg")
        return {
            "dry_run": self.effective_dry_run(env_dry_run),
            "dry_run_source": "settings" if settings.dry_run is not None else "env",
            "default_privacy": settings.default_privacy,
            "default_platforms": settings.default_platforms,
            "llm": {
                "provider": settings.llm_provider,
                "base_url": settings.llm_base_url,
                "model": settings.llm_model,
                "api_key": self.credential_view("llm", "api_key"),
            },
            "credentials": {
                platform: {
                    "client_id": self.credential_view(platform, "client_id"),
                    "client_secret": self.credential_view(platform, "client_secret"),
                }
                for platform in PLATFORMS
            },
            "data_dir": str(self.data_dir.resolve()),
            "secret_backend": self.secrets.backend,
            "ffmpeg": {
                "found": bool(ffmpeg),
                "path": ffmpeg or "",
                "install_url": "https://ffmpeg.org/download.html",
            },
            "monetization_thresholds": [
                {
                    "platform": platform,
                    "program": entry["program"],
                    "threshold": entry["metrics"],
                    "note": entry.get("note", ""),
                }
                for platform, entries in THRESHOLDS.items()
                for entry in entries
            ],
        }

    # ------------------------------------------------------------ test connection
    def test_connection(self, service: str, *, client: httpx.Client | None = None) -> dict:
        """Hit the real API with the stored credential and report the result.

        Returns ``{"ok": bool, "message": str}`` — the exact provider error
        is surfaced, never a generic "failed". No credential is included.
        Never raises: an unexpected failure becomes an honest not-ok
        result instead of a 500.
        """
        try:
            if service in PLATFORMS:
                return self._test_platform(service, client=client)
            if service == "llm":
                return self._test_llm(client=client)
        except Exception as exc:  # noqa: BLE001  (test endpoint must not 500)
            return {"ok": False, "message": f"Test failed before reaching the API: {exc}"}
        return {"ok": False, "message": f"Unknown service: {service}."}

    def _test_platform(self, platform: str, *, client: httpx.Client | None = None) -> dict:
        from clipforge.auth.oauth import check_health  # noqa: PLC0415
        from clipforge.auth.token_store import TokenStore  # noqa: PLC0415

        spec = get_provider(platform)
        record = TokenStore(self.secrets).load(platform)
        if record is None:
            return {
                "ok": False,
                "message": (
                    f"{spec.display_name} is not connected — press Connect on the "
                    "Channels page first."
                ),
            }
        if record.is_expired():
            return {
                "ok": False,
                "message": (
                    f"{spec.display_name} token expired — press Reconnect on the Channels page."
                ),
            }
        return check_health(platform, record.access_token, client=client)

    def _test_llm(self, *, client: httpx.Client | None = None) -> dict:
        import os  # noqa: PLC0415

        settings = self.load()
        key = self.get_credential("llm", "api_key") or os.environ.get("CLIPFORGE_LLM_API_KEY")
        if not key:
            return {
                "ok": False,
                "message": "No LLM API key saved — add one in Settings first.",
            }
        base = settings.llm_base_url.rstrip("/")
        own_client = client is None
        from clipforge.auth.oauth import _new_client  # noqa: PLC0415

        client = client or _new_client(15.0)
        try:
            resp = client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"Could not reach {base}: {exc}"}
        finally:
            if own_client:
                client.close()
        if resp.status_code == 200:
            return {"ok": True, "message": f"LLM reachable at {base} (models list OK)."}
        if resp.status_code in (401, 403):
            return {
                "ok": False,
                "message": f"LLM key rejected by {base} (HTTP {resp.status_code}) — check the key.",
            }
        return {
            "ok": False,
            "message": f"LLM endpoint {base} answered HTTP {resp.status_code}.",
        }


__all__ = [
    "AppSettings",
    "DEFAULT_PRIVACY",
    "LEGACY_ENV_VARS",
    "SECRET_NAMES",
    "SettingsStore",
]
