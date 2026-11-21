"""Integration tests for the v0.4.0 guided-connect / onboarding / FFmpeg work.

Covers GET /connect/guide/{platform} for all four platforms, the
GET/PUT /onboarding round-trip, plain-language test-connection failures,
the versioned /health response, and jobs blocked/failed by a missing
FFmpeg with a clear install link instead of a silent nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from clipforge_api.app import app
from fastapi.testclient import TestClient

from clipforge.workflow import FFMPEG_DOWNLOAD_URL, ffmpeg_missing_message

PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")
REVIEW_KEYWORDS = {
    "youtube": "Google",
    "instagram": "Meta",
    "facebook": "Meta",
    "tiktok": "audit",
}

TRANSCRIPT_TEXT = (
    "First, drink water before coffee. Second, write down your top three goals. "
    "Third, move your body for ten minutes. It sounds simple, but the results are "
    "incredible and honestly shocking. Most people fail because they try to change "
    "everything at once, and that is the worst approach. Instead, focus on one small "
    "win, then build on it. This is the best advice I can give you, and it works. "
    "Why does it work? Because small wins create momentum, and momentum creates "
    "habits. Let me show you how to save time, learn faster, and build a routine "
    "that actually lasts. Watch this trick and try it tomorrow morning. "
    "You will love the difference, I promise, and your future self will thank you."
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the API's data dir at a throwaway folder (no repo writes)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    for var in (
        "YOUTUBE_REFRESH_TOKEN",
        "META_ACCESS_TOKEN",
        "TIKTOK_ACCESS_TOKEN",
        "CLIPFORGE_LLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(scope="module")
def test_video(tmp_path_factory):
    """A short test video (testsrc, 320x240) for real ingest."""
    path = tmp_path_factory.mktemp("clipforge-connect-guide") / "test5.mp4"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=5:size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "10",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not path.exists():
        pytest.skip(f"ffmpeg could not generate a test video: {proc.stderr}")
    return str(path)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ------------------------------------------------------------------ connect guide
@pytest.mark.parametrize("platform", PLATFORMS)
def test_connect_guide_for_every_platform(client: TestClient, platform: str) -> None:
    resp = client.get(f"/connect/guide/{platform}")
    assert resp.status_code == 200, resp.text
    guide = resp.json()
    assert guide["platform"] == platform
    assert guide["display_name"]
    # Official developer console, not a guess.
    assert guide["console_url"].startswith("https://"), platform
    assert guide["console_name"], platform
    # Numbered steps, in order, with real content.
    steps = guide["steps"]
    assert len(steps) >= 5, platform
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    assert all(s["title"] and s["detail"] for s in steps), platform
    # The exact redirect URI the running server expects.
    assert guide["redirect_uri"].endswith(f"/auth/callback/{platform}"), platform
    assert guide["redirect_uri"].startswith("http://127.0.0.1:8000"), platform
    # Scopes, review note, fallback and daily cap all come from the spec.
    assert guide["scopes"], platform
    assert REVIEW_KEYWORDS[platform] in guide["review_notice"], platform
    assert guide["fallback"], platform
    assert guide["daily_cap"] > 0, platform
    # Credential presence flags only — never a value.
    creds = {c["key"]: c for c in guide["credentials"]}
    assert set(creds) == {"client_id", "client_secret"}, platform
    assert all(c["saved"] is False for c in creds.values()), platform
    assert all(c["label"] and c["where"] for c in creds.values()), platform
    assert all("value" not in c for c in creds.values()), platform


def test_connect_guide_unknown_platform_is_404(client: TestClient) -> None:
    assert client.get("/connect/guide/myspace").status_code == 404


def test_connect_guide_marks_saved_credentials(client: TestClient) -> None:
    client.put(
        "/settings",
        json={
            "credentials": {
                "tiktok": {"client_id": "ck-value-1234", "client_secret": "cs-value-5678"}
            }
        },
    )
    guide = client.get("/connect/guide/tiktok").json()
    creds = {c["key"]: c for c in guide["credentials"]}
    assert creds["client_id"]["saved"] is True
    assert creds["client_secret"]["saved"] is True
    assert "ck-value-1234" not in str(guide) and "cs-value-5678" not in str(guide)
    # The other platforms are still unsaved.
    assert all(
        c["saved"] is False for c in client.get("/connect/guide/youtube").json()["credentials"]
    )


def test_channel_cards_link_to_the_guide(client: TestClient) -> None:
    for card in client.get("/channels").json():
        assert card["connect_guide"] == f"/connect/guide/{card['platform']}"


# ------------------------------------------------------------------ onboarding
def test_onboarding_round_trip(client: TestClient) -> None:
    first = client.get("/onboarding")
    assert first.status_code == 200
    assert first.json() == {
        "ffmpeg_acknowledged": False,
        "llm": "",
        "first_channel_connected": False,
        "sample_workflow_run": False,
        "completed": False,
    }

    # Partial updates keep the other fields.
    updated = client.put("/onboarding", json={"ffmpeg_acknowledged": True, "llm": "skipped"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["ffmpeg_acknowledged"] is True
    assert updated.json()["llm"] == "skipped"
    assert updated.json()["first_channel_connected"] is False

    # Resume: GET returns the stored progress.
    assert client.get("/onboarding").json()["ffmpeg_acknowledged"] is True

    # Finish the wizard.
    done = client.put(
        "/onboarding",
        json={"first_channel_connected": True, "sample_workflow_run": True, "completed": True},
    )
    assert done.status_code == 200
    assert done.json()["completed"] is True
    assert client.get("/onboarding").json()["completed"] is True


def test_onboarding_rejects_bad_values(client: TestClient) -> None:
    assert client.put("/onboarding", json={"llm": "maybe"}).status_code == 422
    assert client.put("/onboarding", json={"llm": 42}).status_code == 422
    assert client.put("/onboarding", json={"ffmpeg_acknowledged": ["yes"]}).status_code == 422
    assert client.put("/onboarding", json={"surprise_field": True}).status_code == 422
    # Nothing was stored by the rejected updates.
    assert client.get("/onboarding").json()["llm"] == ""


def test_oauth_connect_marks_onboarding_channel_step(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real OAuth connect flips first_channel_connected by itself."""
    import sys

    app_mod = sys.modules["clipforge_api.app"]
    client.put(
        "/settings",
        json={"credentials": {"youtube": {"client_id": "cid", "client_secret": "csec"}}},
    )
    monkeypatch.setattr(
        app_mod,
        "exchange_code",
        lambda platform, code, **kwargs: {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        app_mod,
        "fetch_profile",
        lambda platform, token, **kwargs: {"account_name": "My Channel"},
    )
    start = client.get("/auth/youtube/start")
    assert start.status_code == 200
    callback = client.get(
        "/auth/callback/youtube",
        params={"code": "auth-code-1", "state": start.json()["state"]},
    )
    assert callback.status_code == 200
    assert client.get("/onboarding").json()["first_channel_connected"] is True


# ------------------------------------------------------------------ test-connection language
def test_test_connection_names_the_missing_credential(client: TestClient) -> None:
    # Nothing saved at all: the client ID is the missing step.
    resp = client.post("/settings/test-connection/youtube")
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "client ID is not saved yet" in detail
    assert "Next step:" in detail
    assert "Step 3" in detail
    assert "Traceback" not in detail

    # Client ID saved, secret missing: the exact example from the spec.
    client.put("/settings", json={"credentials": {"youtube": {"client_id": "yt-id"}}})
    resp = client.post("/settings/test-connection/youtube")
    assert resp.status_code == 502
    assert "client secret is not saved yet" in resp.json()["detail"]
    assert "Step 3" in resp.json()["detail"]

    # Both saved but no token: still a plain next step, not a bare code.
    client.put("/settings", json={"credentials": {"youtube": {"client_secret": "yt-sec"}}})
    resp = client.post("/settings/test-connection/youtube")
    assert resp.status_code == 502
    assert "not connected" in resp.json()["detail"]
    assert "Next step:" in resp.json()["detail"]


def test_test_connection_llm_points_at_skip(client: TestClient) -> None:
    resp = client.post("/settings/test-connection/llm")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "No LLM API key" in detail
    assert "skip the LLM" in detail


# ------------------------------------------------------------------ version + ffmpeg report
def test_health_reports_version_0_4_0(client: TestClient) -> None:
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["version"] == "0.4.0"


def test_settings_reports_ffmpeg_version(client: TestClient) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed in this environment")
    ffmpeg = client.get("/settings").json()["ffmpeg"]
    assert ffmpeg["found"] is True
    assert ffmpeg["path"] == shutil.which("ffmpeg")
    assert ffmpeg["version"].startswith("ffmpeg version"), ffmpeg["version"]
    assert ffmpeg["install_url"] == FFMPEG_DOWNLOAD_URL


# ------------------------------------------------------------------ FFmpeg-missing jobs
def test_job_blocked_when_ffmpeg_missing(
    client: TestClient, test_video: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffprobe (part of FFmpeg) hidden: the job blocks with the install link."""
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: None if name in ("ffmpeg", "ffprobe") else real_which(name),
    )
    job = client.post(
        "/jobs",
        json={
            "source_type": "local",
            "source_path": test_video,
            "title": "Missing ffmpeg test",
            "own_content_declaration": True,
            "transcript_text": TRANSCRIPT_TEXT,
        },
    )
    assert job.status_code == 201, job.text
    run = client.post(f"/jobs/{job.json()['id']}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "blocked"
    assert body["blocked_by"]["code"] == "ffprobe_missing"
    assert FFMPEG_DOWNLOAD_URL in body["blocked_by"]["required_action"]
    assert body["failed_stage"] == "ingest"
    assert body["blocked_by"]["detail"]
    # No silent success: nothing was rendered.
    assert body.get("rendered_count", 0) == 0


def test_job_blocked_when_transcribe_needs_missing_ffmpeg(
    client: TestClient, test_video: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audio extraction without the ffmpeg binary: blocked, with the link.

    Ingest passes (ffprobe is present); the transcribe stage's audio
    extraction then finds no ffmpeg binary and the job blocks instead of
    failing with a bare FileNotFoundError.
    """
    import clipforge.worker

    def _boom(*args, **kwargs):
        raise RuntimeError(ffmpeg_missing_message())

    monkeypatch.setattr(clipforge.worker, "transcribe", _boom)
    job = client.post(
        "/jobs",
        json={
            "source_type": "local",
            "source_path": test_video,
            "title": "Missing ffmpeg transcribe test",
            "own_content_declaration": True,
            "transcript_text": TRANSCRIPT_TEXT,
        },
    )
    assert job.status_code == 201, job.text
    run = client.post(f"/jobs/{job.json()['id']}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "blocked"
    assert body["blocked_by"]["code"] == "ffmpeg_missing"
    assert FFMPEG_DOWNLOAD_URL in body["blocked_by"]["required_action"]
    assert "FFmpeg is missing" in body["blocked_by"]["detail"]
    assert "FileNotFoundError" not in body["blocked_by"]["detail"]
    assert body["failed_stage"] == "transcribe"
