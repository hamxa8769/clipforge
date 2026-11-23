"""Integration tests for the v0.9.2 requirements pre-flight endpoints.

Covers ``GET /requirements/status`` + ``POST /requirements/recheck``
and the end-to-end gate: a YouTube job whose only yt-dlp is a broken
binary must BLOCK at ingest with code ``yt_dlp_broken`` and a
plain-language message — never start a download it cannot finish.

Isolation: the broken-binary test points PATH and HOME at temp dirs
(this VM has a real yt-dlp in ``~/.local/bin``), so the broken script
is the only yt-dlp the probe can see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
from fastapi.testclient import TestClient  # noqa: E402

try:
    from clipforge_api.app import app
except ImportError:
    pytest.skip(
        "clipforge_api.app not implemented yet — see docs/api.md",
        allow_module_level=True,
    )

client = TestClient(app)

REQUIREMENT_IDS = ["yt-dlp", "ffmpeg", "ffprobe", "whisper"]


def test_requirements_status_shape() -> None:
    resp = client.get("/requirements/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body["requirements"]] == REQUIREMENT_IDS
    assert isinstance(body["all_ready"], bool)
    assert isinstance(body["blocking"], list)
    assert body["checked_at"]
    assert "startup" in body  # null when the server never started (tests)
    for req in body["requirements"]:
        assert req["status"] in ("ok", "missing", "broken", "degraded")
        assert req["label"]
        assert isinstance(req["detail"], str) and req["detail"]
        assert "fix_action" in req and "fix_label" in req
    # This VM has working ffmpeg/ffprobe, so those two are ok with a
    # version and a path — the probe really ran them.
    by_id = {r["id"]: r for r in body["requirements"]}
    assert by_id["ffmpeg"]["status"] == "ok"
    assert by_id["ffmpeg"]["version"].startswith("ffmpeg version")
    assert by_id["ffmpeg"]["path"]
    assert by_id["ffprobe"]["status"] == "ok"


def test_requirements_recheck_works() -> None:
    resp = client.post("/requirements/recheck")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body["requirements"]] == REQUIREMENT_IDS
    assert isinstance(body["all_ready"], bool)
    assert body["checked_at"]


def test_broken_ytdlp_blocks_youtube_job_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v0.9.1 report, end to end: yt-dlp exists but does not run.

    The job must end ``blocked`` at ingest with ``yt_dlp_broken`` —
    the download must never start.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    broken = bin_dir / "yt-dlp"
    broken.write_text("#!/bin/sh\necho 'boom' >&2; exit 1\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from clipforge.ytdlp_install import probe_yt_dlp  # noqa: PLC0415

    probe = probe_yt_dlp()
    if not probe["broken"]:
        pytest.skip(f"broken-yt-dlp simulation did not take: {probe}")

    resp = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Own video",
            "own_content_declaration": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]
    run = client.post(f"/jobs/{job_id}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "blocked"
    assert body["blocked_by"]["code"] == "yt_dlp_broken"
    assert body["blocked_by"]["dependency"] == "yt-dlp"
    assert "does not run" in body["blocked_by"]["detail"]
    assert str(broken) in body["blocked_by"]["detail"]
    assert body["blocked_by"]["required_action"]
    assert body["failed_stage"] == "ingest"
    stages = {s["stage"]: s for s in body["stages"]}
    assert stages["ingest"]["status"] == "blocked"
    assert stages["ingest"]["error_code"] == "yt_dlp_broken"
