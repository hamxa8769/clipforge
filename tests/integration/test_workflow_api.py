"""v0.3.0 live-workflow + automation integration tests.

Covers the worker (run / retry / cancel / queue), the blocked state
for missing dependencies, the automation rules engine (SEO on approve,
auto-queue per platform caps, pick-next from retention feedback) and
the /dependencies doctor endpoint. All local: no platform
credentials, no network. Real FFmpeg rendering is exercised with a
small generated test video (40 s, 320x240).
"""

from __future__ import annotations

import io
import os
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

from clipforge.db import ClipCandidate, Job, JobStatus, get_session  # noqa: E402
from clipforge.highlights import load_highlight_prompt  # noqa: E402

client = TestClient(app)

TRANSCRIPT_TEXT = (
    "Stop making this mistake every morning. Nobody tells you the secret behind "
    "a perfect routine, but I discovered the truth after years of trying. "
    "Here is exactly how to fix your mornings in three simple steps. "
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


@pytest.fixture(scope="module")
def test_video(tmp_path_factory):
    """A 40 s test video (testsrc, 320x240) for real ingest + render."""
    path = tmp_path_factory.mktemp("clipforge-workflow") / "test40.mp4"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=40:size=320x240:rate=10",
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


@pytest.fixture(scope="module", autouse=True)
def _isolate_data_dir(tmp_path_factory):
    """Point the worker/media data dir at a temp dir for this module."""
    data_dir = tmp_path_factory.mktemp("clipforge-workflow-data")
    os.environ["CLIPFORGE_DATA_DIR"] = str(data_dir)
    yield
    os.environ.pop("CLIPFORGE_DATA_DIR", None)


def _clear_retention_points():
    """Test isolation: the suite shares one DB, and other modules store
    retention points. The worker applies *global* retention feedback at
    score time, so a leftover early-drop-off point would penalise this
    test's fresh job below the score threshold. Clear them so the e2e
    run measures the worker, not other tests' fixtures."""
    from clipforge.db import RetentionPoint  # noqa: E402

    session = get_session()
    try:
        session.query(RetentionPoint).delete()
        session.commit()
    finally:
        session.close()


def _create_job(source_path: str, **extra) -> str:
    payload = {
        "source_type": "local",
        "source_path": source_path,
        "title": "Morning routine: test",
        "own_content_declaration": True,
        "transcript_text": TRANSCRIPT_TEXT,
        "top_n": 3,
    }
    payload.update(extra)
    resp = client.post("/jobs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_candidate(
    job_id: str, n: int = 1, status: str = "pending", score: float = 80.0, hook: float = 8.0
) -> str:
    session = get_session()
    try:
        cand = ClipCandidate(
            job_id=job_id,
            n=n,
            start_s=0.0,
            end_s=30.0,
            score=score,
            hook_rating=hook,
            hook_line="Stop doing this first",
            status=status,
        )
        session.add(cand)
        session.commit()
        return cand.id
    finally:
        session.close()


def _wait_for_status(job_id: str, status: str, timeout: float = 30.0) -> dict:
    """Poll GET /jobs/{id} until it reaches `status` (auto-run races)."""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body.get("status") == status:
            return body
        _time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached {status!r}: {body!r}")


def _seed_job(status: str = "review") -> str:
    session = get_session()
    try:
        job = Job(
            source_type="local", source_path="/tmp/seed.mp4", title="Seed: test", status=status
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


def test_job_created_is_auto_enqueued_v090(test_video):
    """v0.9.0 auto-run: POST /jobs enqueues the job at once — it must
    never sit ``pending`` forever with nothing to run it (audit R3)."""
    job_id = _create_job(test_video)
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    # The create response itself is the synchronous proof of enqueue;
    # the job may already be further along (running / a stage /
    # review) by the time we GET it — never still pending.
    assert resp.json()["status"] in {
        "queued",
        "running",
        "transcribing",
        "scoring",
        "rendering",
        "review",
        "ready",
        "blocked",
        "failed",
        "done",
    }, resp.json()
    assert resp.json()["status"] != "pending"


def test_upload_creates_and_enqueues_job_v090(test_video):
    """v0.9.0 auto-run: POST /jobs/upload also enqueues at once."""
    with open(test_video, "rb") as fh:
        resp = client.post(
            "/jobs/upload",
            files={"file": ("test40.mp4", fh, "video/mp4")},
            data={"title": "Upload auto-run", "own_content_declaration": "true"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] != "pending"


# ------------------------------------------------------------------ doctor
def test_dependencies_endpoint_names_missing_tools():
    resp = client.get("/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    names = {d["name"] for d in body["dependencies"]}
    assert {"ffmpeg", "ffprobe", "yt-dlp", "OPENAI_API_KEY"} <= names
    ffmpeg = next(d for d in body["dependencies"] if d["name"] == "ffmpeg")
    assert ffmpeg["available"] is True and ffmpeg["kind"] == "tool"
    # No secret value may ever appear in the doctor output.
    assert "sk-" not in resp.text and "EAAB" not in resp.text


# ------------------------------------------------------------------ worker run
def test_job_run_end_to_end_demo(test_video):
    _clear_retention_points()
    job_id = _create_job(test_video)
    resp = client.post(f"/jobs/{job_id}/run")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "review"
    assert body["demo"] is True  # fallback-text transcript is flagged demo
    assert body["progress_pct"] == 95.0
    assert body["blocked_by"] is None
    # Frontend contract (docs/API-CONTRACT.md §2): run timing + failed stage.
    assert body["failed_stage"] is None
    assert body["started_at"] is not None
    assert body["finished_at"] is not None
    assert body["duration_s"] is not None and body["duration_s"] > 0
    # Per-stage logs endpoint.
    logs = client.get(f"/jobs/{job_id}/logs")
    assert logs.status_code == 200
    entries = logs.json()["logs"]
    assert entries, "a finished run must have log entries"
    assert {e["stage"] for e in entries} >= {"ingest", "transcribe", "score", "render", "seo"}
    assert all(e["level"] in {"info", "error"} for e in entries)
    assert all(e["level"] == "info" for e in entries if e["stage"] != "worker")
    stages = {s["stage"]: s for s in body["stages"]}
    assert {"ingest", "transcribe", "score", "render", "seo"} <= set(stages)
    assert all(s["status"] == "done" for s in stages.values())
    assert stages["transcribe"]["detail"]["engine"] == "fallback-text"
    assert stages["transcribe"]["detail"]["demo"] is True
    assert stages["score"]["detail"]["candidate_count"] >= 1
    assert body["worker_runs"], "run history must be recorded"
    assert body["worker_runs"][0]["status"] == "done"
    assert body["worker_runs"][0]["duration_s"] is not None
    assert "alternates" in body  # below-threshold backups, autoclip-style

    candidates = client.get(f"/jobs/{job_id}/candidates")
    assert candidates.status_code == 200
    assert len(candidates.json()) >= 1
    cand = candidates.json()[0]
    assert cand["score_band"] in {"strong", "good", "okay", "weak"}
    assert cand["orig_start_s"] == cand["start_s"] or True  # orig span present
    assert cand["orig_start_s"] <= cand["start_s"]
    assert cand["orig_end_s"] >= cand["end_s"]
    assert cand["file_path"] and Path(cand["file_path"]).is_file()

    seo = client.get(f"/clips/{cand['id']}/seo")
    assert seo.status_code == 200
    assert set(seo.json()["seo"]) == {"youtube", "instagram", "facebook", "tiktok"}


def test_blocked_youtube_job_names_missing_ytdlp():
    if subprocess.run(["which", "yt-dlp"], capture_output=True).returncode == 0:
        pytest.skip("yt-dlp is installed in this environment")
    resp = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Own video",
            "own_content_declaration": True,
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]
    run = client.post(f"/jobs/{job_id}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "blocked"
    assert body["blocked_by"]["code"] == "yt_dlp_missing"
    assert body["blocked_by"]["dependency"] == "yt-dlp"
    assert body["blocked_by"]["required_action"]
    assert body["failed_stage"] == "ingest"  # contract: which stage blocked
    assert body["started_at"] is not None and body["duration_s"] is not None
    stages = {s["stage"]: s for s in body["stages"]}
    assert stages["ingest"]["status"] == "blocked"
    assert stages["ingest"]["error_code"] == "yt_dlp_missing"


def test_missing_source_file_fails_not_blocked():
    job_id = _create_job("/tmp/clipforge-does-not-exist.mp4")
    run = client.post(f"/jobs/{job_id}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "failed"
    assert body["blocked_by"] is None  # a missing file is an error, not a dependency
    assert body["error"]
    assert body["worker_runs"][0]["status"] == "failed"
    assert body["failed_stage"] == "ingest"
    logs = client.get(f"/jobs/{job_id}/logs")
    assert logs.status_code == 200
    error_entries = [e for e in logs.json()["logs"] if e["level"] == "error"]
    assert error_entries and error_entries[0]["stage"] == "ingest"
    assert client.get("/jobs/does-not-exist/logs").status_code == 404


def test_retry_and_cancel():
    # Retry: failed job runs again (and fails again on the missing file),
    # but the run history grows — the retry is a real second run.
    # v0.9.0 auto-run: the job fails once on its own first — wait for
    # that auto-run to land before driving /run and /retry, or the run
    # counts race with the background dispatcher.
    job_id = _create_job("/tmp/clipforge-does-not-exist-2.mp4")
    _wait_for_status(job_id, "failed")
    first = client.get(f"/jobs/{job_id}").json()
    assert first["status"] == "failed"
    assert len(first["worker_runs"]) == 1
    rerun = client.post(f"/jobs/{job_id}/run")
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["status"] == "failed"
    assert len(rerun.json()["worker_runs"]) == 2
    retried = client.post(f"/jobs/{job_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "failed"
    assert len(retried.json()["worker_runs"]) == 3
    # Retry on a job that is not failed/blocked is refused — use a job
    # in review (seeded directly, so auto-run cannot fail it first).
    review_id = _seed_job(status="review")
    assert client.post(f"/jobs/{review_id}/retry").status_code == 409
    assert client.post(f"/jobs/{review_id}/cancel").status_code == 409

    # Cancel: a queued job can be cancelled; a cancelled job cannot run.
    # (Seeded directly — POST /jobs auto-runs, so a queued job created
    # via the API would already be running/failed by the time we cancel.)
    session = get_session()
    try:
        pending_job = Job(
            source_type="local",
            source_path="/tmp/clipforge-does-not-exist-3.mp4",
            title="Seed: test",
            status=JobStatus.QUEUED.value,
        )
        session.add(pending_job)
        session.commit()
        pending_id = pending_job.id
    finally:
        session.close()
    cancelled = client.post(f"/jobs/{pending_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post(f"/jobs/{pending_id}/run").status_code == 409
    assert client.post(f"/jobs/{pending_id}/cancel").status_code == 409


def test_upload_endpoint_creates_job(test_video):
    with open(test_video, "rb") as fh:
        resp = client.post(
            "/jobs/upload",
            files={"file": ("test40.mp4", fh, "video/mp4")},
            data={
                "title": "Uploaded: test",
                "own_content_declaration": "true",
                "transcript_text": TRANSCRIPT_TEXT,
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # v0.9.0 auto-run: the upload is enqueued at once, not left pending.
    assert body["status"] in {"queued", "running", "blocked", "failed"}
    assert body["source"].endswith(".mp4")
    # Clean up so the run-next test below sees an empty queue.
    assert client.post(f"/jobs/{body['id']}/cancel").status_code == 200

    bad = client.post(
        "/jobs/upload",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        data={},
    )
    assert bad.status_code == 422


def test_worker_queue_and_run_next(test_video):
    _clear_retention_points()
    # Drain anything pending left by earlier tests so run-next is deterministic.
    for job in client.get("/jobs").json():
        if job["status"] in {"pending", "queued"}:
            client.post(f"/jobs/{job['id']}/cancel")
    # v0.9.0 auto-run: POST /jobs enqueues at once and the dispatcher
    # claims it, so a manually-driven run-next test must seed the queued
    # job directly in the DB (bypassing the API's auto-enqueue).
    session = get_session()
    try:
        job = Job(
            source_type="local",
            source_path=test_video,
            title="Morning routine: test",
            own_content=True,
            status=JobStatus.QUEUED.value,
            meta_json={"top_n": 3, "caption_style": "bold-pop", "transcript_text": TRANSCRIPT_TEXT},
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()
    queue = client.get("/worker/queue")
    assert queue.status_code == 200
    assert any(j["id"] == job_id for j in queue.json()["queued"])
    assert queue.json()["next"]["id"] == job_id
    assert queue.json()["running"] == []

    ran = client.post("/worker/run-next")
    assert ran.status_code == 200, ran.text
    if ran.json()["ran"] is None:
        # Race: the live dispatcher claimed the seeded job between the
        # GET above and this POST (it polls every 0.2 s). The job is
        # then already running — wait for it to finish instead; the
        # outcome (review, queue drained) is the same either way.
        body = _wait_for_status(job_id, "review", timeout=180.0)
        assert body["demo"] is True
    else:
        assert ran.json()["ran"]["id"] == job_id
        assert ran.json()["ran"]["status"] == "review"
        assert ran.json()["ran"]["demo"] is True
    assert client.get("/worker/queue").json()["queued"] == []

    empty = client.post("/worker/run-next")
    assert empty.status_code == 200
    assert empty.json()["ran"] is None


# ------------------------------------------------------------------ automation
def test_automation_rules_list_and_toggle():
    rules = client.get("/automation/rules")
    assert rules.status_code == 200
    # Contract shape (docs/API-CONTRACT.md §1): {rules: [...]}.
    body = rules.json()
    assert set(body) == {"rules"}
    kinds = {r["kind"] for r in body["rules"]}
    assert kinds == {
        "auto_seo_on_approve",
        "auto_pick_next_clip",
        "auto_queue_approved",
        "staggered_schedule",
        "auto_approve_high_score",
    }
    by_key = {r["key"]: r for r in body["rules"]}
    assert by_key["retention_feedback"]["kind"] == "auto_pick_next_clip"
    assert by_key["auto_seo"]["kind"] == "auto_seo_on_approve"
    assert by_key["auto_approve_high_score"]["enabled"] is False  # off by default
    assert by_key["staggered_schedule"]["enabled"] is True
    assert by_key["auto_seo"]["label"] == "Auto-SEO variants"
    assert by_key["auto_seo"]["description"]

    rule = next(r for r in body["rules"] if r["kind"] == "auto_queue_approved")
    disabled = client.patch(f"/automation/rules/{rule['id']}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    enabled = client.patch(f"/automation/rules/{rule['id']}", json={"enabled": True})
    assert enabled.json()["enabled"] is True

    bad = client.post(
        "/automation/rules",
        json={"kind": "no_such_rule", "enabled": True},
    )
    assert bad.status_code == 422


def test_automation_rules_put_by_frontend_key():
    # PUT is the contract toggle endpoint (docs/API-CONTRACT.md §1):
    # frontend key + enabled → updated rule. Restore afterwards.
    resp = client.put("/automation/rules", json={"key": "staggered_schedule", "enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == "staggered_schedule"
    assert resp.json()["enabled"] is False
    back = client.put("/automation/rules", json={"key": "staggered_schedule", "enabled": True})
    assert back.json()["enabled"] is True

    # The dry-run guard is server-locked: 409, never toggleable.
    locked = client.put("/automation/rules", json={"key": "dry_run_guard", "enabled": False})
    assert locked.status_code == 409
    assert client.put("/automation/rules", json={"key": "nope", "enabled": True}).status_code == 422
    # Backend kinds work too.
    by_kind = client.put("/automation/rules", json={"key": "auto_seo_on_approve", "enabled": True})
    assert by_kind.status_code == 200
    assert by_kind.json()["kind"] == "auto_seo_on_approve"


def test_approve_triggers_seo_and_auto_queue_with_account(monkeypatch):
    # One connected channel (YouTube, via the legacy env token — the
    # canonical GET /channels check). The other platforms have none,
    # so auto-queue must skip them — and say so in the feed.
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "test-dummy-token")
    account = client.post(
        "/accounts",
        json={
            "provider": "youtube",
            "label": "Test channel",
            "token_env_var": "CLIPFORGE_TEST_YT_TOKEN",
        },
    )
    assert account.status_code == 201, account.text

    job_id = _seed_job(status="review")
    cand_id = _seed_candidate(job_id, status="pending")
    approved = client.post(f"/jobs/{job_id}/candidates/1/approve")
    assert approved.status_code == 200, approved.text
    auto = approved.json()["automation"]
    assert auto["seo"]["platforms"] == ["facebook", "instagram", "tiktok", "youtube"]
    queued = [r for r in auto["queue"] if r["status"] == "queued"]
    assert len(queued) == 1 and queued[0]["platform"] == "youtube"
    skipped = {r["platform"]: r["status"] for r in auto["queue"]}
    assert skipped["instagram"] == "skipped_no_account"
    assert skipped["tiktok"] == "skipped_no_account"

    posts = client.get("/posts", params={"platform": "youtube"})
    assert any(p["clip_candidate_id"] == cand_id for p in posts.json())
    post = next(p for p in posts.json() if p["clip_candidate_id"] == cand_id)
    assert post["status"] == "pending_approval"  # queued, NOT published

    feed = client.get("/automation/feed", params={"clip_candidate_id": cand_id})
    actions = {e["action"] for e in feed.json()}
    assert "seo_generated" in actions
    assert "queued_post" in actions
    assert "queue_skipped_no_account" in actions

    seo = client.get(f"/clips/{cand_id}/seo")
    assert set(seo.json()["seo"]) == {"youtube", "instagram", "facebook", "tiktok"}


def test_auto_queue_respects_daily_cap(monkeypatch):
    from clipforge import automation as auto_engine  # noqa: E402

    # Connect the Instagram channel (legacy env token) so the cap —
    # not connectedness — is what blocks the queue.
    monkeypatch.setenv("META_ACCESS_TOKEN", "test-dummy-token")
    account = client.post(
        "/accounts",
        json={
            "provider": "instagram",
            "label": "Test IG",
            "token_env_var": "CLIPFORGE_TEST_IG_TOKEN",
        },
    )
    assert account.status_code == 201, account.text
    account_id = account.json()["id"]
    session = get_session()
    try:
        from clipforge.db import SocialAccount  # noqa: E402

        row = session.get(SocialAccount, account_id)
        row.daily_cap_override = 0  # cap of zero: nothing may queue
        session.commit()
        job_id = _seed_job(status="review")
        cand_id = _seed_candidate(job_id, status="approved")
        cand = session.get(ClipCandidate, cand_id)
        results = auto_engine.auto_queue_candidate(session, cand)
        by_platform = {r["platform"]: r for r in results}
        assert by_platform["instagram"]["status"] == "skipped_cap"
        assert "daily cap reached" in by_platform["instagram"]["reason"]
    finally:
        session.close()


def test_pick_next_uses_retention_feedback():
    job_id = _seed_job(status="review")
    _seed_candidate(job_id, n=1, status="pending", score=85.0, hook=9.0)
    _seed_candidate(job_id, n=2, status="pending", score=70.0, hook=4.0)
    # Early drop-off on the published clips: hooks are failing.
    point = client.post(
        "/analytics/retention",
        json={
            "platform": "youtube",
            "clip_duration_s": 30.0,
            "avg_view_duration_s": 8.0,
            "completion_pct": 30.0,
            "viewed_pct": 40.0,
            "dropoff_second": 2.0,
            "views": 100,
        },
    )
    assert point.status_code == 201, point.text

    pick = client.post(f"/automation/pick-next/{job_id}")
    assert pick.status_code == 200, pick.text
    body = pick.json()
    assert body["pick"] is not None
    assert body["pick"]["feedback"]["early_dropoff_rate"] == 1.0
    assert body["pick"]["feedback"]["hook_penalty"] > 0
    assert body["pick"]["adjustment"] < 0  # hook penalty applied
    assert "hook penalty" in body["pick"]["adjustment_reason"]
    assert body["pick"]["score_band"] in {"strong", "good", "okay", "weak"}

    feed = client.get("/automation/feed", params={"kind": "auto_pick_next_clip", "job_id": job_id})
    assert any(e["action"] == "picked_next_clip" for e in feed.json())


def test_automation_run_pass():
    resp = client.post("/automation/run")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "queued" in body and "picks" in body and "skipped" in body


def test_highlight_prompt_is_externalised(tmp_path):
    prompt = load_highlight_prompt()
    assert "word indices" in prompt
    assert "STRICT JSON" in prompt
    custom = tmp_path / "custom.md"
    custom.write_text("## Instructions\n\nCustom prompt text here.", encoding="utf-8")
    assert load_highlight_prompt(custom) == "Custom prompt text here."
    # A missing file falls back to the built-in prompt, never crashes.
    assert "word indices" in load_highlight_prompt(tmp_path / "nope.md")


# ------------------------------------------------------------------ contract: scheduler status
def test_scheduler_status_reports_real_runs():
    status = client.get("/scheduler/status")
    assert status.status_code == 200
    body = status.json()
    assert set(body) >= {"running", "last_run_at", "next_run_at", "due_now", "last_error"}
    assert body["running"] is False
    assert body["next_run_at"] is None  # on-demand only, no cron configured
    assert isinstance(body["due_now"], int)

    run = client.post("/scheduler/run", params={"dry_run": True})
    assert run.status_code == 200, run.text
    after = client.get("/scheduler/status").json()
    assert after["last_run_at"] is not None
    assert after["last_run"]["status"] == "done"
    assert after["last_run"]["dry_run"] is True
    assert after["last_run"]["processed"] == len(run.json()["results"])


def test_staggered_schedule_places_posts_in_platform_windows(monkeypatch):
    from datetime import datetime  # noqa: E402

    from clipforge import automation as auto_engine  # noqa: E402

    # Connect the Facebook channel (legacy env token — META_ACCESS_TOKEN
    # is the canonical env token for Facebook/Instagram) so the queue
    # runs; the stagger itself is what this test measures.
    monkeypatch.setenv("META_ACCESS_TOKEN", "test-dummy-token")

    # Stagger is on by default: each queued post lands in its platform's
    # best window (IG 11, FB 13, YT 15, TikTok 19 UTC). Facebook is used
    # here because the shared test DB may already have hit YouTube's
    # daily cap (6) by the time this test runs.
    account = client.post(
        "/accounts",
        json={
            "provider": "facebook",
            "label": "Stagger FB",
            "token_env_var": "CLIPFORGE_TEST_STAGGER_FB",
        },
    )
    assert account.status_code == 201, account.text
    session = get_session()
    try:
        job_id = _seed_job(status="review")
        cand_id = _seed_candidate(job_id, status="approved")
        cand = session.get(ClipCandidate, cand_id)
        results = auto_engine.auto_queue_candidate(session, cand)
        queued = next(r for r in results if r["platform"] == "facebook")
        assert queued["status"] == "queued"
        assert queued["scheduled_for"] is not None
        when = datetime.fromisoformat(queued["scheduled_for"])
        assert when.hour == 13  # Facebook window, UTC
        assert when > datetime.now(when.tzinfo) if when.tzinfo else True
    finally:
        session.close()

    # With stagger off, queued posts carry no scheduled time.
    client.put("/automation/rules", json={"key": "staggered_schedule", "enabled": False})
    try:
        session = get_session()
        try:
            job_id = _seed_job(status="review")
            cand_id = _seed_candidate(job_id, status="approved")
            cand = session.get(ClipCandidate, cand_id)
            results = auto_engine.auto_queue_candidate(session, cand)
            queued = next(r for r in results if r["platform"] == "facebook")
            assert queued["status"] == "queued"
            assert queued["scheduled_for"] is None
        finally:
            session.close()
    finally:
        client.put("/automation/rules", json={"key": "staggered_schedule", "enabled": True})


def test_high_score_rule_off_by_default_and_opt_in():
    from clipforge import automation as auto_engine  # noqa: E402

    session = get_session()
    try:
        auto_engine.ensure_default_rules(session)
        job_id = _seed_job(status="review")
        # Off by default: even a 96/9 clip stays pending.
        cand_id = _seed_candidate(job_id, status="pending", score=96.0, hook=9.0)
        cand = session.get(ClipCandidate, cand_id)
        assert auto_engine.apply_high_score_rule(session, cand) is False
        assert cand.status == "pending"

        # Opt in: the same clip auto-approves and is logged.
        client.put("/automation/rules", json={"key": "auto_approve_high_score", "enabled": True})
        try:
            assert auto_engine.apply_high_score_rule(session, cand) is True
            assert cand.status == "approved"
            feed = client.get(
                "/automation/feed",
                params={"kind": "auto_approve_high_score", "clip_candidate_id": cand_id},
            )
            assert any(e["action"] == "auto_approved_high_score" for e in feed.json())
            # Below the bar: no auto-approve even with the rule on.
            low_id = _seed_candidate(job_id, n=2, status="pending", score=90.0, hook=9.0)
            low = session.get(ClipCandidate, low_id)
            assert auto_engine.apply_high_score_rule(session, low) is False
            assert low.status == "pending"
        finally:
            client.put(
                "/automation/rules", json={"key": "auto_approve_high_score", "enabled": False}
            )
    finally:
        session.close()


def test_same_video_different_transcript_text_not_cached(test_video):
    """Regression: the shared transcript cache is keyed by video sha256
    only. Two jobs on the same video with different transcript_text
    must each use their own text — the second job must not silently
    inherit the first job's transcript (and a cached fallback text
    must not poison a later real Whisper run of the same video)."""
    import json

    _clear_retention_points()
    second_text = (
        "Betrayed by my own alarm clock. This is the story of how one "
        "explode-level morning disaster changed everything I thought I "
        "knew about routines. Nobody warned me, and honestly it was "
        "shocking. Here is exactly what happened, step by step, and how "
        "I fixed it with one small change. First, I stopped snoozing. "
        "Second, I put my phone across the room. Third, I drank water "
        "before coffee. The results were incredible, and the routine "
        "actually lasted. Try this trick tomorrow and watch the difference."
    )
    job1 = _create_job(test_video, transcript_text=TRANSCRIPT_TEXT)
    job2 = _create_job(test_video, transcript_text=second_text)
    assert client.post(f"/jobs/{job1}/run").status_code == 200
    assert client.post(f"/jobs/{job2}/run").status_code == 200

    data_dir = Path(os.environ["CLIPFORGE_DATA_DIR"])
    for job_id, needle in ((job1, "perfect routine"), (job2, "Betrayed")):
        artifact = json.loads(
            (data_dir / "jobs" / job_id / "transcript.json").read_text(encoding="utf-8")
        )
        text = " ".join(seg["text"] for seg in artifact["segments"])
        assert needle in text, f"job {job_id} transcript misses {needle!r}"
