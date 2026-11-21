"""API integration tests — Creative Commons discovery + licence
guardrail (v0.7.0, Worker A).

Contract:

  GET  /api/trending/search?niche=&max_results=&licensed_only=
       (licensed_only=false is the v0.6.0 default)
  GET  /api/videos/license?url=<youtube-url>
       -> {video_id, title, channel, channel_id, license,
           license_url, can_clip, reason}
  POST /api/trending/run — the licence guardrail: a standard-licence
       third-party source video is refused (422, plain language) even
       when rights were ticked; a CC video or the user's own video is
       accepted; a video whose licence cannot be verified is refused
       honestly.

YouTube HTTP is mocked by monkeypatching ``httpx.Client`` in the
router module — no test ever hits the real YouTube API. The agent
itself is stubbed for the /run guardrail tests (the full pipeline is
already covered by the v0.6.0 demo-path test); these tests assert the
guardrail decision, not the pipeline.
"""

from __future__ import annotations

import httpx
import pytest
from clipforge_api import trending as trending_router  # noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge.auth.secrets import SecretStore  # noqa: E402
from clipforge.auth.token_store import TokenRecord, TokenStore  # noqa: E402
from clipforge.trending import STANDARD_LICENSE_REASON  # noqa: E402

app = FastAPI()
app.include_router(trending_router.router)
app.include_router(trending_router.videos_router)
client = TestClient(app)

CC_VIDEO = "CCCCCCCCCCC"
STD_VIDEO = "SSSSSSSSSSS"
OWN_VIDEO = "OOOOOOOOOOO"
OWN_CHANNEL_ID = "UCOWNCHANNEL1"
OTHER_CHANNEL_ID = "UCOTHERCHAN1"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    return tmp_path


def _save_api_key():
    resp = client.post("/api/trending/api-key", json={"api_key": "AIzaFAKEKEY1234"})
    assert resp.status_code == 200


def _save_oauth_token(tmp_path, *, account_ref: str):
    record = TokenRecord.from_token_response(
        "youtube", {"access_token": "oauth-token", "expires_in": 3600}
    )
    record.account_name = "My Channel"
    record.account_ref = account_ref
    TokenStore(SecretStore(tmp_path)).save(record)


def _video_item(video_id: str, *, license_: str | None, channel_id: str):
    item: dict = {
        "id": video_id,
        "snippet": {
            "title": f"Video {video_id}",
            "channelTitle": "Some Channel",
            "channelId": channel_id,
            "publishedAt": "2026-11-21T10:00:00Z",
        },
        "statistics": {"viewCount": "120000", "likeCount": "9000", "commentCount": "800"},
    }
    if license_ is not None:
        item["status"] = {"license": license_}
    return item


def _fake_client_factory(*, search_ids=None, video_items=None):
    """Fresh fake YouTube client per call (the router opens its own
    client per request, and a closed httpx.Client cannot be reused)."""
    real_client = httpx.Client  # captured before monkeypatching

    def _make(**kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": {"videoId": vid}, "snippet": {"title": f"Video {vid}"}}
                            for vid in (search_ids or [])
                        ]
                    },
                )
            if request.url.path.endswith("/videos"):
                return httpx.Response(200, json={"items": video_items or []})
            return httpx.Response(404, json={})

        return real_client(transport=httpx.MockTransport(handler))

    return _make


class _StubAgent:
    """Records the config the router tried to run; never downloads."""

    def __init__(self):
        self.started: list = []

    def start_trending(self, config):
        self.started.append(config)
        return {"state": "running", "current_job": None}


# ------------------------------------------------------------------ /videos/license
def test_license_endpoint_cc_video_can_clip(monkeypatch):
    _save_api_key()
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            video_items=[
                _video_item(CC_VIDEO, license_="creativeCommon", channel_id=OTHER_CHANNEL_ID)
            ]
        ),
    )
    resp = client.get(
        "/api/videos/license", params={"url": f"https://www.youtube.com/watch?v={CC_VIDEO}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["video_id"] == CC_VIDEO
    assert body["license"] == "creativeCommon"
    assert body["license_url"] == "https://creativecommons.org/licenses/by/3.0/"
    assert body["can_clip"] is True
    assert "Creative Commons" in body["reason"]
    assert body["channel"] == "Some Channel"


def test_license_endpoint_standard_video_cannot_clip(monkeypatch):
    _save_api_key()
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            video_items=[_video_item(STD_VIDEO, license_="youtube", channel_id=OTHER_CHANNEL_ID)]
        ),
    )
    resp = client.get("/api/videos/license", params={"url": f"https://youtu.be/{STD_VIDEO}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["license"] == "youtube"
    assert body["can_clip"] is False
    assert body["reason"] == STANDARD_LICENSE_REASON


def test_license_endpoint_own_video_can_clip(monkeypatch, _isolated_data_dir):
    _save_oauth_token(_isolated_data_dir, account_ref=OWN_CHANNEL_ID)
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            video_items=[_video_item(OWN_VIDEO, license_="youtube", channel_id=OWN_CHANNEL_ID)]
        ),
    )
    resp = client.get(
        "/api/videos/license", params={"url": f"https://www.youtube.com/watch?v={OWN_VIDEO}"}
    )
    assert resp.status_code == 200
    assert resp.json()["can_clip"] is True
    assert "your own video" in resp.json()["reason"].lower()


def test_license_endpoint_no_credentials_is_plain_422():
    resp = client.get(
        "/api/videos/license", params={"url": f"https://www.youtube.com/watch?v={CC_VIDEO}"}
    )
    assert resp.status_code == 422
    assert "connect your youtube channel" in resp.json()["detail"].lower()


def test_license_endpoint_bad_url_is_plain_422(monkeypatch):
    _save_api_key()
    resp = client.get("/api/videos/license", params={"url": "https://example.com/not-youtube"})
    assert resp.status_code == 422
    assert "youtube video" in resp.json()["detail"].lower()


def test_license_endpoint_video_not_found_is_honest_422(monkeypatch):
    """Unverifiable licence (YouTube has no such video) is refused
    honestly — never a faked licence result."""
    _save_api_key()
    monkeypatch.setattr(trending_router.httpx, "Client", _fake_client_factory(video_items=[]))
    resp = client.get(
        "/api/videos/license", params={"url": f"https://www.youtube.com/watch?v={STD_VIDEO}"}
    )
    assert resp.status_code == 422
    assert "could not find that video" in resp.json()["detail"].lower()


# ------------------------------------------------------------------ search licensed_only
def test_search_licensed_only_filters_to_cc(monkeypatch):
    _save_api_key()
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            search_ids=[CC_VIDEO, STD_VIDEO],
            video_items=[
                _video_item(CC_VIDEO, license_="creativeCommon", channel_id=OTHER_CHANNEL_ID),
                _video_item(STD_VIDEO, license_="youtube", channel_id=OTHER_CHANNEL_ID),
            ],
        ),
    )
    resp = client.get("/api/trending/search", params={"niche": "gaming", "licensed_only": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["licensed_only"] is True
    assert body["count"] == 1
    assert body["results"][0]["video_id"] == CC_VIDEO
    assert body["results"][0]["license"] == "creativeCommon"
    assert body["results"][0]["license_url"].startswith("https://creativecommons.org/")


def test_search_licensed_only_false_is_v060_behaviour(monkeypatch):
    _save_api_key()
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            search_ids=[CC_VIDEO, STD_VIDEO],
            video_items=[
                _video_item(CC_VIDEO, license_="creativeCommon", channel_id=OTHER_CHANNEL_ID),
                _video_item(STD_VIDEO, license_="youtube", channel_id=OTHER_CHANNEL_ID),
            ],
        ),
    )
    resp = client.get("/api/trending/search", params={"niche": "gaming", "licensed_only": False})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert resp.json()["licensed_only"] is False


def test_search_licensed_only_defaults_to_false(monkeypatch):
    _save_api_key()
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            search_ids=[STD_VIDEO],
            video_items=[_video_item(STD_VIDEO, license_="youtube", channel_id=OTHER_CHANNEL_ID)],
        ),
    )
    resp = client.get("/api/trending/search", params={"niche": "gaming"})
    assert resp.status_code == 200
    assert resp.json()["licensed_only"] is False
    assert resp.json()["count"] == 1


# ------------------------------------------------------------------ /run guardrail
def _run_payload(source_url: str):
    return {
        "niche": "gaming",
        "trending_video_id": "dQw4w9WgXcQ",
        "trending_title": "Best gaming clutch moments",
        "own_source_url": source_url,
        "own_source_type": "video",
        "rights_confirmed": True,
    }


def test_run_accepts_cc_source_video(monkeypatch):
    _save_api_key()
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            video_items=[
                _video_item(CC_VIDEO, license_="creativeCommon", channel_id=OTHER_CHANNEL_ID)
            ]
        ),
    )
    stub = _StubAgent()
    monkeypatch.setattr(trending_router, "_agent", lambda: stub)
    resp = client.post(
        "/api/trending/run", json=_run_payload(f"https://www.youtube.com/watch?v={CC_VIDEO}")
    )
    assert resp.status_code == 202
    assert len(stub.started) == 1


def test_run_refuses_standard_licence_third_party_video(monkeypatch, _isolated_data_dir):
    """GUARDRAIL: a standard-licence video that is provably not the
    user's own (connected channel id known, different channel) is
    refused even though rights were ticked."""
    _save_oauth_token(_isolated_data_dir, account_ref=OWN_CHANNEL_ID)
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            video_items=[_video_item(STD_VIDEO, license_="youtube", channel_id=OTHER_CHANNEL_ID)]
        ),
    )
    stub = _StubAgent()
    monkeypatch.setattr(trending_router, "_agent", lambda: stub)
    resp = client.post(
        "/api/trending/run", json=_run_payload(f"https://www.youtube.com/watch?v={STD_VIDEO}")
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == STANDARD_LICENSE_REASON
    assert stub.started == []


def test_run_accepts_own_standard_licence_video(monkeypatch, _isolated_data_dir):
    _save_oauth_token(_isolated_data_dir, account_ref=OWN_CHANNEL_ID)
    monkeypatch.setattr(
        trending_router.httpx,
        "Client",
        _fake_client_factory(
            video_items=[_video_item(OWN_VIDEO, license_="youtube", channel_id=OWN_CHANNEL_ID)]
        ),
    )
    stub = _StubAgent()
    monkeypatch.setattr(trending_router, "_agent", lambda: stub)
    resp = client.post(
        "/api/trending/run", json=_run_payload(f"https://www.youtube.com/watch?v={OWN_VIDEO}")
    )
    assert resp.status_code == 202
    assert len(stub.started) == 1


def test_run_refuses_unverifiable_licence_honestly(monkeypatch):
    """The video cannot be found on YouTube at all: the run is refused
    with a plain-language reason instead of proceeding on an
    unverified source."""
    _save_api_key()
    monkeypatch.setattr(trending_router.httpx, "Client", _fake_client_factory(video_items=[]))
    stub = _StubAgent()
    monkeypatch.setattr(trending_router, "_agent", lambda: stub)
    resp = client.post(
        "/api/trending/run", json=_run_payload(f"https://www.youtube.com/watch?v={STD_VIDEO}")
    )
    assert resp.status_code == 422
    assert "could not find that video" in resp.json()["detail"].lower()
    assert stub.started == []


def test_run_without_credentials_falls_back_to_rights_attestation(monkeypatch):
    """No YouTube connection / API key: the server cannot verify the
    licence, so the run proceeds on the user's rights confirmation
    (v0.6.0 behaviour) — it never invents a licence verdict."""
    stub = _StubAgent()
    monkeypatch.setattr(trending_router, "_agent", lambda: stub)
    resp = client.post(
        "/api/trending/run", json=_run_payload(f"https://www.youtube.com/watch?v={OWN_VIDEO}")
    )
    assert resp.status_code == 202
    assert len(stub.started) == 1
