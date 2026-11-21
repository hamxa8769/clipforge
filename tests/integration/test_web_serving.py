"""Serving the static web export from the real server (``clipforge_api.__main__``).

Regression test (v0.3.0): ``GET /channels/{platform}`` used to claim the
Next.js RSC payload ``/channels/index.txt`` and 404 it as an unknown
platform, so every Channels visit logged console 404s. The launcher now
serves real files under ``web/out`` before API routing, while
``GET /channels`` (no extension) still returns the API's JSON list and
``GET /channels/bogus`` still 404s from the API.

Regression test (v0.4.1): a full-page browser navigation to an
extensionless UI route — clicking "Connect a channel" (a plain ``<a>``,
so a full navigation, not a client-side one) — used to fall through to
the API routes and show raw JSON: ``GET /channels/connect`` answered
``{"detail": "unknown platform: connect"}`` and ``GET /channels``,
``/settings``, ``/jobs``, ``/monetization`` and ``/jobs/view`` answered
their JSON payloads. The launcher now serves the exported page for a
real browser navigation (``Accept: text/html``) to a route that has an
exported ``index.html``, while API fetches (``Accept: */*``) still get
JSON.
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
    (web_out / "channels" / "connect").mkdir(parents=True)
    (web_out / "settings").mkdir(parents=True)
    (web_out / "jobs" / "view").mkdir(parents=True)
    (web_out / "index.html").write_text("<h1>ClipForge</h1>", encoding="utf-8")
    (web_out / "channels" / "index.html").write_text("<h1>Channels</h1>", encoding="utf-8")
    (web_out / "channels" / "index.txt").write_text("RSC payload", encoding="utf-8")
    (web_out / "channels" / "connect" / "index.html").write_text(
        "<h1>Connect a channel</h1>", encoding="utf-8"
    )
    (web_out / "settings" / "index.html").write_text("<h1>Settings</h1>", encoding="utf-8")
    (web_out / "jobs" / "view" / "index.html").write_text("<h1>Job</h1>", encoding="utf-8")
    os.environ["CLIPFORGE_WEB_OUT"] = str(web_out)
    try:
        _app, _settings, _version, web_mode = _build_app(tmp_path)
    finally:
        del os.environ["CLIPFORGE_WEB_OUT"]
    assert web_mode == "web UI: static export (web/out)"
    return TestClient(_app, raise_server_exceptions=False)


# What a real browser sends on a full-page navigation (link click,
# refresh, pasted URL). The web UI's own fetch() calls send */* instead.
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


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


def test_browser_navigation_to_ui_routes_serves_html_not_json(tmp_path):
    """v0.4.1: full-page loads of UI routes serve the exported page.

    Clicking "Connect a channel" (Dashboard / Channels) is a plain
    <a>, so the browser navigates to /channels/connect and used to
    get {"detail": "unknown platform: connect"} from the API.
    """
    client = _launcher_client(tmp_path)
    for path, marker in [
        ("/channels", "<h1>Channels</h1>"),
        ("/channels/connect", "<h1>Connect a channel</h1>"),
        ("/channels/connect?platform=youtube", "<h1>Connect a channel</h1>"),
        ("/settings", "<h1>Settings</h1>"),
        ("/jobs/view", "<h1>Job</h1>"),
    ]:
        res = client.get(path, headers={"Accept": BROWSER_ACCEPT})
        assert res.status_code == 200, path
        assert "text/html" in res.headers["content-type"], path
        assert marker in res.text, path
    # Trailing-slash navigations serve the same page.
    res = client.get("/channels/connect/", headers={"Accept": BROWSER_ACCEPT})
    assert res.status_code == 200
    assert "<h1>Connect a channel</h1>" in res.text


def test_api_fetches_still_get_json_when_ui_route_exists(tmp_path):
    """v0.4.1: the navigation fix must not break the UI's own fetches.

    The web UI calls GET /channels etc. with fetch(), whose default
    Accept is */* — those must still return JSON, not HTML.
    """
    client = _launcher_client(tmp_path)
    res = client.get("/channels", headers={"Accept": "*/*"})
    assert res.status_code == 200
    assert isinstance(res.json(), list)
    res = client.get("/settings", headers={"Accept": "*/*"})
    assert res.status_code == 200
    assert isinstance(res.json(), dict)


def test_browser_navigation_to_unknown_platform_still_404s(tmp_path):
    """v0.4.1: no exported page exists for a bogus platform, so the
    request still falls through to the API, which 404s it."""
    client = _launcher_client(tmp_path)
    res = client.get("/channels/bogus-platform", headers={"Accept": BROWSER_ACCEPT})
    assert res.status_code == 404
