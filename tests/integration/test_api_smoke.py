"""API integration tests — health + job/candidate/post approve flow.

The FastAPI app lives at ``apps/api/clipforge_api/app.py`` and is imported
as ``clipforge_api.app`` (see ``tests/conftest.py`` for the path setup and
the throwaway test database — tests never touch ``./clipforge.db``).

Real endpoints (docs/api.md):
  GET  /health            -> 200 {"status": "ok", "version": "0.3.0", ...}
  POST /jobs              -> 201 {"id": ..., "status": "pending", ...}
  GET  /jobs/{id}/candidates
  POST /jobs/{id}/candidates/{n}/approve -> 200 {"status": "approved"}
  POST /posts             -> 201, or 409 if the candidate is not approved
  POST /posts/{id}/approve -> 200 {"status": "approved"}
  POST /posts/{id}/publish?dry_run=true -> 200 {"status": "dry_run"}

Everything runs in dry-run mode: no platform is ever called. Candidates
are seeded directly in the test DB (the engine worker that normally
creates them is out of scope for this API test).
"""

from __future__ import annotations

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

from clipforge.db import ClipCandidate, get_session  # noqa: E402

client = TestClient(app)


def _seed_candidate(job_id: str, n: int = 1) -> str:
    session = get_session()
    try:
        cand = ClipCandidate(
            job_id=job_id,
            n=n,
            start_s=0.0,
            end_s=30.0,
            score=80.0,
            hook_rating=8.0,
            hook_line="Stop doing this first",
            reason="test candidate",
            status="pending",
        )
        session.add(cand)
        session.commit()
        return cand.id
    finally:
        session.close()


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.4.0"
    assert body["dry_run"] is True


def test_job_create_youtube_requires_ownership_declaration():
    no_declaration = client.post("/jobs", json={"source_type": "youtube"})
    assert no_declaration.status_code == 422
    declared = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": "https://example.com/own-video.mp4",
            "own_content_declaration": True,
        },
    )
    assert declared.status_code == 201
    assert declared.json()["status"] == "pending"
    assert declared.json()["own_content"] is True


def test_job_create_local_requires_a_source():
    response = client.post("/jobs", json={"source_type": "local", "title": "no source"})
    assert response.status_code == 422


def test_approval_first_publish_flow_dry_run():
    job = client.post(
        "/jobs",
        json={
            "source_type": "local",
            "source_path": "/tmp/clipforge-test-source.mp4",
            "title": "Morning routine: test",
            "own_content_declaration": True,
        },
    )
    assert job.status_code == 201
    job_id = job.json()["id"]

    candidate_id = _seed_candidate(job_id, n=1)

    candidates = client.get(f"/jobs/{job_id}/candidates")
    assert candidates.status_code == 200
    assert len(candidates.json()) == 1
    assert set(candidates.json()[0]["seo"]) == {"youtube", "instagram", "facebook", "tiktok"}

    # A post cannot be created from an unapproved candidate — 409.
    unapproved = client.post(
        "/posts", json={"clip_candidate_id": candidate_id, "platform": "youtube"}
    )
    assert unapproved.status_code == 409

    approve_candidate = client.post(f"/jobs/{job_id}/candidates/1/approve")
    assert approve_candidate.status_code == 200
    assert approve_candidate.json()["status"] == "approved"

    post = client.post(
        "/posts",
        json={
            "clip_candidate_id": candidate_id,
            "platform": "youtube",
            "video_path": "/tmp/clipforge-test-clip.mp4",
        },
    )
    assert post.status_code == 201
    assert post.json()["status"] == "pending_approval"
    post_id = post.json()["id"]

    # Publishing before the POST itself is approved must be refused — 409.
    publish_first = client.post(f"/posts/{post_id}/publish", params={"dry_run": True})
    assert publish_first.status_code == 409

    approve_post = client.post(f"/posts/{post_id}/approve")
    assert approve_post.status_code == 200
    assert approve_post.json()["status"] == "approved"

    published = client.post(f"/posts/{post_id}/publish", params={"dry_run": True})
    assert published.status_code == 200
    assert published.json()["status"] == "dry_run"
    assert published.json()["publish_result"]["status"] == "dry_run"

    # Already dry-run published: a second publish is refused, not repeated.
    republish = client.post(f"/posts/{post_id}/publish", params={"dry_run": True})
    assert republish.status_code == 409
