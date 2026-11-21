"""Integration tests for the ClipForge Pro (v0.8.0) endpoints.

Detection / perf / batch / X / monetization-revenue / publish-scan,
plus the X provider wiring (channels card, connect guide, OAuth
callback page). Uses FastAPI's TestClient against the real app with
an isolated data dir; no network calls are made.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from clipforge_api.app import app
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _segments() -> list[dict]:
    return [
        {
            "start": 0.0,
            "end": 3.0,
            "text": "Did you know this one trick changes everything?",
            "energy": 0.9,
        },
        {"start": 3.0, "end": 12.0, "text": "Here is the secret step by step.", "energy": 0.7},
        {"start": 12.0, "end": 20.0, "text": "So that is why it works every time.", "energy": 0.5},
    ]


def test_detection_accepts_string_niche(client: TestClient) -> None:
    resp = client.post(
        "/detection/analyze",
        json={"segments": _segments(), "duration": 20.0, "niche": "tech"},
    )
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["candidates"]
    assert candidates, "expected at least one candidate"
    top = candidates[0]
    assert 0 <= top["score"] <= 100
    assert top["start"] < top["end"]


def test_detection_rejects_empty_segments(client: TestClient) -> None:
    resp = client.post("/detection/analyze", json={"segments": [], "duration": 10})
    assert resp.status_code == 422


def test_perf_profile_round_trip(client: TestClient) -> None:
    resp = client.get("/perf/profile")
    assert resp.status_code == 200
    assert resp.json()["profile"] in ("low", "balanced", "high")
    assert resp.json()["settings"]["whisper_model"] in ("tiny", "base")
    resp = client.post("/perf/profile", json={"profile": "balanced"})
    assert resp.status_code == 200
    assert resp.json()["profile"] == "balanced"
    resp = client.post("/perf/profile", json={"profile": "ultra"})
    assert resp.status_code == 422


def test_batch_bad_source_fails_plainly(client: TestClient) -> None:
    resp = client.post("/batch", json={"sources": ["/nonexistent/video.mp4"]})
    assert resp.status_code == 202, resp.text
    batch_id = resp.json()["batch_id"]
    for _ in range(50):
        got = client.get(f"/batch/{batch_id}")
        assert got.status_code == 200
        item = got.json()["items"][0]
        if item["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert item["state"] == "failed"
    assert "not found" in item["error"].lower()
    assert "traceback" not in item["error"].lower()


def test_batch_youtube_needs_declaration(client: TestClient) -> None:
    resp = client.post("/batch", json={"sources": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]})
    assert resp.status_code == 422


def test_x_status_and_connect_honest(client: TestClient) -> None:
    resp = client.get("/x/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False
    assert "paid" in resp.json()["note"].lower()
    resp = client.post("/x/connect")
    assert resp.status_code == 422
    assert "developer.x.com" in resp.json()["detail"]


def test_x_guide_and_channels_card(client: TestClient) -> None:
    resp = client.get("/connect/guide/x")
    assert resp.status_code == 200
    assert resp.json()["platform"] == "x"
    resp = client.get("/channels")
    assert resp.status_code == 200
    cards = {c["platform"]: c for c in resp.json()}
    assert "x" in cards
    assert cards["x"]["status"] == "not_connected"
    assert cards["x"]["daily_cap"] > 0


def test_x_callback_without_code_is_html(client: TestClient) -> None:
    resp = client.get("/auth/callback/x")
    assert resp.status_code in (200, 400)
    assert "text/html" in resp.headers["content-type"]
    assert "Connection failed" in resp.text


def test_monetization_revenue_honest_empty(client: TestClient) -> None:
    resp = client.get("/monetization/revenue")
    assert resp.status_code == 200
    rows = {r["platform"]: r for r in resp.json()["platforms"]}
    assert rows["youtube"]["earnings"] is None
    assert "not reported" in rows["youtube"]["basis"]


def test_publish_scan_unknown_clip_404(client: TestClient) -> None:
    resp = client.post("/publish/scan", json={"clip_id": "nope"})
    assert resp.status_code == 404
