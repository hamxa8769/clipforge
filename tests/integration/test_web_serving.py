"""Serving the static web export from the real server (``clipforge_api.__main__``).

Regression test (v0.3.0): ``GET /channels/{platform}`` used to claim the
Next.js RSC payload ``/channels/index.txt`` and 404 it as an unknown
platform, so every Channels visit logged console 404s. The launcher now
serves real files under ``web/out`` before API routing, while
``GET /channels`` (no extension) still returns the API's JSON list and
``GET /channels/bogus`` still 404s from the API.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
from clipforge_api.__main__ import _build_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _launcher_client(tmp_path: Path) -> TestClient:
    web_out = tmp_path / "web" / "out"
    (web_out / "channels").mkdir(parents=True)
    (web_out / "index.html").write_text("<h1>ClipForge</h1>", encoding="utf-8")
    (web_out / "channels" / "index.html").write_text("<h1>Channels</h1>", encoding="utf-8")
    (web_out / "channels" / "index.txt").write_text("RSC payload", encoding="utf-8")
    os.environ["CLIPFORGE_WEB_OUT"] = str(web_out)
    try:
        _app, _settings, _version, web_mode = _build_app(tmp_path)
    finally:
        del os.environ["CLIPFORGE_WEB_OUT"]
    assert web_mode == "web UI: static export (web/out)"
    return TestClient(_app, raise_server_exceptions=False)


def test_web_export_served_before_api_routing(tmp_path):
    client = _launcher_client(tmp_path)
    # RSC payload for the Channels page: a real file, not a platform.
    res = client.get("/channels/index.txt")
    assert res.status_code == 200
    assert res.text == "RSC payload"
    # The API list endpoint is untouched (no file extension → API wins).
    res = client.get("/channels")
    assert res.status_code == 200
    assert isinstance(res.json(), list)
    # Unknown platforms still 404 from the API, not from the file server.
    assert client.get("/channels/bogus-platform").status_code == 404
    # The Channels page itself still serves (directory index via the mount).
    assert client.get("/channels/").status_code == 200
    # Traversal out of web/out never serves a file.
    assert client.get("/channels/..%2F..%2Findex.txt").status_code in {404, 400}
