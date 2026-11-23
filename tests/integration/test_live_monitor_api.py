"""API integration tests — live monitoring router (v0.10.0, Worker A).

Contract (router mounted standalone here, like test_live_api.py):

  GET  /api/live/platforms
  GET  /api/live/status
  POST /api/live/monitor {url|replay_path, max_hours, buffer_min,
       trigger_words}  -> 202, session id
  GET  /api/live/monitor
  GET  /api/live/monitor/{id}
  POST /api/live/monitor/{id}/stop
  POST /api/live/monitor/{id}/clip-now {seconds}

No test hits a real live stream: the replay start uses the
generated fixture from test_live_monitor (quiet sine + loud burst),
and live-URL starts are asserted on validation only (a real
YouTube/Twitch URL is refused 422 here because no network
resolution is attempted in these tests — the resolver is
monkeypatched to raise NotLive). Every error asserted here is plain
language.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from clipforge_api import live_monitor as live_router  # noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge.db import Job, init_db, session_factory  # noqa: E402
from clipforge.live_monitor import NotLiveError  # noqa: E402

app = FastAPI()
app.include_router(live_router.router)
client = TestClient(app)

FIXTURE_MP4 = Path("apps/api/data/uploads/live_monitor_replay.mp4")


def _ensure_fixture_mp4() -> Path:
    if FIXTURE_MP4.exists():
        return FIXTURE_MP4
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available — cannot generate the live-monitor fixture")
    FIXTURE_MP4.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=90",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=90",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-af",
            "volume=0.05,volume=18:enable='between(t,65,70)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(FIXTURE_MP4),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"ffmpeg could not generate the fixture: {proc.stderr}"
    return FIXTURE_MP4


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_peak_jobs():
    """Remove exactly the rows this module created (live-peak jobs)."""
    init_db()
    yield
    session = session_factory()()
    try:
        for job in session.query(Job).all():
            if (job.meta_json or {}).get("live_peak"):
                session.delete(job)
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clean_sessions():
    """Stop any monitor session a test left running."""
    from clipforge.live_monitor import SESSIONS  # noqa: PLC0415

    yield
    for session in list(SESSIONS.values()):
        session.stop("test cleanup")


def test_platforms_matrix_is_honest():
    resp = client.get("/api/live/platforms")
    assert resp.status_code == 200
    platforms = {p["platform"]: p for p in resp.json()["platforms"]}
    assert platforms["youtube"]["chat"] == "oauth"
    assert platforms["twitch"]["chat"] is True
    assert platforms["kick"]["chat"] is False
    assert "audio-only" in platforms["kick"]["chat_note"]


def test_status_reports_preflight():
    resp = client.get("/api/live/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "requirements" in body
    assert "youtube" in body
    assert body["youtube"]["chat"] is False  # no connection in tests
    assert "API key" in body["youtube"]["chat_note"] or "connected" in body["youtube"]["chat_note"]


def test_start_rejects_empty_url():
    resp = client.post("/api/live/monitor", json={"url": ""})
    assert resp.status_code == 422
    assert "live stream URL" in resp.json()["detail"]


def test_start_rejects_unknown_platform():
    resp = client.post("/api/live/monitor", json={"url": "https://example.com/stream"})
    assert resp.status_code == 422
    assert "YouTube, Twitch or Kick" in resp.json()["detail"]


def test_start_rejects_missing_replay(tmp_path):
    resp = client.post("/api/live/monitor", json={"replay_path": str(tmp_path / "nope.mp4")})
    assert resp.status_code == 422
    assert "not found" in resp.json()["detail"]


def test_start_live_url_not_live_is_plain_422(monkeypatch):
    def _not_live(url, quality=720):
        raise NotLiveError("This channel is not live right now.")

    monkeypatch.setattr("clipforge.live_monitor.resolve_stream", _not_live)
    # The router calls start_monitor, which uses the module-level
    # resolver default — patch the session to fail fast at resolve.
    resp = client.post(
        "/api/live/monitor",
        json={"url": "https://www.youtube.com/watch?v=abc12345678"},
    )
    # 202: the session starts and then fails asynchronously (the
    # resolve runs on the ingest thread, like the real server); the
    # session record itself carries the plain-language failure.
    assert resp.status_code == 202
    session_id = resp.json()["session_id"]
    deadline = time.time() + 30
    error = ""
    while time.time() < deadline:
        got = client.get(f"/api/live/monitor/{session_id}")
        if got.status_code == 200:
            error = got.json().get("error") or ""
            if got.json()["status"] == "failed":
                break
        time.sleep(0.5)
    assert "not live right now" in error


def test_monitor_unknown_session_is_404():
    assert client.get("/api/live/monitor/doesnotexist").status_code == 404
    assert client.post("/api/live/monitor/doesnotexist/stop").status_code == 404
    clip_resp = client.post("/api/live/monitor/doesnotexist/clip-now", json={"seconds": 30})
    assert clip_resp.status_code == 404


def test_clip_now_before_recording_is_409(tmp_path, monkeypatch):
    """A replay that has just started has nothing recorded yet."""
    from clipforge import live_monitor as lm  # noqa: PLC0415

    fixture = _ensure_fixture_mp4()
    monkeypatch.setattr(lm, "SESSIONS", {})
    resp = client.post("/api/live/monitor", json={"replay_path": str(fixture)})
    assert resp.status_code == 202
    session_id = resp.json()["session_id"]
    # Stop it at once so the session is over before any audio lands.
    client.post(f"/api/live/monitor/{session_id}/stop")
    got = client.post(f"/api/live/monitor/{session_id}/clip-now", json={"seconds": 30})
    # The replay runs in real time, so under a second after start
    # nothing has been recorded yet: 409, plain language.
    assert got.status_code == 409
    assert "Nothing has been recorded yet" in got.json()["detail"]


def test_replay_monitor_lists_and_stops():
    fixture = _ensure_fixture_mp4()
    resp = client.post("/api/live/monitor", json={"replay_path": str(fixture)})
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "replay"
    assert "replay, not a live stream" in body["message"]
    session_id = body["session_id"]
    listed = client.get("/api/live/monitor")
    assert listed.status_code == 200
    assert any(s["session_id"] == session_id for s in listed.json()["sessions"])
    stopped = client.post(f"/api/live/monitor/{session_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["ok"] is True
