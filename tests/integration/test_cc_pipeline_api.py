"""Integration tests for the v0.7.0 CC pipeline API: thumbnails, attribution,
licence records, and the publish refusal for Standard-licence sources.

Endpoints / fields under test (see ``clipforge.licensing`` for the shared
field names):
  POST /jobs (source_license / source_title / source_channel)
  GET  /jobs/{id}/candidates (licence record + thumbnail_url + SEO credit)
  GET  /clips/{id}/thumbnail | POST /clips/{id}/thumbnail
  POST /clips/{id}/thumbnail/select
  GET  /media/thumbnails/{job_id}/{filename}
  POST /posts | GET /posts (licence record on the approval-queue item)
  POST /posts/{id}/publish (refuses Standard-licence sources, 409)
"""

from __future__ import annotations

import subprocess
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

from clipforge import automation  # noqa: E402
from clipforge.db import ClipCandidate, Job, Post, get_session  # noqa: E402
from clipforge.scheduler import run_due_posts  # noqa: E402

client = TestClient(app)

CC_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
CREDIT = (
    "Source: Sunset Timelapse by Free Nature Films — https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    " — Licensed under Creative Commons Attribution 3.0 Unported "
    "(https://creativecommons.org/licenses/by/3.0/)"
)


def _make_video(path: Path, duration: int = 4) -> Path:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size=320x240:rate=10",
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
    return path


def _seed_job(**overrides) -> str:
    session = get_session()
    try:
        job = Job(
            source_type=overrides.get("source_type", "youtube"),
            source_url=overrides.get("source_url", CC_URL),
            title=overrides.get("title", "Sunset Timelapse"),
            own_content=overrides.get("own_content", False),
            status=overrides.get("status", "review"),
            source_license=overrides.get("source_license"),
            source_title=overrides.get("source_title"),
            source_channel=overrides.get("source_channel"),
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


def _seed_candidate(job_id: str, status: str = "pending", **overrides) -> str:
    session = get_session()
    try:
        cand = ClipCandidate(
            job_id=job_id,
            n=overrides.get("n", 1),
            start_s=overrides.get("start_s", 0.0),
            end_s=overrides.get("end_s", 4.0),
            score=80.0,
            hook_rating=8.0,
            hook_line=overrides.get("hook_line", "Sunset timelapse"),
            reason="test candidate",
            status=status,
            file_path=overrides.get("file_path"),
            source_license=overrides.get("source_license"),
            source_url=overrides.get("source_url"),
            source_title=overrides.get("source_title"),
            source_channel=overrides.get("source_channel"),
        )
        session.add(cand)
        session.commit()
        return cand.id
    finally:
        session.close()


def _delete_post(post_id: str) -> None:
    """Remove a post created by a test.

    The suite shares one test database and the YouTube daily cap is 6:
    a leftover pending / approved / scheduled / dry-run post counts
    toward the cap and can starve later tests (e.g. the auto-queue
    workflow test) — so tests that create YouTube posts clean up.
    """
    session = get_session()
    try:
        post = session.get(Post, post_id)
        if post is not None:
            session.delete(post)
            session.commit()
    finally:
        session.close()
    """Seed a Post directly (bypasses the create-post licence gate — this
    is how a pre-existing / slipped-through post is simulated)."""
    session = get_session()
    try:
        post = Post(
            clip_candidate_id=candidate_id,
            platform=overrides.get("platform", "youtube"),
            status=status,
            seo_json=overrides.get("seo_json", {"title": "T", "description": "D"}),
            video_path=overrides.get("video_path"),
            scheduled_for=overrides.get("scheduled_for"),
            source_license=overrides.get("source_license"),
            source_url=overrides.get("source_url"),
            source_title=overrides.get("source_title"),
            source_channel=overrides.get("source_channel"),
            attribution_text=overrides.get("attribution_text"),
        )
        session.add(post)
        session.commit()
        return post.id
    finally:
        session.close()


# ------------------------------------------------------------ job creation
def test_create_youtube_job_with_cc_licence_needs_no_declaration():
    res = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": CC_URL,
            "title": "Sunset Timelapse",
            "source_license": "Creative Commons Attribution 3.0",
            "source_title": "Sunset Timelapse",
            "source_channel": "Free Nature Films",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["source_license"] == "cc-by-3.0"
    assert body["source_title"] == "Sunset Timelapse"
    assert body["source_channel"] == "Free Nature Films"
    assert body["attribution_text"] == CREDIT


def test_create_youtube_job_with_standard_licence_refused():
    res = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": CC_URL,
            "title": "Someone Else's Video",
            "source_license": "YouTube Standard License",
            "own_content_declaration": True,
        },
    )
    assert res.status_code == 422
    assert "Standard licence" in res.text


def test_create_youtube_job_without_declaration_or_licence_refused():
    res = client.post(
        "/jobs",
        json={"source_type": "youtube", "source_url": CC_URL, "title": "No rights"},
    )
    assert res.status_code == 422


# ------------------------------------------------- candidates + attribution
def test_candidate_carries_licence_and_seo_credit_block():
    job_id = _seed_job(
        source_license="cc-by-3.0",
        source_title="Sunset Timelapse",
        source_channel="Free Nature Films",
    )
    _seed_candidate(job_id)
    res = client.get(f"/jobs/{job_id}/candidates")
    assert res.status_code == 200, res.text
    cand = res.json()[0]
    assert cand["source_license"] == "cc-by-3.0"
    assert cand["source_url"] == CC_URL
    assert cand["source_title"] == "Sunset Timelapse"
    assert cand["source_channel"] == "Free Nature Films"
    assert cand["attribution_text"] == CREDIT
    assert cand["seo"]["youtube"]["description"].count(CREDIT) == 1


def test_seo_regeneration_never_duplicates_credit_block():
    job_id = _seed_job(
        source_license="cc-by-3.0",
        source_title="Sunset Timelapse",
        source_channel="Free Nature Films",
    )
    cand_id = _seed_candidate(job_id)
    session = get_session()
    try:
        cand = session.get(ClipCandidate, cand_id)
        assert cand is not None
        first = automation.generate_seo_for_candidate(session, cand)
        second = automation.generate_seo_for_candidate(session, cand)
    finally:
        session.close()
    assert first["youtube"]["description"].count(CREDIT) == 1
    assert second["youtube"]["description"].count(CREDIT) == 1
    assert first["youtube"]["description"] == second["youtube"]["description"]
    stored = client.get(f"/clips/{cand_id}/seo").json()["seo"]
    assert stored["youtube"]["description"].count(CREDIT) == 1


# ---------------------------------------------------------------- thumbnails
@pytest.fixture()
def clip_with_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "data"
    job_id = _seed_job(
        source_license="cc-by-3.0",
        source_title="Sunset Timelapse",
        source_channel="Free Nature Films",
    )
    clips_dir = root / "clips" / job_id
    clips_dir.mkdir(parents=True)
    video = _make_video(clips_dir / "clip_01.mp4", duration=4)
    cand_id = _seed_candidate(job_id, file_path=str(video))
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(root))
    return {"job_id": job_id, "candidate_id": cand_id, "video": video, "root": root}


def test_thumbnail_regenerate_returns_two_variants_and_serves_jpg(clip_with_video):
    cand_id = clip_with_video["candidate_id"]
    job_id = clip_with_video["job_id"]
    res = client.post(f"/clips/{cand_id}/thumbnail", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["thumbnails"]) == 2
    assert body["thumbnail_url"] == f"/media/thumbnails/{job_id}/thumbnail.jpg"
    assert body["thumbnails"][0]["frame_second"] == pytest.approx(1.0)
    assert body["thumbnails"][1]["frame_second"] == pytest.approx(3.0)
    served = client.get(body["thumbnail_url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.content[:3] == b"\xff\xd8\xff"
    # Served via the candidates list too.
    listed = client.get(f"/jobs/{job_id}/candidates").json()[0]
    assert listed["thumbnail_url"] == body["thumbnail_url"]
    assert len(listed["thumbnails"]) == 2


def test_thumbnail_regenerate_with_frame_second_and_text(clip_with_video):
    cand_id = clip_with_video["candidate_id"]
    res = client.post(
        f"/clips/{cand_id}/thumbnail",
        json={"variant": 1, "frame_second": 2.0, "text": "New Title Here"},
    )
    assert res.status_code == 200, res.text
    variant = res.json()["thumbnails"][0]
    assert variant["frame_second"] == pytest.approx(2.0)
    assert variant["title_text"] == "New Title Here"


def test_thumbnail_select_picks_variant_2(clip_with_video):
    cand_id = clip_with_video["candidate_id"]
    job_id = clip_with_video["job_id"]
    client.post(f"/clips/{cand_id}/thumbnail", json={})
    res = client.post(f"/clips/{cand_id}/thumbnail/select", json={"variant": 2})
    assert res.status_code == 200, res.text
    assert res.json()["thumbnail_url"] == f"/media/thumbnails/{job_id}/thumbnail_2.jpg"
    current = client.get(f"/clips/{cand_id}/thumbnail").json()
    assert current["thumbnail_url"].endswith("thumbnail_2.jpg")


def test_thumbnail_without_rendered_clip_is_409():
    job_id = _seed_job()
    cand_id = _seed_candidate(job_id)
    res = client.post(f"/clips/{cand_id}/thumbnail", json={})
    assert res.status_code == 409
    assert client.get(f"/clips/{cand_id}/thumbnail").json()["thumbnails"] == []


def test_thumbnail_unknown_clip_is_404():
    assert client.post("/clips/nope/thumbnail", json={}).status_code == 404
    assert client.get("/clips/nope/thumbnail").status_code == 404


# ------------------------------------------------------- posts + publishing
def test_approval_queue_post_displays_licence_record():
    job_id = _seed_job(
        source_license="cc-by-3.0",
        source_title="Sunset Timelapse",
        source_channel="Free Nature Films",
    )
    cand_id = _seed_candidate(job_id, status="approved")
    res = client.post("/posts", json={"clip_candidate_id": cand_id, "platform": "youtube"})
    assert res.status_code == 201, res.text
    post = res.json()
    assert post["source_license"] == "cc-by-3.0"
    assert post["source_url"] == CC_URL
    assert post["source_title"] == "Sunset Timelapse"
    assert post["source_channel"] == "Free Nature Films"
    assert post["attribution_text"] == CREDIT
    assert CREDIT in (post["seo"].get("description") or "")


def test_create_post_refuses_standard_licence_source():
    job_id = _seed_job(source_license="standard")
    cand_id = _seed_candidate(job_id, status="approved", source_license="standard")
    res = client.post("/posts", json={"clip_candidate_id": cand_id, "platform": "youtube"})
    assert res.status_code == 409
    assert "Standard licence" in res.text


def test_publish_refuses_standard_licence_even_if_post_exists():
    job_id = _seed_job(source_license="standard")
    cand_id = _seed_candidate(job_id, status="approved", source_license="standard")
    post_id = _seed_post(cand_id, status="approved", source_license="standard")
    res = client.post(f"/posts/{post_id}/publish", params={"dry_run": True})
    assert res.status_code == 409
    assert "Standard licence" in res.text
    # The post was NOT published and NOT failed — it stays approved.
    assert client.get(f"/posts/{post_id}").json()["status"] == "approved"


def test_publish_cc_clip_dry_run_succeeds_with_credit():
    job_id = _seed_job(
        source_license="cc-by-3.0",
        source_title="Sunset Timelapse",
        source_channel="Free Nature Films",
    )
    cand_id = _seed_candidate(job_id, status="approved")
    created = client.post(
        "/posts",
        json={
            "clip_candidate_id": cand_id,
            "platform": "youtube",
            "video_path": "/tmp/clipforge-test-clip.mp4",
        },
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]
    assert client.post(f"/posts/{post_id}/approve").status_code == 200
    published = client.post(f"/posts/{post_id}/publish", params={"dry_run": True})
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "dry_run"


def test_scheduler_skips_standard_licence_post():
    from datetime import UTC, datetime, timedelta

    job_id = _seed_job(source_license="standard")
    cand_id = _seed_candidate(job_id, status="approved", source_license="standard")
    post_id = _seed_post(
        cand_id,
        status="scheduled",
        source_license="standard",
        scheduled_for=datetime.now(UTC) - timedelta(minutes=1),
    )
    session = get_session()
    try:
        results = run_due_posts(session, dry_run=True)
    finally:
        session.close()
    mine = [r for r in results if r["post_id"] == post_id]
    assert mine and mine[0]["status"] == "skipped_licence"
    assert client.get(f"/posts/{post_id}").json()["status"] == "scheduled"
