"""API integration tests — ClipForge Agent + Gemini endpoints (v0.5.0).

Contract (exact paths):
  GET  /api/agent/status
  GET/PUT /api/agent/config
  POST /api/agent/start | POST /api/agent/stop
  GET  /api/llm/providers
  POST /api/llm/gemini/test {api_key}
  POST /api/llm/gemini/save {api_key}

Gemini HTTP is mocked by monkeypatching ``clipforge_api.app.test_gemini`` —
no test ever hits the real Gemini API. Every error asserted here is
plain language: no traceback, no secret value in any response.
"""

from __future__ import annotations

# ``clipforge_api/__init__.py`` re-exports the FastAPI instance as
# ``clipforge_api.app``, so attribute lookup on the package finds the
# app, not the module. The real module lives in sys.modules.
import sys as _sys  # noqa: E402
import time

import pytest
from clipforge_api.app import app  # noqa: E402
from fastapi.testclient import TestClient

from tests.unit.test_agent_niches import GAMING_TEXT  # noqa: E402

app_module = _sys.modules["clipforge_api.app"]

client = TestClient(app)

SOURCE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
DEMO_TRANSCRIPT = (GAMING_TEXT + " ") * 3
FAKE_KEY = "AIzaFakeGeminiKey1234"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own data dir (agent.json + secrets live there)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_agent_rows():
    """The suite shares one throwaway DB per pytest process: remove
    exactly the rows this agent created (jobs flagged agent in meta),
    so later tests see the DB as they left it."""
    from clipforge.db import ClipCandidate, Job, Post, session_factory  # noqa: PLC0415

    yield
    session = session_factory()()
    try:
        agent_jobs = [j for j in session.query(Job).all() if (j.meta_json or {}).get("agent")]
        ids = {j.id for j in agent_jobs}
        if not ids:
            return
        cand_ids = [
            c.id for c in session.query(ClipCandidate).filter(ClipCandidate.job_id.in_(ids)).all()
        ]
        if cand_ids:
            session.query(Post).filter(Post.clip_candidate_id.in_(cand_ids)).delete(
                synchronize_session=False
            )
        session.query(ClipCandidate).filter(ClipCandidate.job_id.in_(ids)).delete(
            synchronize_session=False
        )
        session.query(Job).filter(Job.id.in_(ids)).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _put_config(**overrides):
    payload = {
        "enabled": True,
        "source_type": "video",
        "source_url": SOURCE_URL,
        "niche": "gaming",
        "auto_post": False,
        "rights_confirmed": True,
        "transcript_text": DEMO_TRANSCRIPT,
        "top_n": 3,
    }
    payload.update(overrides)
    return client.put("/api/agent/config", json=payload)


def _wait_for_state(want: str, timeout_s: float = 15.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        last = client.get("/api/agent/status").json()
        if last["state"] == want:
            return last
        if last["state"] == "error":
            raise AssertionError(f"agent errored: {last.get('last_error')}")
        time.sleep(0.05)
    raise AssertionError(f"agent never reached {want!r}: {last}")


# ---------------------------------------------------------------- status/config
def test_agent_status_contract():
    body = client.get("/api/agent/status").json()
    assert body["enabled"] is False
    assert body["running"] is False
    assert body["current_job"] is None
    assert isinstance(body["accounts"], list)
    assert body["auto_post"] is False  # default OFF
    assert "own" in body["rights_notice"].lower()
    assert "re-upload" in body["rights_notice"]


def test_agent_config_get_put_round_trip():
    got = client.get("/api/agent/config").json()
    assert got["enabled"] is False
    assert got["auto_post"] is False
    assert got["rights_confirmed"] is False
    assert set(got["niches"]) == {
        "gaming",
        "tech",
        "vlogging",
        "education",
        "finance",
        "fitness",
        "cooking",
        "music",
    }
    assert "own" in got["rights_notice"].lower()

    saved = _put_config()
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["enabled"] is True
    assert body["source_url"] == SOURCE_URL
    assert body["niche"] == "gaming"
    assert body["auto_post"] is False
    assert body["rights_confirmed"] is True

    again = client.get("/api/agent/config").json()
    assert again["source_url"] == SOURCE_URL


def test_agent_config_normalizes_free_text_niche():
    saved = _put_config(niche="Street Food")
    assert saved.status_code == 200, saved.text
    assert saved.json()["niche"] == "street-food"


def test_agent_config_rejects_bad_source_plain_language():
    resp = _put_config(source_url="not-a-url")
    assert resp.status_code == 422
    assert "http" in resp.json()["detail"].lower()
    assert "Traceback" not in resp.text


def test_agent_config_rejects_bad_platform_plain_language():
    resp = _put_config(platforms=["youtube", "myspace"])
    assert resp.status_code == 422
    assert "myspace" in resp.json()["detail"]
    assert "Traceback" not in resp.text


def test_agent_config_can_save_before_rights_confirmed():
    resp = _put_config(rights_confirmed=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["rights_confirmed"] is False


# ---------------------------------------------------------------- start/stop
def test_agent_start_requires_rights_and_enabled():
    _put_config(rights_confirmed=False)
    resp = client.post("/api/agent/start")
    assert resp.status_code == 422
    assert "confirm the source" in resp.json()["detail"]

    _put_config(enabled=False, rights_confirmed=True)
    resp = client.post("/api/agent/start")
    assert resp.status_code == 422
    assert "agent is off" in resp.json()["detail"].lower()


def test_agent_start_stop_and_status_end_to_end():
    _put_config()
    started = client.post("/api/agent/start")
    assert started.status_code == 200, started.text
    assert started.json()["ok"] is True

    done = _wait_for_state("done")
    assert done["queued"] == 12  # 3 clips x 4 platforms
    assert done["engine"] == "rules"  # no keys configured
    assert done["demo"] is True
    assert done["current_job"]
    # Per-account roll-up: one row per platform with queued posts.
    by_platform = {a["platform"]: a for a in done["accounts"]}
    assert set(by_platform) >= {"youtube", "tiktok", "instagram", "facebook"}
    assert all(by_platform[p]["queued"] >= 3 for p in by_platform)
    assert all(by_platform[p]["published"] == 0 for p in by_platform)

    # Approval-first: posts wait in the queue, nothing published.
    queue = client.get("/posts?status=pending_approval").json()
    assert len(queue) >= 12
    assert all((p.get("seo") or {}).get("engine") == "rules" for p in queue)

    stopped = client.post("/api/agent/stop")
    assert stopped.status_code == 200
    assert stopped.json()["ok"] is True


def test_agent_stop_on_idle_is_ok():
    resp = client.post("/api/agent/stop")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------- llm providers
def test_llm_providers_list_contract():
    providers = client.get("/api/llm/providers").json()
    assert isinstance(providers, list)
    by_id = {p["id"]: p for p in providers}
    assert set(by_id) == {"gemini", "openai", "rules"}
    assert by_id["rules"]["configured"] is True
    assert by_id["rules"]["active"] is True  # no keys -> rules
    assert by_id["gemini"]["configured"] is False
    assert by_id["gemini"]["last4"] == ""
    assert by_id["gemini"]["model"] == "gemini-2.0-flash"
    assert "gemini-1.5-flash" in by_id["gemini"]["models"]


def test_gemini_save_test_providers_round_trip(monkeypatch):
    # Save: masked view only, full key never in the response.
    saved = client.post("/api/llm/gemini/save", json={"api_key": FAKE_KEY})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["configured"] is True
    assert body["last4"] == "1234"
    assert FAKE_KEY not in saved.text

    providers = client.get("/api/llm/providers").json()
    by_id = {p["id"]: p for p in providers}
    assert by_id["gemini"]["configured"] is True
    assert by_id["gemini"]["last4"] == "1234"
    assert by_id["gemini"]["active"] is True  # gemini wins once configured
    assert by_id["rules"]["active"] is False
    assert FAKE_KEY not in client.get("/api/llm/providers").text

    # Test the SAVED key (no key in the request body) — mocked HTTP.
    monkeypatch.setattr(
        app_module,
        "test_gemini",
        lambda key, model="gemini-2.0-flash": {
            "ok": True,
            "model": model,
            "message": f"Gemini is working (model {model} answered).",
        },
    )
    tested = client.post("/api/llm/gemini/test", json={})
    assert tested.status_code == 200, tested.text
    assert tested.json() == {
        "ok": True,
        "model": "gemini-2.0-flash",
        "message": "Gemini is working (model gemini-2.0-flash answered).",
    }
    assert FAKE_KEY not in tested.text

    # Clear: empty key removes it; agent falls back to rules.
    cleared = client.post("/api/llm/gemini/save", json={"api_key": ""})
    assert cleared.json()["configured"] is False
    by_id = {p["id"]: p for p in client.get("/api/llm/providers").json()}
    assert by_id["gemini"]["configured"] is False
    assert by_id["rules"]["active"] is True


def test_gemini_test_with_explicit_key_mocked(monkeypatch):
    seen: dict = {}

    def _fake_test(key, model="gemini-2.0-flash"):
        seen["key"] = key
        seen["model"] = model
        return {"ok": False, "model": model, "message": "The Gemini API key was rejected."}

    monkeypatch.setattr(app_module, "test_gemini", _fake_test)
    resp = client.post(
        "/api/llm/gemini/test",
        json={"api_key": "bad-key-0000", "model": "gemini-1.5-flash"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "rejected" in resp.json()["message"]
    assert "bad-key-0000" not in resp.text
    assert seen["key"] == "bad-key-0000"
    assert seen["model"] == "gemini-1.5-flash"


def test_gemini_test_no_key_is_plain_422():
    resp = client.post("/api/llm/gemini/test", json={})
    assert resp.status_code == 422
    assert "no gemini api key" in resp.json()["detail"].lower()
    assert "Traceback" not in resp.text


def test_gemini_test_unknown_model_is_plain_422():
    resp = client.post("/api/llm/gemini/test", json={"api_key": FAKE_KEY, "model": "gemini-9.9"})
    assert resp.status_code == 422
    assert "gemini-9.9" in resp.json()["detail"]


def test_gemini_save_unknown_model_is_plain_422():
    resp = client.post("/api/llm/gemini/save", json={"api_key": FAKE_KEY, "model": "gemini-9.9"})
    assert resp.status_code == 422


def test_settings_shows_gemini_alongside_llm():
    client.post("/api/llm/gemini/save", json={"api_key": FAKE_KEY})
    settings = client.get("/settings").json()
    gemini = settings["llm"]["gemini"]
    assert gemini["api_key"]["configured"] is True
    assert gemini["api_key"]["last4"] == "1234"
    assert FAKE_KEY not in client.get("/settings").text
