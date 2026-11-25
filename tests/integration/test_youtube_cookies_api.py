"""YouTube browser-cookies plumbing (v0.11.1): the Settings default,
the per-job override on POST /jobs, the retry-with-cookies query
param, and the cookies test endpoint — end to end through the API.

Only browser *names* are ever sent or stored; no test here (or in
the app) ever handles a cookie value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from clipforge_api.app import app
from fastapi.testclient import TestClient

from clipforge.db import Job, get_session

YOUTUBE_URL = "https://www.youtube.com/watch?v=M7lc1UVf-VE"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the API's data dir at a throwaway folder (no repo writes)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _job_meta(job_id: str) -> dict:
    session = get_session()
    try:
        job = session.query(Job).filter_by(id=job_id).one()
        return dict(job.meta_json or {})
    finally:
        session.close()


def test_settings_cookies_browser_round_trip(client: TestClient) -> None:
    """GET /settings exposes the field ("" by default); PUT saves a
    known browser and the next GET shows it."""
    assert client.get("/settings").json()["youtube_cookies_browser"] == ""
    resp = client.put("/settings", json={"youtube_cookies_browser": "chrome"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["youtube_cookies_browser"] == "chrome"
    assert client.get("/settings").json()["youtube_cookies_browser"] == "chrome"
    # Off again.
    resp = client.put("/settings", json={"youtube_cookies_browser": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["youtube_cookies_browser"] == ""


def test_settings_rejects_unknown_cookies_browser(client: TestClient) -> None:
    """An unknown browser is a 422, never silently stored."""
    resp = client.put("/settings", json={"youtube_cookies_browser": "safari"})
    assert resp.status_code == 422, resp.text
    assert client.get("/settings").json()["youtube_cookies_browser"] == ""


def test_job_create_stores_per_job_cookies_override(client: TestClient) -> None:
    """POST /jobs with youtube_cookies_browser stores the override in
    job meta (the worker prefers it over the Settings default)."""
    resp = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": YOUTUBE_URL,
            "own_content_declaration": True,
            "youtube_cookies_browser": "edge",
        },
    )
    assert resp.status_code == 201, resp.text
    assert _job_meta(resp.json()["id"])["youtube_cookies_browser"] == "edge"


def test_job_create_rejects_unknown_cookies_browser(client: TestClient) -> None:
    resp = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": YOUTUBE_URL,
            "own_content_declaration": True,
            "youtube_cookies_browser": "safari",
        },
    )
    assert resp.status_code == 422, resp.text


def test_retry_with_cookies_sets_the_override(client: TestClient) -> None:
    """POST /jobs/{id}/retry?youtube_cookies_browser=chrome on a
    failed job stores the per-job override for the new run (and the
    retry keeps it in meta instead of dropping it)."""
    created = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": YOUTUBE_URL,
            "own_content_declaration": True,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    session = get_session()
    try:
        job = session.query(Job).filter_by(id=job_id).one()
        job.status = "failed"
        job.error = "YouTube asked to sign in"
        session.commit()
    finally:
        session.close()
    resp = client.post(f"/jobs/{job_id}/retry?youtube_cookies_browser=chrome&wait=false")
    assert resp.status_code in {200, 202}, resp.text
    assert _job_meta(job_id)["youtube_cookies_browser"] == "chrome"


def test_retry_rejects_unknown_cookies_browser(client: TestClient) -> None:
    created = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": YOUTUBE_URL,
            "own_content_declaration": True,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    session = get_session()
    try:
        job = session.query(Job).filter_by(id=job_id).one()
        job.status = "failed"
        session.commit()
    finally:
        session.close()
    resp = client.post(f"/jobs/{job_id}/retry?youtube_cookies_browser=safari&wait=false")
    assert resp.status_code == 422, resp.text


def test_cookies_test_endpoint_needs_a_browser(client: TestClient) -> None:
    """No browser picked → a plain not-ok result, not a 500."""
    resp = client.post("/settings/test-youtube-cookies", json={"browser": ""})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["code"] == "no_browser"


def test_cookies_test_endpoint_rejects_non_youtube_url(client: TestClient) -> None:
    resp = client.post(
        "/settings/test-youtube-cookies",
        json={"browser": "chrome", "url": "https://example.com/watch?v=x"},
    )
    assert resp.status_code == 422, resp.text
