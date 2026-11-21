"""Unit + router tests for the pre-publish copyright scan (v0.8.0, Worker C).

The scan is a metadata check only — tests pin the flag cases
(unknown licence -> warning, CC source -> clean, claimed music ->
warning, duration violations) and that the no-guarantee wording is
always present. It never claims Content ID verification.
"""

from __future__ import annotations

from types import SimpleNamespace

import clipforge_api.monetization_api as mon_router
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge.copyright_scan import NO_GUARANTEE_DISCLAIMER, scan_clip
from clipforge.db import ClipCandidate, Job, get_session

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
    application.include_router(mon_router.publish_router)
    return application, tmp_path


@pytest.fixture()
def client(app):
    application, _ = app
    return TestClient(application, raise_server_exceptions=False)


def _clip(**overrides):
    base = {"id": "clip-1", "duration_s": 30.0, "file_path": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def _job(**overrides):
    base = {"source_type": "local", "own_content": True, "meta_json": {}}
    base.update(overrides)
    return SimpleNamespace(**base)


def _severities(result):
    return {f["severity"] for f in result["flags"]}


def _types(result):
    return {f["type"] for f in result["flags"]}


# ------------------------------------------------------------------ licence
def test_unknown_licence_is_a_warning():
    result = scan_clip(_clip(), _job(own_content=False, meta_json={}))
    assert result["clip_id"] == "clip-1"
    licence_flags = [f for f in result["flags"] if f["type"] == "licence"]
    assert len(licence_flags) == 1
    assert licence_flags[0]["severity"] == "warning"
    assert "unknown" in licence_flags[0]["message"].lower()
    assert "own" in licence_flags[0]["message"].lower()


def test_cc_source_is_clean(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    result = scan_clip(
        _clip(file_path=str(video)),
        _job(own_content=False, meta_json={"licence": "Creative Commons CC-BY"}),
    )
    assert "warning" not in _severities(result)
    assert "severe" not in _severities(result)
    assert any(
        f["type"] == "licence" and "Creative Commons" in f["message"] for f in result["flags"]
    )


def test_own_content_is_clean(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    result = scan_clip(_clip(file_path=str(video)), _job(own_content=True, meta_json={}))
    assert "warning" not in _severities(result)
    licence_flags = [f for f in result["flags"] if f["type"] == "licence"]
    assert licence_flags[0]["severity"] == "info"


def test_youtube_source_without_declaration_warns():
    result = scan_clip(_clip(), _job(source_type="youtube", own_content=False, meta_json={}))
    licence_flags = [f for f in result["flags"] if f["type"] == "licence"]
    assert licence_flags[0]["severity"] == "warning"
    assert "YouTube" in licence_flags[0]["message"]


# ------------------------------------------------------------------ audio / duration / file
def test_claimed_music_is_a_warning():
    result = scan_clip(_clip(), _job(meta_json={"claimed_music": True}))
    audio_flags = [f for f in result["flags"] if f["type"] == "audio"]
    assert len(audio_flags) == 1
    assert audio_flags[0]["severity"] == "warning"
    assert "claimed" in audio_flags[0]["message"].lower()


def test_original_audio_is_clean(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    result = scan_clip(_clip(file_path=str(video)), _job(meta_json={"music": "original"}))
    assert "warning" not in _severities(result)


def test_long_clip_flags_each_platform_it_exceeds():
    result = scan_clip(_clip(duration_s=200.0), _job())
    duration_flags = [f for f in result["flags"] if f["type"] == "duration"]
    flagged = " ".join(f["message"] for f in duration_flags)
    assert "YouTube Shorts" in flagged  # 180s max
    assert "Facebook Reels" in flagged  # 90s max
    assert "X video" in flagged  # 140s max
    assert "TikTok" not in flagged  # 600s max — 200s is fine there
    assert "Instagram" not in flagged  # 900s max
    assert all(f["severity"] == "warning" for f in duration_flags)


def test_short_clip_has_no_duration_flags():
    result = scan_clip(_clip(duration_s=30.0), _job())
    assert "duration" not in _types(result)


def test_missing_clip_file_is_a_warning(tmp_path):
    result = scan_clip(_clip(file_path=str(tmp_path / "gone.mp4")), _job())
    file_flags = [f for f in result["flags"] if f["type"] == "file"]
    assert len(file_flags) == 1
    assert file_flags[0]["severity"] == "warning"


def test_existing_clip_file_is_clean(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    result = scan_clip(_clip(file_path=str(video)), _job())
    assert "file" not in _types(result)


# ------------------------------------------------------------------ disclaimer
def test_no_guarantee_wording_is_always_present():
    for meta in ({}, {"licence": "Creative Commons CC-BY"}, {"claimed_music": True}):
        result = scan_clip(_clip(), _job(meta_json=meta))
        assert "does NOT guarantee" in result["disclaimer"]
        assert "copyright-safe" in result["disclaimer"]
        assert "Content ID" in result["disclaimer"]
        assert "metadata check only" in result["disclaimer"]


def test_disclaimer_constant_matches():
    assert NO_GUARANTEE_DISCLAIMER.startswith("This scan is a metadata check only")


# ------------------------------------------------------------------ router
def _seed_clip(session):
    job = Job(source_type="local", title="test", own_content=True)
    session.add(job)
    session.commit()
    clip = ClipCandidate(job_id=job.id, n=1, start_s=0.0, end_s=30.0, status="approved")
    session.add(clip)
    session.commit()
    _seeded_job_ids.append(job.id)
    _seeded_clip_ids.append(clip.id)
    return clip.id


def test_router_scan_unknown_clip_404(client):
    resp = client.post("/publish/scan", json={"clip_id": "nope"})
    assert resp.status_code == 404


def test_router_scan_returns_flags_and_disclaimer(client):
    session = get_session()
    try:
        clip_id = _seed_clip(session)
    finally:
        session.close()
    resp = client.post("/publish/scan", json={"clip_id": clip_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["clip_id"] == clip_id
    assert isinstance(body["flags"], list)
    assert all(set(f) >= {"type", "message", "severity"} for f in body["flags"])
    assert all(f["severity"] in ("info", "warning", "severe") for f in body["flags"])
    assert "does NOT guarantee" in body["disclaimer"]
    assert "Content ID" in body["disclaimer"]
    # Own-content clip, no file: licence info + file warning, nothing severe.
    assert "severe" not in {f["severity"] for f in body["flags"]}


def test_router_scan_unknown_licence_warns(client):
    session = get_session()
    try:
        job = Job(source_type="youtube", title="t", own_content=False)
        session.add(job)
        session.commit()
        clip = ClipCandidate(job_id=job.id, n=1, start_s=0.0, end_s=30.0, status="pending")
        session.add(clip)
        session.commit()
        _seeded_job_ids.append(job.id)
        _seeded_clip_ids.append(clip.id)
        clip_id = clip.id
    finally:
        session.close()
    resp = client.post("/publish/scan", json={"clip_id": clip_id})
    assert resp.status_code == 200
    licence = [f for f in resp.json()["flags"] if f["type"] == "licence"]
    assert licence and licence[0]["severity"] == "warning"
