"""Setup-wizard state API (v0.5.0, worker 3).

GET/POST /api/setup/state — the wizard's persisted progress (which steps
are done, current step, niche + source choice). No secret is ever
accepted or stored here: the Gemini key goes through the LLM endpoints,
never this one. Tests run against a temp CLIPFORGE_DATA_DIR so the
repo's real setup.json is never touched.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
from fastapi.testclient import TestClient  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="clipforge-setup-test-"))
os.environ["CLIPFORGE_DATA_DIR"] = str(_TMP)

from clipforge_api.app import app  # noqa: E402
from clipforge_api.setup import DEFAULT_STATE  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def _state_file() -> Path:
    return _TMP / "setup.json"


def test_get_state_returns_defaults_when_never_started():
    if _state_file().exists():
        _state_file().unlink()
    res = client.get("/api/setup/state")
    assert res.status_code == 200
    body = res.json()
    assert body["current_step"] == 0
    assert body["steps"] == {k: False for k in DEFAULT_STATE["steps"]}
    assert body["niche"] == ""
    assert body["source_type"] == "video"
    assert body["completed"] is False


def test_post_state_persists_partial_updates_and_marks_steps():
    res = client.post(
        "/api/setup/state",
        json={"current_step": 3, "steps": {"ffmpeg": True, "gemini": True}, "niche": "tech"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["current_step"] == 3
    assert body["steps"]["ffmpeg"] is True
    assert body["steps"]["gemini"] is True
    assert body["steps"]["channel"] is False  # untouched steps stay false
    assert body["niche"] == "tech"
    assert body["completed"] is False
    # The file on disk holds the same state (resumable across restarts).
    assert json.loads(_state_file().read_text(encoding="utf-8")) == body


def test_post_state_completes_when_last_step_done():
    res = client.post(
        "/api/setup/state",
        json={"current_step": 4, "steps": {"agent": True}},
    )
    assert res.status_code == 200
    assert res.json()["completed"] is True


def test_post_state_rejects_unknown_fields_and_bad_values():
    for bad in (
        {"steps": {"bogus_step": True}},
        {"steps": {"ffmpeg": "yes"}},
        {"current_step": 99},
        {"current_step": -1},
        {"source_type": "playlist"},
        {"unknown_key": 1},
    ):
        res = client.post("/api/setup/state", json=bad)
        assert res.status_code == 422, bad


def test_get_state_ignores_corrupt_file():
    _state_file().write_text("{not json", encoding="utf-8")
    res = client.get("/api/setup/state")
    assert res.status_code == 200
    assert res.json()["current_step"] == 0


def test_post_state_never_accepts_a_secret():
    # A key smuggled into the payload must be rejected, not stored.
    res = client.post("/api/setup/state", json={"api_key": "sk-secret-value"})
    assert res.status_code == 422
    raw = _state_file().read_text(encoding="utf-8") if _state_file().exists() else ""
    assert "sk-secret-value" not in raw
