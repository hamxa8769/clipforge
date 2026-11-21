"""Unit + router tests for the X (Twitter) platform (v0.8.0, Worker C).

Live X calls are NOT tested here — there is no X API key and no
developer app in this environment. The HTTP layer is tested with
``httpx.MockTransport``; the router tests cover status honesty,
connect-without-credentials (422), publish validation, the
approval-first / rights guardrails, and the dry-run path.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import clipforge_api.x_platform as x_router
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge.auth.secrets import SecretStore
from clipforge.auth.settings_store import SettingsStore
from clipforge.auth.token_store import TokenRecord, TokenStore
from clipforge.db import ClipCandidate, Job, get_session
from clipforge.x_platform import (
    XError,
    build_authorize_url,
    exchange_code,
    fetch_account,
    load_pending_verifier,
    new_pkce_pair,
    publish_tweet,
    record_from_token_response,
    save_pending_verifier,
    validate_text,
    validate_video,
)

# Ids seeded by this file, deleted after each test: the suite shares one
# throwaway DB per process, so seeded rows must never leak.
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
    """Fresh app (X router only) + temp data dir."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    application = FastAPI()
    application.include_router(x_router.router)
    return application, tmp_path


@pytest.fixture()
def client(app):
    application, _ = app
    return TestClient(application, raise_server_exceptions=False)


def _seed_clip(
    tmp_path: Path,
    *,
    status: str = "approved",
    own_content: bool = True,
    duration_s: float = 30.0,
    with_file: bool = True,
) -> tuple[str, str]:
    """Seed a job + candidate. Returns (job_id, clip_id)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-video-bytes")
    session = get_session()
    try:
        job = Job(source_type="local", title="test", own_content=own_content)
        session.add(job)
        session.commit()
        clip = ClipCandidate(
            job_id=job.id,
            n=1,
            start_s=0.0,
            end_s=duration_s,
            status=status,
            file_path=str(video) if with_file else None,
        )
        session.add(clip)
        session.commit()
        _seeded_job_ids.append(job.id)
        _seeded_clip_ids.append(clip.id)
        return job.id, clip.id
    finally:
        session.close()


def _store_token(tmp_path: Path, *, expired: bool = False) -> None:
    record = TokenRecord(
        platform="x",
        access_token="x-access-token",
        refresh_token="x-refresh-token",
        expires_at=datetime.now(UTC) + (timedelta(hours=-1) if expired else timedelta(hours=2)),
        scopes=["tweet.read", "tweet.write", "users.read", "offline.access"],
        account_name="testuser",
    )
    TokenStore(SecretStore(tmp_path)).save(record)


# ------------------------------------------------------------------ PKCE / authorize URL
def test_pkce_challenge_is_sha256_of_verifier():
    verifier, challenge = new_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    assert verifier != challenge


def test_authorize_url_is_x_oauth2_with_pkce():
    url = build_authorize_url(
        client_id="cid123",
        redirect_uri="http://127.0.0.1:8000/auth/callback/x",
        state="s1",
        code_challenge="chal",
    )
    assert url.startswith("https://twitter.com/i/oauth2/authorize?")
    assert "response_type=code" in url
    assert "client_id=cid123" in url
    assert "tweet.write" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "offline.access" in url


def test_pending_verifier_round_trip_and_consume(tmp_path):
    save_pending_verifier(tmp_path, "state-1", "verifier-abc")
    assert load_pending_verifier(tmp_path, "state-1") == "verifier-abc"
    # Consumed: a second load finds nothing (single-use).
    assert load_pending_verifier(tmp_path, "state-1") is None
    assert load_pending_verifier(tmp_path, "nope") is None


# ------------------------------------------------------------------ validation rules
def test_validate_text_empty_and_too_long():
    assert validate_text("")  # empty is a violation
    assert validate_text("   ")
    assert validate_text("x" * 280) == []
    assert validate_text("x" * 281)


def test_validate_video_duration_limit_is_140s(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    assert validate_video(duration_s=140.0, video_path=str(video)) == []
    assert validate_video(duration_s=141.0, video_path=str(video))
    assert validate_video(duration_s=30.0, video_path=str(tmp_path / "missing.mp4"))
    assert validate_video(duration_s=30.0, video_path=None)


# ------------------------------------------------- mocked HTTP: exchange / account / publish
def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exchange_code_success_and_403_tier_message():
    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth2/token")
        return httpx.Response(
            200,
            json={"access_token": "tok", "refresh_token": "ref", "expires_in": 7200},
        )

    data = exchange_code(
        code="code1",
        code_verifier="ver",
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://127.0.0.1:8000/auth/callback/x",
        client=_mock_client(ok_handler),
    )
    assert data["access_token"] == "tok"

    def denied_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden", "error_description": "not allowed"})

    with pytest.raises(XError) as excinfo:
        exchange_code(
            code="code1",
            code_verifier="ver",
            client_id="cid",
            client_secret="csec",
            redirect_uri="http://127.0.0.1:8000/auth/callback/x",
            client=_mock_client(denied_handler),
        )
    assert "403" in str(excinfo.value)
    assert "paid" in excinfo.value.detail().lower() or "Basic" in excinfo.value.detail()


def test_record_from_token_response_is_x_record():
    record = record_from_token_response(
        {"access_token": "tok", "refresh_token": "ref", "expires_in": 7200}
    )
    assert record.platform == "x"
    assert record.access_token == "tok"
    assert not record.is_expired()


def test_fetch_account_success_and_rejection():
    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/users/me")
        return httpx.Response(
            200, json={"data": {"id": "1", "username": "clipforge", "name": "CF"}}
        )

    account = fetch_account("tok", client=_mock_client(ok_handler))
    assert account["account"] == "@clipforge"

    def denied_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Unauthorized"})

    with pytest.raises(XError):
        fetch_account("bad", client=_mock_client(denied_handler))


def test_publish_tweet_mocked_media_upload_then_tweet(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789" * 100)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "upload.twitter.com" in request.url.host:
            form = request.read().decode("utf-8", errors="ignore")
            if "FINALIZE" in form:
                calls.append("FINALIZE")
                return httpx.Response(200, json={"media_id_string": "mid-1"})
            if "APPEND" in form:
                calls.append("APPEND")
                return httpx.Response(204)
            calls.append("INIT")
            return httpx.Response(200, json={"media_id_string": "mid-1"})
        assert request.url.path.endswith("/tweets")
        calls.append("TWEET")
        body = request.read().decode("utf-8")
        assert "mid-1" in body
        return httpx.Response(201, json={"data": {"id": "tweet-123"}})

    result = publish_tweet(
        "tok",
        text="Hello X",
        video_path=str(video),
        duration_s=30.0,
        client=_mock_client(handler),
    )
    assert result["status"] == "published"
    assert result["provider_post_id"] == "tweet-123"
    assert calls == ["INIT", "APPEND", "FINALIZE", "TWEET"]


def test_publish_tweet_403_reports_tier_limit(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        if "upload.twitter.com" in request.url.host:
            return httpx.Response(200, json={"media_id_string": "mid-1"})
        return httpx.Response(403, json={"detail": "Forbidden"})

    with pytest.raises(XError) as excinfo:
        publish_tweet(
            "tok",
            text="hi",
            video_path=str(video),
            duration_s=10.0,
            client=_mock_client(handler),
        )
    assert "403" in str(excinfo.value)
    assert "paid" in excinfo.value.detail().lower()


def test_publish_tweet_validates_before_network(tmp_path):
    # 281 chars + 200s video: must fail without any HTTP call.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no network call should happen")

    with pytest.raises(XError):
        publish_tweet(
            "tok",
            text="x" * 281,
            video_path=str(tmp_path / "clip.mp4"),
            duration_s=200.0,
            client=_mock_client(handler),
        )


# ------------------------------------------------------------------ router: status honesty
def test_status_not_connected_is_honest(client):
    resp = client.get("/x/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert body["account"] is None
    assert "paid" in body["note"].lower() or "Basic" in body["note"]


def test_status_expired_token_is_not_connected(client, app):
    _, tmp_path = app
    _store_token(tmp_path, expired=True)
    resp = client.get("/x/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False
    assert "expired" in resp.json()["note"].lower()


def test_status_valid_token_but_x_unreachable_is_not_connected(client, app, monkeypatch):
    """A stored token is not enough: /x/status validates it live.

    The network here is stubbed to fail, so the endpoint must report
    not-connected rather than claiming the account.
    """
    _, tmp_path = app
    _store_token(tmp_path)

    def failing_fetch_account(access_token, *, client=None):
        raise XError("Could not reach X: boom", "Check your internet connection and try again.")

    monkeypatch.setattr(x_router, "fetch_account", failing_fetch_account)
    resp = client.get("/x/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False
    assert resp.json()["account"] is None


# ------------------------------------------------------------------ router: connect
def test_connect_without_credentials_is_422_plain_language(client):
    resp = client.post("/x/connect")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "developer.x.com" in detail
    assert "paid" in detail.lower() or "Basic" in detail
    assert "Traceback" not in detail


def test_connect_with_credentials_returns_authorize_url(client, app):
    _, tmp_path = app
    store = SettingsStore(tmp_path)
    store.set_credential("x", "client_id", "cid123")
    store.set_credential("x", "client_secret", "csec456")
    resp = client.post("/x/connect")
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"].startswith("https://twitter.com/i/oauth2/authorize?")
    assert "client_id=cid123" in body["url"]
    assert body["redirect_uri"].endswith("/auth/callback/x")


# ------------------------------------------------------------------ router: publish
def test_publish_unknown_clip_404(client):
    resp = client.post("/x/publish", json={"clip_id": "nope", "text": "hi"})
    assert resp.status_code == 404


def test_publish_unapproved_clip_409(client, app):
    _, tmp_path = app
    _, clip_id = _seed_clip(tmp_path, status="pending")
    resp = client.post("/x/publish", json={"clip_id": clip_id, "text": "hi"})
    assert resp.status_code == 409
    assert "Approve" in resp.json()["detail"]


def test_publish_non_own_content_clip_422(client, app):
    _, tmp_path = app
    _, clip_id = _seed_clip(tmp_path, own_content=False)
    resp = client.post("/x/publish", json={"clip_id": clip_id, "text": "hi"})
    assert resp.status_code == 422
    assert "own" in resp.json()["detail"].lower()


def test_publish_text_too_long_422(client, app):
    _, tmp_path = app
    _, clip_id = _seed_clip(tmp_path)
    resp = client.post("/x/publish", json={"clip_id": clip_id, "text": "x" * 281})
    assert resp.status_code == 422
    assert "280" in resp.json()["detail"]


def test_publish_video_too_long_422(client, app):
    _, tmp_path = app
    _, clip_id = _seed_clip(tmp_path, duration_s=200.0)
    resp = client.post("/x/publish", json={"clip_id": clip_id, "text": "hi"})
    assert resp.status_code == 422
    assert "140" in resp.json()["detail"]


def test_publish_not_connected_live_422(client, app):
    _, tmp_path = app
    _, clip_id = _seed_clip(tmp_path)
    resp = client.post("/x/publish", json={"clip_id": clip_id, "text": "hi", "dry_run": False})
    assert resp.status_code == 422
    assert "not connected" in resp.json()["detail"].lower()


def test_publish_dry_run_validates_and_sends_nothing(client, app):
    _, tmp_path = app
    _store_token(tmp_path)
    _, clip_id = _seed_clip(tmp_path)
    resp = client.post("/x/publish", json={"clip_id": clip_id, "text": "Hello X", "dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dry_run"
    assert body["provider_post_id"] is None
    assert body["text"] == "Hello X"
