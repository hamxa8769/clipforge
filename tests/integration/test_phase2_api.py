"""Phase 2 API integration tests — CSV bulk, brand kits, A/B, B-roll,
retention. All dry-run / local: no platform credentials, no network."""

from __future__ import annotations

import io

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

from clipforge.db import ClipCandidate, Job, get_session  # noqa: E402

client = TestClient(app)


def _seed_candidate(status: str = "approved") -> tuple[str, str]:
    session = get_session()
    try:
        job = Job(
            source_type="local", source_path="/tmp/phase2-api-source.mp4", title="API phase2: test"
        )
        session.add(job)
        session.commit()
        cand = ClipCandidate(
            job_id=job.id,
            n=1,
            start_s=0.0,
            end_s=30.0,
            score=80.0,
            hook_rating=8.0,
            hook_line="Stop doing this first",
            status=status,
        )
        session.add(cand)
        session.commit()
        return job.id, cand.id
    finally:
        session.close()


def _csv_file(text: str):
    return {"file": ("schedule.csv", io.BytesIO(text.encode()), "text/csv")}


# ---------------------------------------------------------------- brand kits
def test_brand_kit_crud_and_templates():
    templates = client.get("/brand-kits/templates")
    assert templates.status_code == 200
    assert {t["template"] for t in templates.json()} >= {"bold-pop", "clean-minimal"}

    created = client.post(
        "/brand-kits",
        json={"name": "Phase2Test Kit", "template": "bold-pop", "outro_text": "See you soon."},
    )
    assert created.status_code == 201, created.text
    kit = created.json()
    assert kit["caption_style"] == "bold-pop"  # inherited from the template
    assert kit["outro_text"] == "See you soon."
    kit_id = kit["id"]

    bad = client.post("/brand-kits", json={"name": "Bad", "primary_colour": "red"})
    assert bad.status_code == 422

    updated = client.put(f"/brand-kits/{kit_id}", json={"accent_colour": "#00FF00"})
    assert updated.status_code == 200
    assert updated.json()["accent_colour"] == "#00FF00"

    fetched = client.get(f"/brand-kits/{kit_id}")
    assert fetched.status_code == 200 and fetched.json()["name"] == "Phase2Test Kit"

    deleted = client.delete(f"/brand-kits/{kit_id}")
    assert deleted.status_code == 200
    assert client.get(f"/brand-kits/{kit_id}").status_code == 404


def test_post_applies_brand_kit_and_auto_assigns_variant():
    _, cand_id = _seed_candidate()
    kit = client.post("/brand-kits", json={"name": "Phase2Test Kit 2", "intro_text": "Intro line"})
    kit_id = kit.json()["id"]
    variant = client.post(
        f"/clips/{cand_id}/title-variants",
        json={"platform": "youtube", "title": "A/B title wins?", "is_control": True},
    )
    assert variant.status_code == 201, variant.text
    variant_id = variant.json()["id"]

    post = client.post(
        "/posts",
        json={"clip_candidate_id": cand_id, "platform": "youtube", "brand_kit_id": kit_id},
    )
    assert post.status_code == 201, post.text
    body = post.json()
    assert body["brand_kit_id"] == kit_id
    assert body["title_variant_id"] == variant_id  # auto-assigned (least-used)
    assert body["seo"]["title"] == "A/B title wins?"
    assert body["seo"]["description"].startswith("Intro line")
    assert body["seo"]["brand_kit"]["name"] == "Phase2Test Kit 2"


# ---------------------------------------------------------------- A/B variants
def test_title_variant_flow_and_outcome():
    _, cand_id = _seed_candidate()
    control = client.post(
        f"/clips/{cand_id}/title-variants",
        json={"platform": "tiktok", "title": "Control", "is_control": True},
    )
    assert control.status_code == 201
    challenger = client.post(
        f"/clips/{cand_id}/title-variants",
        json={"platform": "tiktok", "title": "Challenger"},
    )
    assert challenger.status_code == 201

    listing = client.get(f"/clips/{cand_id}/title-variants", params={"platform": "tiktok"})
    assert listing.status_code == 200
    assert len(listing.json()) == 2
    assert next(r for r in listing.json() if r["is_control"])["title"] == "Control"
    assert all(r["has_outcome"] is False for r in listing.json())

    outcome = client.post(
        f"/title-variants/{challenger.json()['id']}/outcome",
        json={"views": 900, "completion_pct": 68.0, "viewed_pct": 61.0},
    )
    assert outcome.status_code == 200
    assert outcome.json()["views"] == 900

    bad_outcome = client.post(
        f"/title-variants/{challenger.json()['id']}/outcome",
        json={"completion_pct": 250.0},
    )
    assert bad_outcome.status_code == 422


# ---------------------------------------------------------------- B-roll
def test_broll_api_accepts_legal_sources_rejects_urls():
    _, cand_id = _seed_candidate()
    ok = client.post(
        f"/clips/{cand_id}/broll",
        json={
            "source_type": "source_video",
            "source_path": "/tmp/phase2-source.mp4",
            "declaration": "Cut from my own source video",
            "source_start_s": 5.0,
            "source_end_s": 10.0,
            "clip_start_s": 2.0,
            "clip_end_s": 7.0,
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["output"]["width"] == int(1080 * 0.45)
    asset_id = ok.json()["id"]

    url = client.post(
        f"/clips/{cand_id}/broll",
        json={
            "source_type": "uploaded_licensed",
            "source_path": "https://example.com/stock.mp4",
            "declaration": "licensed",
        },
    )
    assert url.status_code == 422

    no_declaration = client.post(
        f"/clips/{cand_id}/broll",
        json={
            "source_type": "uploaded_licensed",
            "source_path": "/tmp/licensed.mp4",
            "declaration": "",
        },
    )
    assert no_declaration.status_code == 422

    listing = client.get(f"/clips/{cand_id}/broll")
    assert listing.status_code == 200 and len(listing.json()) == 1

    deleted = client.delete(f"/clips/{cand_id}/broll/{asset_id}")
    assert deleted.status_code == 200
    assert client.get(f"/clips/{cand_id}/broll").json() == []


# ---------------------------------------------------------------- CSV bulk
def test_csv_bulk_preview_import_and_idempotency():
    _, cand_id = _seed_candidate()
    csv_text = (
        "clip_candidate_id,platform,scheduled_for,title_override\n"
        f"{cand_id},youtube,2026-11-21T18:00:00Z,Preview title\n"
        f"{cand_id},instagram,2026-11-22T09:00:00Z,\n"
        f"{cand_id},tiktok,bad-date,Bad row\n"
    )
    preview = client.post("/posts/bulk/csv", files=_csv_file(csv_text), params={"dry_run": True})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["dry_run"] is True
    assert body["summary"] == {"total_rows": 3, "created": 2, "skipped_duplicates": 0, "errors": 1}
    assert body["errors"][0]["row"] == 4

    imported = client.post("/posts/bulk/csv", files=_csv_file(csv_text), params={"dry_run": False})
    assert imported.status_code == 200
    assert imported.json()["summary"]["created"] == 2

    again = client.post("/posts/bulk/csv", files=_csv_file(csv_text), params={"dry_run": False})
    assert again.json()["summary"]["skipped_duplicates"] == 2
    assert again.json()["summary"]["created"] == 0

    posts = client.get("/posts", params={"platform": "youtube"})
    assert any(p["clip_candidate_id"] == cand_id for p in posts.json())


# ---------------------------------------------------------------- retention
def test_retention_ingest_feedback_and_preview():
    _, cand_id = _seed_candidate()
    bad = client.post(
        "/analytics/retention",
        json={"platform": "youtube", "clip_duration_s": 30.0, "completion_pct": 140.0},
    )
    assert bad.status_code == 422

    point = client.post(
        "/analytics/retention",
        json={
            "platform": "youtube",
            "clip_duration_s": 30.0,
            "avg_view_duration_s": 8.0,
            "completion_pct": 30.0,
            "viewed_pct": 40.0,
            "dropoff_second": 2.0,
            "views": 400,
            "clip_candidate_id": cand_id,
            "source": "manual",
        },
    )
    assert point.status_code == 201, point.text
    assert point.json()["source"] == "manual"

    listing = client.get("/analytics/retention", params={"clip_candidate_id": cand_id})
    assert listing.status_code == 200 and len(listing.json()) >= 1

    feedback = client.get("/analytics/feedback", params={"platform": "youtube"})
    assert feedback.status_code == 200
    assert feedback.json()["points_used"] >= 1

    preview = client.post(
        "/analytics/feedback/preview",
        json={
            "candidates": [
                {"id": "a", "score": 80.0, "hook": 9, "signals": {"hook": 0.9}, "duration_s": 30.0}
            ],
            "points": [
                {
                    "platform": "youtube",
                    "clip_duration_s": 30.0,
                    "completion_pct": 30.0,
                    "viewed_pct": 40.0,
                    "dropoff_second": 2.0,
                    "avg_view_duration_s": 8.0,
                }
            ],
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["candidates"][0]["adjusted_score"] < 80.0
    assert "hook penalty" in preview.json()["candidates"][0]["adjustment_reason"]
