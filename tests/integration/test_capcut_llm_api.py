"""API integration tests — CapCut export + more LLM providers (v0.10.0).

Contract (exact paths):
  POST /clips/{candidate_id}/capcut
  GET  /api/llm/providers
  POST /api/llm/{provider}/test {api_key}
  POST /api/llm/{provider}/save {api_key}
  POST /api/llm/active {provider}

Provider HTTP is mocked by monkeypatching ``clipforge_api.app.test_provider``
— no test ever hits a real provider API. The CapCut export writes a
real draft folder into the test data dir and validates it structurally.
No real key exists in this repo; fake keys below are obviously fake.
"""

from __future__ import annotations

import json
import sys as _sys

import pytest
from fastapi.testclient import TestClient

from clipforge_api.app import app

app_module = _sys.modules["clipforge_api.app"]

client = TestClient(app)

FAKE_GROQ_KEY = "gsk_fake-test-key-9999"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own data dir (settings + secrets live there)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)


@pytest.fixture()
def _seeded_clip(tmp_path):
    """A job + candidate with a rendered mp4 and transcript words.

    Yields (job_id, candidate_id). Rows are removed after the test.
    """
    from clipforge.db import ClipCandidate, Job, session_factory  # noqa: PLC0415

    data_dir = tmp_path
    job_dir = data_dir / "jobs"
    clips_dir = data_dir / "clips"
    session = session_factory()()
    try:
        job = Job(title="Ranked games clutch", source_type="local")
        session.add(job)
        session.commit()
        job_id = job.id
        mp4 = clips_dir / job_id / "clip_01.mp4"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"fake mp4")
        (job_dir / job_id).mkdir(parents=True, exist_ok=True)
        (job_dir / job_id / "transcript.json").write_text(
            json.dumps(
                {
                    "engine": "test",
                    "words": [
                        {"text": "stop", "start": 1.0, "end": 1.3},
                        {"text": "making", "start": 1.3, "end": 1.6},
                        {"text": "this", "start": 1.6, "end": 1.8},
                        {"text": "mistake", "start": 1.9, "end": 2.3},
                    ],
                }
            ),
            encoding="utf-8",
        )
        cand = ClipCandidate(
            job_id=job_id,
            n=1,
            start_s=1.0,
            end_s=11.0,
            score=80.0,
            hook_rating=8.0,
            hook_line="Stop making this mistake",
            reason="test candidate",
            status="approved",
            file_path=f"clips/{job_id}/clip_01.mp4",
        )
        session.add(cand)
        session.commit()
        candidate_id = cand.id
    finally:
        session.close()
    yield job_id, candidate_id
    session = session_factory()()
    try:
        session.query(ClipCandidate).filter(
            ClipCandidate.job_id == job_id
        ).delete(synchronize_session=False)
        session.query(Job).filter(Job.id == job_id).delete(
            synchronize_session=False
        )
        session.commit()
    finally:
        session.close()


# ------------------------------------------------------------- CapCut export
def test_capcut_export_creates_valid_draft(_seeded_clip, tmp_path):
    job_id, candidate_id = _seeded_clip
    response = client.post(f"/clips/{candidate_id}/capcut")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["draft_name"] == f"ClipForge_{job_id[:8]}_clip_01"
    assert body["tracks"] == ["ClipForge Video", "Hook Title", "Captions"]
    assert "CapCut draft ready" in body["message"]
    assert body["draft_path"] in body["message"]
    assert body["in_capcut_folder"] is False

    from clipforge.capcut import validate_draft  # noqa: PLC0415

    validation = validate_draft(body["draft_path"])
    assert validation["ok"] is True, validation["errors"]
    assert validation["duration_s"] == pytest.approx(10.0)
    draft_dir = tmp_path / "capcut_drafts" / body["draft_name"]
    assert (draft_dir / "draft_content.json").is_file()
    assert (draft_dir / "draft_meta_info.json").is_file()
    doc = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    # Captions come from the transcript words, shifted to clip time.
    texts = [json.loads(t["content"])["text"] for t in doc["materials"]["texts"]]
    assert "stop making this" in texts
    assert "mistake" in texts


def test_capcut_export_is_idempotent(_seeded_clip):
    _job_id, candidate_id = _seeded_clip
    first = client.post(f"/clips/{candidate_id}/capcut")
    second = client.post(f"/clips/{candidate_id}/capcut")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["draft_path"] == second.json()["draft_path"]


def test_capcut_export_unknown_clip_is_404():
    response = client.post("/clips/does-not-exist/capcut")
    assert response.status_code == 404


def test_capcut_export_without_rendered_mp4_is_plain_422(_seeded_clip, tmp_path):
    from clipforge.db import ClipCandidate, session_factory  # noqa: PLC0415

    job_id, candidate_id = _seeded_clip
    (tmp_path / "clips" / job_id / "clip_01.mp4").unlink()
    session = session_factory()()
    try:
        cand = session.query(ClipCandidate).filter_by(id=candidate_id).one()
        cand.file_path = None
        session.commit()
    finally:
        session.close()
    response = client.post(f"/clips/{candidate_id}/capcut")
    assert response.status_code == 422
    assert "no rendered mp4 yet" in response.json()["detail"]


# ------------------------------------------------------------ LLM providers
def test_list_providers_has_all_v010_providers():
    response = client.get("/api/llm/providers")
    assert response.status_code == 200
    providers = {p["id"]: p for p in response.json()}
    assert set(providers) == {
        "gemini", "openai", "grok", "groq", "openrouter",
        "anthropic", "ollama", "rules",
    }
    for provider_id in ("grok", "groq", "openrouter", "anthropic"):
        entry = providers[provider_id]
        assert entry["needs_key"] is True
        assert entry["key_url"].startswith("https://")
        assert entry["models"]
        assert entry["configured"] is False
        assert entry["active"] is False
    assert providers["ollama"]["needs_key"] is False
    assert providers["ollama"]["configured"] is False
    assert providers["rules"]["active"] is True  # nothing configured yet


def test_save_and_test_groq_key_mocked(monkeypatch):
    def _fake_test(provider_id, api_key, model=None, **kwargs):
        assert provider_id == "groq"
        assert api_key == FAKE_GROQ_KEY
        return {"ok": True, "model": model, "message": "Groq is working (model test answered)."}

    monkeypatch.setattr(app_module, "test_provider", _fake_test)
    save = client.post("/api/llm/groq/save", json={"api_key": FAKE_GROQ_KEY})
    assert save.status_code == 200, save.text
    body = save.json()
    assert body["configured"] is True
    assert body["last4"] == "9999"
    assert FAKE_GROQ_KEY not in save.text
    test = client.post("/api/llm/groq/test", json={})
    assert test.status_code == 200
    assert test.json()["ok"] is True
    providers = {p["id"]: p for p in client.get("/api/llm/providers").json()}
    assert providers["groq"]["configured"] is True
    assert providers["groq"]["active"] is True


def test_test_provider_without_key_is_plain_422():
    response = client.post("/api/llm/grok/test", json={})
    assert response.status_code == 422
    assert "No Grok" in response.json()["detail"]


def test_test_unknown_provider_is_422():
    response = client.post("/api/llm/nope/test", json={"api_key": "x"})
    assert response.status_code == 422


def test_ollama_test_reports_not_running_without_mock():
    # No Ollama server runs in CI: the endpoint must answer in plain
    # language, not 500 and not hang on a real network call.
    response = client.post("/api/llm/ollama/test", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "Ollama is not answering" in body["message"]


def test_set_active_provider_and_rules(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "test_provider",
        lambda provider_id, api_key, model=None, **kwargs: {
            "ok": True, "model": model, "message": "ok",
        },
    )
    client.post("/api/llm/anthropic/save", json={"api_key": "sk-ant-fake-test-key-7777"})
    response = client.post("/api/llm/active", json={"provider": "anthropic"})
    assert response.status_code == 200
    assert response.json()["active"] == "anthropic"
    providers = {p["id"]: p for p in client.get("/api/llm/providers").json()}
    assert providers["anthropic"]["active"] is True

    response = client.post("/api/llm/active", json={"provider": "rules"})
    assert response.status_code == 200
    assert response.json()["active"] == "rules"

    response = client.post("/api/llm/active", json={"provider": "nope"})
    assert response.status_code == 422


def test_gemini_endpoints_still_work_via_generic_route(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "test_provider",
        lambda provider_id, api_key, model=None, **kwargs: {
            "ok": True, "model": model, "message": "Gemini is working (model test answered).",
        },
    )
    response = client.post(
        "/api/llm/gemini/test", json={"api_key": "AIza-fake-test-key-1234"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    response = client.post(
        "/api/llm/gemini/save", json={"api_key": "AIza-fake-test-key-1234"}
    )
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert "AIza-fake" not in response.text
