"""Unit + router tests for the v0.8.0 monetization extensions (Worker C).

Covers: UTM builder, affiliate URL validation + storage, A/B
store/retrieve, revenue basis honesty. No live platform calls: the
YouTube Analytics fetch is tested with ``httpx.MockTransport``.
"""

from __future__ import annotations

import json

import clipforge_api.monetization_api as mon_router
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge import abtest
from clipforge.db import CandidateSeo, ClipCandidate, Job, Post, get_session
from clipforge.monetization import (
    MonetizationError,
    ab_results_for_clip,
    add_utm,
    attach_affiliate_link,
    create_ab_variants,
    fetch_youtube_revenue,
    revenue_rows,
    save_variant_thumbnail,
    validate_affiliate_url,
    variant_thumbnail,
)

_seeded_job_ids: list[str] = []
_seeded_clip_ids: list[str] = []


@pytest.fixture(autouse=True)
def _clean_seeded():
    yield
    if not (_seeded_job_ids or _seeded_clip_ids):
        return
    session = get_session()
    try:
        if _seeded_clip_ids:
            session.query(Post).filter(Post.clip_candidate_id.in_(_seeded_clip_ids)).delete(
                synchronize_session=False
            )
            session.query(CandidateSeo).filter(
                CandidateSeo.clip_candidate_id.in_(_seeded_clip_ids)
            ).delete(synchronize_session=False)
            session.query(ClipCandidate).filter(ClipCandidate.id.in_(_seeded_clip_ids)).delete(
                synchronize_session=False
            )
        if _seeded_job_ids:
            session.query(Job).filter(Job.id.in_(_seeded_job_ids)).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
        _seeded_job_ids.clear()
        _seeded_clip_ids.clear()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    application = FastAPI()
    application.include_router(mon_router.router)
    application.include_router(mon_router.publish_router)
    return application, tmp_path


@pytest.fixture()
def client(app):
    application, _ = app
    return TestClient(application, raise_server_exceptions=False)


def _seed_clip(tmp_path, *, with_seo: bool = True) -> str:
    session = get_session()
    try:
        job = Job(source_type="local", title="test", own_content=True)
        session.add(job)
        session.commit()
        clip = ClipCandidate(job_id=job.id, n=1, start_s=0.0, end_s=30.0, status="approved")
        session.add(clip)
        session.commit()
        if with_seo:
            for platform in ("youtube", "tiktok"):
                session.add(
                    CandidateSeo(
                        clip_candidate_id=clip.id,
                        platform=platform,
                        seo_json={
                            "title": "Test clip",
                            "description": "A test clip description.",
                            "hashtags": ["test"],
                        },
                    )
                )
            session.commit()
        _seeded_job_ids.append(job.id)
        _seeded_clip_ids.append(clip.id)
        return clip.id
    finally:
        session.close()


# ------------------------------------------------------------------ UTM
def test_add_utm_basic_shape():
    url = add_utm("https://example.com/product", "youtube", "clip", "clip123")
    assert url == (
        "https://example.com/product?utm_source=youtube&utm_medium=clip&utm_campaign=clip123"
    )


def test_add_utm_default_medium_is_clip():
    url = add_utm("https://example.com/p", "tiktok", campaign="c1")
    assert "utm_medium=clip" in url
    assert "utm_source=tiktok" in url
    assert "utm_campaign=c1" in url


def test_add_utm_preserves_existing_params_and_replaces_old_utm():
    url = add_utm(
        "https://example.com/p?ref=home&utm_source=old&utm_medium=x",
        "x",
        "clip",
        "clip9",
    )
    assert "ref=home" in url
    assert "utm_source=x" in url
    assert "utm_source=old" not in url
    assert url.count("utm_medium=") == 1


def test_add_utm_rejects_non_http_url():
    with pytest.raises(ValueError):
        add_utm("ftp://example.com/p", "youtube", "clip", "c1")
    with pytest.raises(ValueError):
        add_utm("not-a-url", "youtube", "clip", "c1")


# ------------------------------------------------------------------ affiliate URL validation
def test_validate_affiliate_url():
    assert validate_affiliate_url("https://shop.example.com/item?x=1").startswith("https://")
    assert validate_affiliate_url("http://example.com/a") == "http://example.com/a"
    with pytest.raises(ValueError):
        validate_affiliate_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        validate_affiliate_url("")


# ------------------------------------------------------------------ affiliate storage
def test_attach_affiliate_stores_on_clip_and_tags_descriptions(tmp_path):
    clip_id = _seed_clip(tmp_path)
    session = get_session()
    try:
        result = attach_affiliate_link(session, clip_id, "https://shop.example.com/item")
        assert result["clip_id"] == clip_id
        assert result["affiliate_url"] == "https://shop.example.com/item"
        assert result["descriptions_updated"] == 2  # two CandidateSeo rows
        assert set(result["platforms"]) == {"youtube", "tiktok"}
        job = session.query(Job).filter_by(id=session.get(ClipCandidate, clip_id).job_id).one()
        assert (job.meta_json or {}).get("affiliate_links", {}).get(clip_id) == (
            "https://shop.example.com/item"
        )
        rows = session.query(CandidateSeo).filter_by(clip_candidate_id=clip_id).all()
        for row in rows:
            seo = row.seo_json or {}
            assert seo["affiliate_url"] == "https://shop.example.com/item"
            assert "utm_source=" + row.platform in seo["description"]
            assert f"utm_campaign={clip_id}" in seo["description"]
            assert seo["description"].startswith("A test clip description.")
        # Idempotent: attaching again does not duplicate the link.
        attach_affiliate_link(session, clip_id, "https://shop.example.com/item")
        rows = session.query(CandidateSeo).filter_by(clip_candidate_id=clip_id).all()
        for row in rows:
            assert row.seo_json["description"].count("shop.example.com/item") == 1
    finally:
        session.close()


def test_attach_affiliate_unknown_clip_and_bad_url(tmp_path):
    session = get_session()
    try:
        with pytest.raises(MonetizationError):
            attach_affiliate_link(session, "nope", "https://example.com/a")
        clip_id = _seed_clip(tmp_path)
        with pytest.raises(ValueError):
            attach_affiliate_link(session, clip_id, "ftp://example.com/a")
    finally:
        session.close()


# ------------------------------------------------------------------ A/B variants
def test_ab_create_and_results_not_measured_yet(tmp_path):
    clip_id = _seed_clip(tmp_path)
    session = get_session()
    try:
        created = create_ab_variants(
            session,
            clip_id,
            [
                {"title": "Title A", "thumbnail": "https://example.com/a.jpg"},
                {"title": "Title B"},
            ],
            tmp_path,
        )
        assert len(created) == 2
        assert created[0]["title"] == "Title A"
        assert created[0]["thumbnail"] == "https://example.com/a.jpg"
        assert created[0]["views"] == 0
        assert created[0]["measured"] is False
        assert created[0]["ctr"] is None
        assert "not measured yet" in created[0]["views_basis"]
        assert "impressions" in created[1]["ctr_basis"]
        assert variant_thumbnail(tmp_path, created[0]["id"]) == "https://example.com/a.jpg"
        assert variant_thumbnail(tmp_path, created[1]["id"]) is None
    finally:
        session.close()


def test_ab_results_use_recorded_outcome(tmp_path):
    clip_id = _seed_clip(tmp_path)
    session = get_session()
    try:
        created = create_ab_variants(session, clip_id, [{"title": "Title A"}], tmp_path)
        variant_id = created[0]["id"]
        abtest.record_outcome(session, variant_id, views=1234, completion_pct=55.0)
        rows = ab_results_for_clip(session, clip_id, tmp_path)
        assert rows[0]["views"] == 1234
        assert rows[0]["measured"] is True
        assert rows[0]["views_basis"] == "recorded A/B outcome"
        assert rows[0]["ctr"] is None  # CTR is never invented
    finally:
        session.close()


def test_ab_create_validates(tmp_path):
    clip_id = _seed_clip(tmp_path)
    session = get_session()
    try:
        with pytest.raises(MonetizationError):
            create_ab_variants(session, "nope", [{"title": "T"}], tmp_path)
        with pytest.raises(MonetizationError):
            create_ab_variants(session, clip_id, [], tmp_path)
        with pytest.raises(MonetizationError):
            create_ab_variants(session, clip_id, [{"title": "  "}], tmp_path)
        with pytest.raises(MonetizationError):
            create_ab_variants(
                session, clip_id, [{"title": "T", "thumbnail": "ftp://x/y.jpg"}], tmp_path
            )
    finally:
        session.close()


def test_save_variant_thumbnail_validates(tmp_path):
    save_variant_thumbnail(tmp_path, "v1", "https://example.com/t.png")
    assert variant_thumbnail(tmp_path, "v1") == "https://example.com/t.png"
    with pytest.raises(ValueError):
        save_variant_thumbnail(tmp_path, "v2", "not-a-url")


# ------------------------------------------------------------------ revenue
def test_revenue_rows_honest_without_any_connection(tmp_path):
    session = get_session()
    try:
        rows = revenue_rows(session, data_dir=tmp_path, youtube_access_token=None)
        assert [r["platform"] for r in rows] == [
            "youtube",
            "instagram",
            "facebook",
            "tiktok",
            "x",
        ]
        for row in rows:
            assert row["earnings"] is None
            assert row["rpm"] is None
            assert row["views"] == 0
            assert row["basis"] == "not reported by this platform's API"
    finally:
        session.close()


def test_revenue_rows_youtube_analytics_mocked(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "youtubeanalytics.googleapis.com" in request.url.host
        assert request.url.params["metrics"] == "views,estimatedRevenue"
        return httpx.Response(200, json={"rows": [[10000, 42.5]]})

    session = get_session()
    try:
        rows = revenue_rows(
            session,
            data_dir=tmp_path,
            youtube_access_token="yt-token",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        yt = [r for r in rows if r["platform"] == "youtube"][0]
        assert yt["earnings"] == 42.5
        assert yt["views"] == 10000
        assert yt["rpm"] == 4.25
        assert yt["currency"] == "USD"
        assert "YouTube Analytics" in yt["basis"]
        # Every other platform still reports nothing invented.
        for row in rows:
            if row["platform"] != "youtube":
                assert row["earnings"] is None
                assert row["basis"] == "not reported by this platform's API"
    finally:
        session.close()


def test_fetch_youtube_revenue_403_is_honest_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "forbidden"}})

    from clipforge.monetization import RevenueError

    with pytest.raises(RevenueError) as excinfo:
        fetch_youtube_revenue("tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "403" in str(excinfo.value)


def test_revenue_uses_measured_growth_views(tmp_path):
    clip_id = _seed_clip(tmp_path)
    session = get_session()
    try:
        post = Post(
            clip_candidate_id=clip_id,
            platform="youtube",
            status="published",
            publish_result_json={"provider_post_id": "vid-1"},
            seo_json={"title": "T"},
        )
        session.add(post)
        session.commit()
        (tmp_path / "growth_snapshots.json").write_text(
            json.dumps({"vid-1": {"views": 777, "likes": 10, "comments": 1}}),
            encoding="utf-8",
        )
        rows = revenue_rows(session, data_dir=tmp_path, youtube_access_token=None)
        yt = [r for r in rows if r["platform"] == "youtube"][0]
        assert yt["views"] == 777
        assert yt["earnings"] is None  # views measured, earnings still not reported
    finally:
        session.close()


# ------------------------------------------------------------------ router
def test_router_affiliate_422_bad_url(client, app):
    _, tmp_path = app
    clip_id = _seed_clip(tmp_path)
    resp = client.post("/monetization/affiliate", json={"clip_id": clip_id, "url": "ftp://x/y"})
    assert resp.status_code == 422
    resp = client.post("/monetization/affiliate", json={"clip_id": "nope", "url": "https://x.co/a"})
    assert resp.status_code == 404


def test_router_affiliate_ok(client, app):
    _, tmp_path = app
    clip_id = _seed_clip(tmp_path)
    resp = client.post(
        "/monetization/affiliate", json={"clip_id": clip_id, "url": "https://shop.example.com/i"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["affiliate_url"] == "https://shop.example.com/i"
    assert body["descriptions_updated"] == 2


def test_router_ab_create_and_get(client, app):
    _, tmp_path = app
    clip_id = _seed_clip(tmp_path)
    resp = client.post(
        "/monetization/ab",
        json={
            "clip_id": clip_id,
            "variants": [
                {"title": "A", "thumbnail": "https://example.com/a.jpg"},
                {"title": "B"},
            ],
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["variants"]) == 2
    resp = client.get(f"/monetization/ab/{clip_id}")
    assert resp.status_code == 200
    variants = resp.json()["variants"]
    assert [v["title"] for v in variants] == ["A", "B"]
    assert variants[0]["thumbnail"] == "https://example.com/a.jpg"
    assert variants[0]["views"] == 0
    assert variants[0]["ctr"] is None
    resp = client.post("/monetization/ab", json={"clip_id": clip_id, "variants": []})
    assert resp.status_code == 422
    resp = client.get("/monetization/ab/nope")
    assert resp.status_code == 404


def test_router_revenue_honest(client, app):
    resp = client.get("/monetization/revenue")
    assert resp.status_code == 200
    platforms = resp.json()["platforms"]
    assert len(platforms) == 5
    for row in platforms:
        assert row["earnings"] is None
        assert row["rpm"] is None
        assert row["basis"] == "not reported by this platform's API"
