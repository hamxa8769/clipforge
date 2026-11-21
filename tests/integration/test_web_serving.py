"""Serving the static web export from the real server (``clipforge_api.__main__``).

Regression test (v0.3.0): ``GET /channels/{platform}`` used to claim the
Next.js RSC payload ``/channels/index.txt`` and 404 it as an unknown
platform, so every Channels visit logged console 404s. The launcher now
serves real files under ``web/out`` before API routing, while
``GET /channels`` (no extension) still returns the API's JSON list and
``GET /channels/bogus`` still 404s from the API.

Regression test (v0.6.0): UI-shaped paths with no exported page used to
fall through to the API and show raw JSON on a full-page load — ``GET
/jobs`` answered the job list (``[]``), ``GET /channels/youtube`` the
channel-status JSON, ``GET /posts`` the post list, ``GET
/auth/youtube/start`` a 409 JSON, and ``GET /jobs/<live-id>/candidates``
a 404 JSON. A browser navigation to each now redirects (303) to the real
page that answers it, while API fetches (``Accept: */*``) still get JSON
and unknown platforms still 404 from the API.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
from clipforge_api.__main__ import _build_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Every route the static export emits (apps/web/app/**), plus one demo
# job id (generateStaticParams lists the demo ids only). The fixture
# writes one index.html per route so the audit covers the whole UI.
_EXPORTED_PAGES = {
    "": "<h1>ClipForge</h1>",
    "pipeline": "<h1>Pipeline</h1>",
    "agent": "<h1>Agent</h1>",
    "automation": "<h1>Automation</h1>",
    "channels": "<h1>Channels</h1>",
    "channels/connect": "<h1>Connect a channel</h1>",
    "queue": "<h1>Approval Queue</h1>",
    "schedule": "<h1>Schedule</h1>",
    "monetization": "<h1>Monetization</h1>",
    "settings": "<h1>Settings</h1>",
    "setup": "<h1>Setup</h1>",
    "jobs/view": "<h1>Job</h1>",
    "jobs/job-001": "<h1>Job job-001</h1>",
}

KNOWN_PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")


def _launcher_client(tmp_path: Path) -> TestClient:
    web_out = tmp_path / "web" / "out"
    for route, marker in _EXPORTED_PAGES.items():
        (web_out / route).mkdir(parents=True, exist_ok=True)
        (web_out / route / "index.html").write_text(marker, encoding="utf-8")
    (web_out / "channels" / "index.txt").write_text("RSC payload", encoding="utf-8")
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


def test_browser_navigation_to_live_job_id_redirects_to_review(tmp_path):
    """v0.5.0: a live job id has no exported directory (only demo ids
    are generated at build time), so a direct load / refresh of
    /jobs/<live-id> used to fall through to the API and show raw JSON.
    A browser navigation now redirects to the review page that reads
    the id from the query string. API fetches (Accept: */*) are
    unaffected and still get the job JSON."""
    client = _launcher_client(tmp_path)
    live_id = "262819a0612c4368"
    res = client.get(f"/jobs/{live_id}", headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False)
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"/jobs/view/?id={live_id}"
    # Following the redirect lands on the exported review page.
    res = client.get(res.headers["location"], headers={"Accept": BROWSER_ACCEPT})
    assert res.status_code == 200
    assert "<h1>Job</h1>" in res.text
    # The API still answers JSON for the same id when asked as an API.
    res = client.get(f"/jobs/{live_id}", headers={"Accept": "*/*"})
    assert res.status_code in {200, 404}  # 200 when the job exists, else API 404
    assert "text/html" not in res.headers.get("content-type", "")


def test_browser_navigation_to_every_exported_route_serves_html(tmp_path):
    """v0.6.0: full audit — every route the static export emits serves
    its page (200 text/html) on a browser navigation, including the
    v0.5.0 pages (agent / pipeline / setup) and trailing-slash loads."""
    client = _launcher_client(tmp_path)
    for route, marker in _EXPORTED_PAGES.items():
        path = "/" + route
        res = client.get(path, headers={"Accept": BROWSER_ACCEPT})
        assert res.status_code == 200, path
        assert "text/html" in res.headers["content-type"], path
        assert marker in res.text, path
    # Trailing-slash navigations serve the same page.
    for path, marker in [
        ("/pipeline/", "<h1>Pipeline</h1>"),
        ("/agent/", "<h1>Agent</h1>"),
        ("/setup/", "<h1>Setup</h1>"),
        ("/jobs/job-001/", "<h1>Job job-001</h1>"),
    ]:
        res = client.get(path, headers={"Accept": BROWSER_ACCEPT})
        assert res.status_code == 200, path
        assert marker in res.text, path


def test_browser_navigation_to_jobs_list_redirects_to_automation(tmp_path):
    """v0.6.0: no /jobs list page exists, yet the Pipeline page links
    "Go to Jobs" here — a direct load used to answer GET /jobs, raw
    JSON ([]). A browser navigation now redirects to Automation, the
    page that lists jobs. API fetches still get the JSON list."""
    client = _launcher_client(tmp_path)
    for path in ("/jobs", "/jobs/"):
        res = client.get(path, headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False)
        assert res.status_code == 303, path
        assert res.headers["location"] == "/automation", path
        res = client.get(path, headers={"Accept": BROWSER_ACCEPT})
        assert res.status_code == 200
        assert "<h1>Automation</h1>" in res.text
    res = client.get("/jobs", headers={"Accept": "*/*"})
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_browser_navigation_to_channel_platform_redirects_to_connect(tmp_path):
    """v0.6.0: /channels/<platform> has no exported page, so a direct
    load used to answer the channel-status JSON. A browser navigation
    now redirects to the Connect wizard with the platform preselected.
    API fetches still get the channel JSON; unknown platforms still 404."""
    client = _launcher_client(tmp_path)
    for platform in KNOWN_PLATFORMS:
        res = client.get(
            f"/channels/{platform}", headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False
        )
        assert res.status_code == 303, platform
        assert res.headers["location"] == f"/channels/connect?platform={platform}", platform
        res = client.get(f"/channels/{platform}", headers={"Accept": BROWSER_ACCEPT})
        assert res.status_code == 200
        assert "<h1>Connect a channel</h1>" in res.text
        # The API still answers JSON for the same path when asked as an API.
        res = client.get(f"/channels/{platform}", headers={"Accept": "*/*"})
        assert res.status_code == 200
        assert res.json()["platform"] == platform
    # /channels/connect/<platform> and deeper platform paths, same fix.
    res = client.get("/channels/connect/youtube", headers={"Accept": BROWSER_ACCEPT})
    assert res.status_code == 200
    assert "<h1>Connect a channel</h1>" in res.text
    res = client.get(
        "/channels/connect/bogus", headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/channels/connect"


def test_browser_navigation_to_deeper_job_paths_redirects_to_review(tmp_path):
    """v0.6.0: /jobs/<live-id>/candidates (and /logs, ...) are API
    shapes, but a browser landing on one used to see raw JSON. It now
    redirects to the same review page as a bare /jobs/<live-id> load."""
    client = _launcher_client(tmp_path)
    live_id = "262819a0612c4368"
    for path in (f"/jobs/{live_id}/candidates", f"/jobs/{live_id}/logs"):
        res = client.get(path, headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False)
        assert res.status_code == 303, path
        assert res.headers["location"] == f"/jobs/view/?id={live_id}", path
    # /jobs/view/<anything> has no page either — back to the review page.
    res = client.get("/jobs/view/extra", headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/jobs/view/"
    # API fetches are unaffected.
    res = client.get(f"/jobs/{live_id}/candidates", headers={"Accept": "*/*"})
    assert res.status_code == 404
    assert "text/html" not in res.headers.get("content-type", "")


def test_browser_navigation_to_posts_redirects_to_queue(tmp_path):
    """v0.6.0: no /posts page exists; a direct load used to answer the
    post list (or a 404 JSON for one id). A browser navigation now
    redirects to the approval queue, which lists posts. API fetches
    still get JSON."""
    client = _launcher_client(tmp_path)
    for path in ("/posts", "/posts/", "/posts/abc123"):
        res = client.get(path, headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False)
        assert res.status_code == 303, path
        assert res.headers["location"] == "/queue", path
    res = client.get("/posts", headers={"Accept": BROWSER_ACCEPT})
    assert "<h1>Approval Queue</h1>" in res.text
    res = client.get("/posts", headers={"Accept": "*/*"})
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_browser_navigation_to_auth_start_redirects_to_connect(tmp_path):
    """v0.6.0: /auth/<platform>/start is a fetch-only API endpoint; a
    browser landing on it used to see raw JSON (or a 409). It now
    redirects to the Connect wizard, the page that starts the flow.
    /auth/callback/* is untouched — it already answers HTML."""
    client = _launcher_client(tmp_path)
    for platform in KNOWN_PLATFORMS:
        res = client.get(
            f"/auth/{platform}/start", headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False
        )
        assert res.status_code == 303, platform
        assert res.headers["location"] == f"/channels/connect?platform={platform}", platform
    # Unknown platform: still the API 404, not a redirect.
    res = client.get("/auth/bogus/start", headers={"Accept": BROWSER_ACCEPT})
    assert res.status_code == 404
    # API fetches still get the JSON error (no credentials configured).
    res = client.get("/auth/youtube/start", headers={"Accept": "*/*"})
    assert res.status_code == 409
    assert "text/html" not in res.headers.get("content-type", "")
