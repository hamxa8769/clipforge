"""Phase 2 unit tests — brand kits, B-roll, CSV import, A/B, retention.

Pure-logic tests plus DB-backed tests for the CSV importer and the
A/B assignment (both use the throwaway test DB from conftest.py).
No platform credentials, no network, no live uploads anywhere here.
"""

from __future__ import annotations

import pytest

from clipforge import abtest, brandkits, retention
from clipforge.brandkits import BrandKitError, BrandKitSpec, apply_kit_to_seo, builtin_template
from clipforge.broll import (
    BrollError,
    BrollOverlay,
    build_broll_filter,
    overlay_spec,
    validate_broll,
)
from clipforge.csv_import import derive_idempotency_key, import_csv, parse_csv
from clipforge.db import ClipCandidate, Job, get_session, init_db
from clipforge.retention import (
    RetentionError,
    RetentionInput,
    adjust_candidate_scores,
    feedback_from_points,
    feedback_with_signals,
)


@pytest.fixture(scope="module", autouse=True)
def _tables():
    init_db()


def _session():
    return get_session()


def _seed_approved_candidate(session, status="approved") -> str:
    job = Job(source_type="local", source_path="/tmp/phase2-source.mp4", title="Test: phase2")
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
    return cand.id


# ---------------------------------------------------------------- brand kits
def test_brand_kit_rejects_bad_colour_and_style():
    with pytest.raises(BrandKitError):
        brandkits.validate_kit(BrandKitSpec(name="X", primary_colour="red"))
    with pytest.raises(BrandKitError):
        brandkits.validate_kit(BrandKitSpec(name="X", caption_style="comic-sans"))
    with pytest.raises(BrandKitError):
        brandkits.validate_kit(BrandKitSpec(name="  "))
    with pytest.raises(BrandKitError):
        brandkits.validate_kit(BrandKitSpec(name="X", logo_path="https://example.com/logo.png"))


def test_brand_kit_builtin_templates_exist_and_apply():
    templates = brandkits.list_builtin_templates()
    assert {t["template"] for t in templates} >= {"bold-pop", "clean-minimal"}
    assert len(templates) >= 2
    kit = builtin_template("bold-pop")
    assert kit.caption_style == "bold-pop"
    assert kit.accent_colour == "#FFD700"
    with pytest.raises(BrandKitError):
        builtin_template("nope")
    seo = {"title": "Keep me", "description": "Body text", "caption": "Body text"}
    out = apply_kit_to_seo(seo, BrandKitSpec(name="K", intro_text="Intro", outro_text="Outro"))
    assert out["title"] == "Keep me"  # kits never rewrite the title
    assert out["description"].startswith("Intro")
    assert out["description"].endswith("Outro")
    assert out["brand_kit"]["name"] == "K"
    assert seo["description"] == "Body text"  # input dict is not mutated


def test_brand_kit_caption_style_default():
    assert brandkits.caption_style_for_kit(None) == "bold-pop"
    assert brandkits.caption_style_for_kit(BrandKitSpec(name="K", caption_style="boxed")) == "boxed"


# ---------------------------------------------------------------- B-roll
def test_broll_rejects_urls_and_missing_declaration():
    with pytest.raises(BrollError, match="URLs are not accepted"):
        validate_broll(
            source_type="uploaded_licensed",
            source_path="https://example.com/stock.mp4",
            declaration="licensed",
        )
    with pytest.raises(BrollError, match="declaration is required"):
        validate_broll(source_type="source_video", source_path="/tmp/src.mp4", declaration="  ")
    with pytest.raises(BrollError, match="source_type"):
        validate_broll(source_type="youtube_download", source_path="/tmp/x.mp4", declaration="mine")


def test_broll_rejects_bad_ranges_and_positions():
    with pytest.raises(BrollError):
        validate_broll(
            source_type="source_video",
            source_path="/tmp/s.mp4",
            declaration="mine",
            clip_start_s=25.0,
            clip_end_s=20.0,
        )
    with pytest.raises(BrollError):
        validate_broll(
            source_type="source_video",
            source_path="/tmp/s.mp4",
            declaration="mine",
            clip_start_s=40.0,
            clip_duration_s=30.0,
        )
    with pytest.raises(BrollError):
        validate_broll(
            source_type="source_video",
            source_path="/tmp/s.mp4",
            declaration="mine",
            position="centre",
            width_frac=0.45,
        )
    with pytest.raises(BrollError):
        validate_broll(
            source_type="source_video",
            source_path="/tmp/s.mp4",
            declaration="mine",
            width_frac=1.5,
        )


def test_broll_accepts_both_legal_sources_and_builds_spec():
    for source_type in ("source_video", "uploaded_licensed"):
        ov = validate_broll(
            source_type=source_type,
            source_path="/tmp/own.mp4",
            declaration="My own footage, licensed to me",
            source_start_s=5.0,
            source_end_s=10.0,
            clip_start_s=2.0,
            clip_end_s=7.0,
        )
        assert ov.duration_s() == 5.0
        spec = overlay_spec(ov, clip_duration_s=30.0)
        assert spec["output"]["width"] == int(1080 * 0.45)
        assert spec["output"]["x"] > 0 and spec["output"]["y"] > 0
    fragment, final = build_broll_filter(
        [
            BrollOverlay(
                source_type="source_video",
                source_path="/tmp/own.mp4",
                declaration="mine",
                clip_start_s=2.0,
                clip_end_s=7.0,
            )
        ]
    )
    assert "overlay=" in fragment and "between(t,2.000,7.000)" in fragment
    assert final == "v1"
    assert build_broll_filter([]) == ("", "v0")


# ---------------------------------------------------------------- CSV import
def test_csv_missing_columns_reported():
    rows, errors = parse_csv("platform,scheduled_for\n")
    assert rows == [] and len(errors) == 1 and "clip_candidate_id" in errors[0].message


def test_csv_import_dry_run_preview_then_real_import_is_idempotent():
    session = _session()
    try:
        cand_id = _seed_approved_candidate(session)
        csv_text = (
            "clip_candidate_id,platform,scheduled_for,title_override\n"
            f"{cand_id},youtube,2026-11-21T18:00:00Z,First title\n"
            f"{cand_id},tiktok,2026-11-22T18:00:00Z,\n"
            f"{cand_id},youtube,not-a-date,Bad row\n"
            "missing-candidate,youtube,2026-11-21T18:00:00Z,No such clip\n"
            f"{cand_id},myspace,2026-11-21T18:00:00Z,Bad platform\n"
        )
        preview = import_csv(session, csv_text, dry_run=True)
        assert preview.dry_run is True
        assert len(preview.created) == 2  # the two valid rows
        assert len(preview.errors) == 3  # bad date, missing clip, bad platform
        assert {e.row_number for e in preview.errors} == {4, 5, 6}
        assert all("row" in e.to_dict() for e in preview.errors)
        # Dry-run stored nothing.
        from clipforge.db import Post

        assert session.query(Post).filter_by(clip_candidate_id=cand_id).count() == 0

        real = import_csv(session, csv_text, dry_run=False)
        assert len(real.created) == 2 and len(real.errors) == 3
        assert session.query(Post).filter_by(clip_candidate_id=cand_id).count() == 2
        posts = session.query(Post).filter_by(clip_candidate_id=cand_id).all()
        assert {p.status for p in posts} == {"pending_approval"}  # approval-first
        assert {p.platform for p in posts} == {"youtube", "tiktok"}
        yt = next(p for p in posts if p.platform == "youtube")
        assert yt.seo_json["title"] == "First title"

        again = import_csv(session, csv_text, dry_run=False)
        assert len(again.created) == 0
        assert len(again.skipped_duplicates) == 2
        assert session.query(Post).filter_by(clip_candidate_id=cand_id).count() == 2
    finally:
        session.close()


def test_csv_rejects_unapproved_candidate():
    session = _session()
    try:
        cand_id = _seed_approved_candidate(session, status="pending")
        result = import_csv(
            session,
            f"clip_candidate_id,platform,scheduled_for\n{cand_id},youtube,2026-11-21T18:00:00Z\n",
            dry_run=True,
        )
        assert len(result.created) == 0
        assert len(result.errors) == 1
        assert "not 'approved'" in result.errors[0].message
    finally:
        session.close()


def test_csv_idempotency_key_is_stable():
    from datetime import datetime

    when = datetime.fromisoformat("2026-11-21T18:00:00+00:00")
    key1 = derive_idempotency_key("abc", "youtube", when, "T")
    key2 = derive_idempotency_key("abc", "youtube", when, "T")
    key3 = derive_idempotency_key("abc", "tiktok", when, "T")
    assert key1 == key2 and key1 != key3


# ---------------------------------------------------------------- A/B testing
def test_ab_variants_balanced_assignment_and_outcomes():
    session = _session()
    try:
        cand_id = _seed_approved_candidate(session)
        control = abtest.create_variant(
            session,
            clip_candidate_id=cand_id,
            platform="youtube",
            title="Control title",
            is_control=True,
        )
        variant = abtest.create_variant(
            session,
            clip_candidate_id=cand_id,
            platform="youtube",
            title="Variant title",
        )
        assert abtest.pick_variant(session, cand_id, "youtube").id == control.id
        # After one post uses the control, the next pick is the variant.
        from clipforge.db import Post

        post = Post(
            clip_candidate_id=cand_id,
            platform="youtube",
            title_variant_id=control.id,
            status="pending_approval",
        )
        session.add(post)
        session.commit()
        assert abtest.pick_variant(session, cand_id, "youtube").id == variant.id
        # A second control demotes the first.
        abtest.create_variant(
            session,
            clip_candidate_id=cand_id,
            platform="youtube",
            title="New control",
            is_control=True,
        )
        session.refresh(control)
        assert control.is_control is False
        # Outcomes are stored, never fetched from a platform here.
        updated = abtest.record_outcome(
            session, variant.id, views=1200, completion_pct=82.0, viewed_pct=74.0
        )
        assert updated.views == 1200 and updated.completion_pct == 82.0
        with pytest.raises(abtest.ABTestError):
            abtest.record_outcome(session, variant.id, completion_pct=140.0)
        results = abtest.variant_results(session, cand_id, "youtube")
        assert len(results) == 3
        assert next(r for r in results if r["id"] == variant.id)["has_outcome"] is True
        assert abtest.stable_variant_for_post("post-1", [control.id, variant.id]) in {
            control.id,
            variant.id,
        }
        assert abtest.stable_variant_for_post("post-1", []) is None
    finally:
        session.close()


# ---------------------------------------------------------------- retention
def test_retention_validation_rejects_bad_values():
    with pytest.raises(RetentionError):
        retention.validate_retention(RetentionInput(platform="myspace", clip_duration_s=30.0))
    with pytest.raises(RetentionError):
        retention.validate_retention(
            RetentionInput(platform="youtube", clip_duration_s=30.0, completion_pct=150.0)
        )
    with pytest.raises(RetentionError):
        retention.validate_retention(
            RetentionInput(platform="youtube", clip_duration_s=30.0, dropoff_second=45.0)
        )


def test_retention_feedback_early_dropoff_penalises_hooks():
    points = [
        RetentionInput(
            platform="youtube",
            clip_duration_s=30.0,
            completion_pct=35.0,
            viewed_pct=40.0,
            dropoff_second=2.0,
            avg_view_duration_s=8.0,
        ),
        RetentionInput(
            platform="youtube",
            clip_duration_s=30.0,
            completion_pct=42.0,
            viewed_pct=45.0,
            dropoff_second=2.5,
            avg_view_duration_s=9.0,
        ),
    ]
    feedback = feedback_from_points(points)
    assert feedback.points_used == 2
    assert feedback.early_dropoff_rate == 1.0
    assert feedback.hook_penalty == retention.MAX_HOOK_PENALTY
    assert feedback.recommended_max_duration_s is not None
    assert feedback.recommended_max_duration_s >= 15.0
    assert any("Hook failing" in r for r in feedback.recommendations)

    candidates = [
        {"id": "a", "score": 80.0, "hook": 9, "signals": {"hook": 0.9}, "duration_s": 30.0},
        {"id": "b", "score": 70.0, "hook": 3, "signals": {"hook": 0.2}, "duration_s": 20.0},
    ]
    adjusted = adjust_candidate_scores(candidates, feedback)
    a = next(c for c in adjusted if c["id"] == "a")
    b = next(c for c in adjusted if c["id"] == "b")
    # The hook-heavy candidate loses more than the weak-hook one.
    assert a["adjustment"] < b["adjustment"]
    assert a["adjusted_score"] < a["score"]
    assert "hook penalty" in a["adjustment_reason"]
    # Base score is never mutated.
    assert candidates[0]["score"] == 80.0


def test_retention_feedback_strong_retention_boosts_signals():
    points = [
        {
            "platform": "youtube",
            "clip_duration_s": 30.0,
            "completion_pct": 82.0,
            "viewed_pct": 76.0,
            "dropoff_second": 18.0,
            "avg_view_duration_s": 24.0,
            "signals": {"opinion_bomb": 0.9, "hook": 0.8},
            "hook": 8,
        },
        {
            "platform": "youtube",
            "clip_duration_s": 30.0,
            "completion_pct": 78.0,
            "viewed_pct": 72.0,
            "dropoff_second": 20.0,
            "avg_view_duration_s": 23.0,
            "signals": {"opinion_bomb": 0.7},
            "hook": 7,
        },
    ]
    feedback = feedback_with_signals(points)
    assert feedback.hook_penalty == 0.0
    assert feedback.signal_boosts.get("opinion_bomb", 0) > 0
    assert any("reuse" in r for r in feedback.recommendations)
    adjusted = adjust_candidate_scores(
        [
            {
                "id": "x",
                "score": 60.0,
                "hook": 5,
                "signals": {"opinion_bomb": 0.9},
                "duration_s": 30.0,
            }
        ],
        feedback,
    )
    assert adjusted[0]["adjusted_score"] > 60.0
    assert "boost" in adjusted[0]["adjustment_reason"]


def test_retention_feedback_empty_is_honest():
    feedback = feedback_from_points([])
    assert feedback.points_used == 0
    assert feedback.hook_penalty == 0.0
    assert feedback.signal_boosts == {}
    assert any("No retention data" in r for r in feedback.recommendations)
    adjusted = adjust_candidate_scores(
        [{"id": "x", "score": 50.0, "hook": 5, "signals": {}, "duration_s": 30.0}],
        feedback,
    )
    assert adjusted[0]["adjusted_score"] == 50.0


def test_retention_store_roundtrip():
    session = _session()
    try:
        cand_id = _seed_approved_candidate(session)
        point = retention.store_retention_point(
            session,
            RetentionInput(
                platform="tiktok",
                clip_duration_s=30.0,
                completion_pct=61.0,
                viewed_pct=55.0,
                dropoff_second=4.0,
                views=500,
                clip_candidate_id=cand_id,
                source="manual",
            ),
        )
        assert point.id and point.source == "manual"
        from clipforge.db import RetentionPoint

        stored = session.get(RetentionPoint, point.id)
        assert stored is not None and stored.views == 500
    finally:
        session.close()
