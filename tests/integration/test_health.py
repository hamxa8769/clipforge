"""Regression tests for the v0.8.1 startup failure (ClipForge v0.8.2).

The desktop splash said "clipforge-server did not answer /health within
30 s" while /yt-dlp/status answered fine. Root cause: every /health call
built a fresh SecretStore, whose __init__ does a live OS-keyring probe
(read from Windows Credential Manager). A slow/hung Credential Manager
therefore hung /health — the one endpoint the desktop polls to decide
the server is up.

/health is now a pure liveness probe: a static payload computed once at
startup, no keyring / SecretStore / SettingsStore / database on any
request. These tests pin that: a hung or broken keyring must not slow
/health at all, and repeated calls must stay well under 1 s.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
import clipforge_api.app  # noqa: E402,F401  (ensures sys.modules entry exists)
from clipforge_api.app import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from clipforge import __version__  # noqa: E402

# `clipforge_api.app` the attribute is the FastAPI instance (the package
# __init__ imports it), so patch the real module object via sys.modules.
app_mod = sys.modules["clipforge_api.app"]
from clipforge.auth.secrets import SecretStore  # noqa: E402
from clipforge.auth.settings_store import SettingsStore  # noqa: E402

client = TestClient(app)


def test_health_payload_shape():
    """Field names are a contract: the Rust shell looks for "status" +
    ok and "version", the web status bar reads "dry_run"."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert isinstance(body["dry_run"], bool)
    assert isinstance(body["database_url_scheme"], str)
    assert body["database_url_scheme"]


def test_health_never_touches_keyring_or_settings(monkeypatch: pytest.MonkeyPatch):
    """A hung/broken keyring (or settings store) must not slow /health.

    Both store constructors are rigged to fail, and the real keyring
    read is rigged to hang for 5 s: if /health touched any of them the
    call would raise or blow the 1 s budget. The caches are cleared
    first so a fresh construction would actually be attempted.
    """
    app_mod._SECRETS_CACHE.clear()
    app_mod._SETTINGS_CACHE.clear()

    def _boom(*args, **kwargs):
        raise AssertionError("/health must not construct a SecretStore/SettingsStore")

    monkeypatch.setattr(SecretStore, "__init__", _boom)
    monkeypatch.setattr(SettingsStore, "__init__", _boom)
    try:
        import keyring  # noqa: PLC0415

        def _hung_get_password(*args, **kwargs):
            time.sleep(5)
            raise AssertionError("/health must not read the keyring")

        monkeypatch.setattr(keyring, "get_password", _hung_get_password)
    except ImportError:
        pass  # no keyring installed: the __init__ tripwires still apply

    start = time.perf_counter()
    response = client.get("/health")
    elapsed = time.perf_counter() - start
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert elapsed < 1.0, f"/health took {elapsed:.2f}s with a hung keyring"


def test_health_is_fast_repeatedly():
    """20 back-to-back probes (the desktop polls every 250 ms at
    startup) must each answer well under 1 s."""
    timings: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get("/health")
        timings.append(time.perf_counter() - start)
        assert response.status_code == 200
    assert max(timings) < 1.0, f"slowest /health call: {max(timings):.3f}s"
    timings.sort()
    assert timings[len(timings) // 2] < 0.1, f"median /health: {timings[10]:.3f}s"


def test_health_dry_run_follows_settings_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The cached dry_run is refreshed when settings are saved, so the
    status-bar pill stays correct without a restart."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    try:
        assert client.get("/health").json()["dry_run"] is True
        resp = client.put("/settings", json={"dry_run": False})
        assert resp.status_code == 200
        assert client.get("/health").json()["dry_run"] is False
        resp = client.put("/settings", json={"dry_run": True})
        assert resp.status_code == 200
        assert client.get("/health").json()["dry_run"] is True
    finally:
        # Restore the temp dir's cached value; the default data dir's
        # entry is untouched by this test.
        client.put("/settings", json={"dry_run": True})
