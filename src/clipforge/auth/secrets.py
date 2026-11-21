"""Secret storage for ClipForge v0.4.0 — OS keyring first, encrypted file fallback.

Design borrowed (not code) from artbyjazi/autoclip (MIT): secrets go into
the OS keyring — Credential Manager on Windows, Keychain on macOS, Secret
Service on Linux — never into a config file. When no working keyring backend
exists, ClipForge falls back to an encrypted file in the app-data dir and
says so (the Settings page shows which backend is active).

Encryption for the file fallback is Fernet (AES-128-CBC + HMAC-SHA256,
via ``cryptography``). The machine key is a random 32-byte key stored in
``<data_dir>/secrets/.machine.key`` with 0600 permissions; it is never
committed (``*.key`` is gitignored) and never logged. OAuth tokens and app
credentials are stored through this module only — never in the SQLite
database, never in ``settings.json``, never in the repo.

``keyring`` is an optional runtime dependency: if it cannot be imported, or
its backend raises on a probe read, the encrypted-file backend is used.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_KEYRING_SERVICE = "clipforge"

_NAME_OK = re.compile(r"[^A-Za-z0-9_:\-.]")


def mask_secret(value: str | None) -> str:
    """Masked display for a secret: only the last 4 characters ever show."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * 8 + value[-4:]


def _sanitize_name(name: str) -> str:
    return _NAME_OK.sub("_", name)


class SecretStore:
    """One secret backend: OS keyring when it works, encrypted file otherwise."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.secrets_dir = self.data_dir / "secrets"
        self._keyring = self._probe_keyring()
        self._fernet = None  # lazy: only built for the file backend

    # ------------------------------------------------------------ backend
    @staticmethod
    def _probe_keyring():
        """Return a working keyring module, or None if keyring is unusable."""
        try:
            import keyring  # noqa: PLC0415  (optional dependency)
            import keyring.backends.fail  # noqa: PLC0415
        except ImportError:
            return None
        try:
            backend = keyring.get_keyring()
        except Exception:  # keyring misconfigured — fall back to file
            return None
        if isinstance(backend, keyring.backends.fail.Keyring):
            return None
        try:  # probe read must not raise (headless Secret Service does)
            keyring.get_password(_KEYRING_SERVICE, "__clipforge_probe__")
        except Exception:
            return None
        return keyring

    @property
    def backend(self) -> str:
        """Which backend is active: ``os-keyring`` or ``encrypted-file``."""
        return "os-keyring" if self._keyring is not None else "encrypted-file"

    # ------------------------------------------------------------ file backend
    def _get_fernet(self):
        """Fernet instance keyed by the machine key (created once, 0600)."""
        if self._fernet is not None:
            return self._fernet
        from cryptography.fernet import Fernet  # noqa: PLC0415

        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        key_path = self.secrets_dir / ".machine.key"
        try:
            raw = key_path.read_bytes()
            key = raw.strip()
            Fernet(key)  # validate
        except Exception:
            key = Fernet.generate_key()
            key_path.write_bytes(key)
            try:
                os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Windows has no POSIX modes; ACLs are out of scope
        self._fernet = Fernet(key)
        return self._fernet

    def _secret_path(self, name: str) -> Path:
        return self.secrets_dir / f"{_sanitize_name(name)}.enc"

    # ------------------------------------------------------------ API
    def set(self, name: str, value: str) -> None:
        """Store a secret value (never logged, never returned by any API)."""
        if self._keyring is not None:
            try:
                self._keyring.set_password(_KEYRING_SERVICE, name, value)
                return
            except Exception:
                self._keyring = None  # keyring broke mid-flight; use file
        fernet = self._get_fernet()
        path = self._secret_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fernet.encrypt(value.encode("utf-8")))
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def get(self, name: str) -> str | None:
        """Return a secret value, or None when it was never stored."""
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(_KEYRING_SERVICE, name)
                if value is not None:
                    return value
            except Exception:
                self._keyring = None
        path = self._secret_path(name)
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        try:
            return self._get_fernet().decrypt(blob).decode("utf-8")
        except Exception:
            return None  # corrupt / wrong machine key: treat as missing

    def delete(self, name: str) -> bool:
        """Delete a secret. True when something was removed."""
        removed = False
        if self._keyring is not None:
            try:
                self._keyring.delete_password(_KEYRING_SERVICE, name)
                removed = True
            except Exception:
                pass
        path = self._secret_path(name)
        try:
            path.unlink()
            removed = True
        except OSError:
            pass
        return removed

    def exists(self, name: str) -> bool:
        return self.get(name) is not None


__all__ = ["SecretStore", "mask_secret"]
