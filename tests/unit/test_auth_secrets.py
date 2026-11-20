"""Unit tests for the encrypted secret store (clipforge.auth.secrets).

The store has two backends — OS keyring first, Fernet-encrypted file
fallback — and both are exercised here with fakes, so no real keyring or
network is needed. A stored value must never be recoverable from the
bytes on disk.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from clipforge.auth.secrets import SecretStore, mask_secret


class FakeKeyring:
    """In-memory stand-in for the `keyring` module."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[f"{service}:{username}"] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get(f"{service}:{username}")

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.store[f"{service}:{username}"]
        except KeyError:
            raise Exception("not found") from None


@pytest.fixture
def file_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SecretStore:
    """SecretStore forced onto the encrypted-file backend."""
    monkeypatch.setattr(SecretStore, "_probe_keyring", staticmethod(lambda: None))
    return SecretStore(tmp_path / "data")


@pytest.fixture
def keyring_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SecretStore:
    """SecretStore forced onto a fake OS keyring backend."""
    fake = FakeKeyring()
    monkeypatch.setattr(SecretStore, "_probe_keyring", staticmethod(lambda: fake))
    store = SecretStore(tmp_path / "data")
    store._fake = fake  # type: ignore[attr-defined]
    return store


def test_mask_secret_shows_only_last_four() -> None:
    assert mask_secret("sk-abcdef123456") == "••••••••3456"
    assert mask_secret("abc") == "•••"
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_file_backend_round_trip(file_store: SecretStore) -> None:
    file_store.set("oauth_token:youtube", '{"access_token": "ya29.secret"}')
    assert file_store.get("oauth_token:youtube") == '{"access_token": "ya29.secret"}'
    assert file_store.exists("oauth_token:youtube")
    assert file_store.backend == "encrypted-file"


def test_file_backend_never_stores_plaintext(file_store: SecretStore, tmp_path: Path) -> None:
    file_store.set("app:youtube:client_secret", "TOPSECRET-value-1234")
    blob = "".join(
        p.read_bytes().decode("utf-8", "ignore")
        for p in (tmp_path / "data" / "secrets").rglob("*")
        if p.is_file()
    )
    assert "TOPSECRET-value-1234" not in blob


def test_file_backend_machine_key_is_private(file_store: SecretStore, tmp_path: Path) -> None:
    file_store.set("x", "y")
    key_path = tmp_path / "data" / "secrets" / ".machine.key"
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_file_backend_delete_and_missing(file_store: SecretStore) -> None:
    assert file_store.get("nope") is None
    assert file_store.delete("nope") is False
    file_store.set("a", "1")
    assert file_store.delete("a") is True
    assert file_store.get("a") is None


def test_file_backend_corrupt_blob_reads_as_missing(
    file_store: SecretStore, tmp_path: Path
) -> None:
    file_store.set("a", "1")
    enc = tmp_path / "data" / "secrets" / "a.enc"
    enc.write_bytes(b"not a fernet token")
    assert file_store.get("a") is None


def test_keyring_backend_round_trip(keyring_store: SecretStore) -> None:
    assert keyring_store.backend == "os-keyring"
    keyring_store.set("app:llm:api_key", "sk-test-1234")
    assert keyring_store.get("app:llm:api_key") == "sk-test-1234"
    assert keyring_store.delete("app:llm:api_key") is True
    assert keyring_store.get("app:llm:api_key") is None


def test_keyring_failure_falls_back_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyring that raises mid-flight must not lose the secret."""

    class BrokenKeyring(FakeKeyring):
        def set_password(self, service: str, username: str, password: str) -> None:
            raise RuntimeError("Secret Service unavailable")

    monkeypatch.setattr(SecretStore, "_probe_keyring", staticmethod(lambda: BrokenKeyring()))
    store = SecretStore(tmp_path / "data")
    store.set("k", "v")
    assert store.backend == "encrypted-file"
    assert store.get("k") == "v"
