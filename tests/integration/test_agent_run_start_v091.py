"""v0.9.1 agent-run-start regressions (Worker A).

Root causes (reproduced 2026-11-23 against v0.9.0, see report):
  A. The dashboard's "Run the agent on this source" button saved the
     source/niche via PUT /api/agent/config WITHOUT ``enabled`` — a
     partial update, so the stored config stayed ``enabled: false``
     and POST /api/agent/start 422d "The agent is off". Pasting ANY
     video link on the dashboard started nothing. The fix lives in
     the web UI (``startAgentOnSource`` in lib/api.ts, unit-tested in
     lib/api.test.ts); these tests pin the server contract it relies
     on (the old shape still 422s; the fixed shape starts and runs).
  B. The dashboard's clip-job path required a passed licence check
     before the Start button appeared at all — and the check needs a
     YouTube connection or API key, so an own video could never start
     on a fresh install. The server already accepts the own-content
     declaration (POST /jobs, own_content_declaration=true) with no
     licence check; the UI now exposes it. These tests prove that
     server path actually runs a pasted YouTube link end to end
     (fake yt-dlp on PATH — the real download cannot run on this VM,
     the egress proxy MITM-blocks yt-dlp's YouTube fetch) and that a
     failed stage surfaces a plain-language error instead of a silent
     dead-end.

All local: no platform credentials, no network. Real FFmpeg
rendering is exercised with a small generated test video.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
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

from tests.unit.test_agent_niches import GAMING_TEXT  # noqa: E402

client = TestClient(app)

SOURCE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
CC_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
DEMO_TRANSCRIPT = (GAMING_TEXT + " ") * 3
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

_CREATED_JOBS: list[str] = []


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own data dir (agent.json + job artifacts).

    monkeypatch (not a module-scoped set/pop): the previous value is
    restored after the test, so a later module that set
    CLIPFORGE_DATA_DIR at import time (test_setup_state_api) keeps
    seeing its own dir. A set/pop here would leave the variable unset
    and break those tests — the pre-existing full-suite ordering
    sensitivity other modules still have."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _cleanup_jobs():
    """Remove exactly the jobs this module created (agent jobs + pasted
    YouTube jobs), so later modules see the shared DB as they left it."""
    yield
    from clipforge.db import ClipCandidate, Job, Post, session_factory  # noqa: PLC0415

    session = session_factory()()
    try:
        agent_jobs = [j for j in session.query(Job).all() if (j.meta_json or {}).get("agent")]
        ids = {j.id for j in agent_jobs} | set(_CREATED_JOBS)
        _CREATED_JOBS.clear()
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


@pytest.fixture
def fake_ytdlp(tmp_path, monkeypatch):
    """A fake yt-dlp on PATH: answers --version / --flat-playlist and
    "downloads" a YouTube URL by generating a small mp4 with FFmpeg at
    the -o template location (the real download cannot run here)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    shim = bindir / "yt-dlp"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import re, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('2024.01.01'); raise SystemExit(0)\n"
        "if '--flat-playlist' in args:\n"
        "    print('dQw4w9WgXcQ'); raise SystemExit(0)\n"
        "url = args[-1]\n"
        "m = re.search(r'[?&]v=([\\w-]{11})', url) or re.search(r'/([\\w-]{11})$', url)\n"
        "vid = m.group(1) if m else 'testvideo01'\n"
        "if '--simulate' in args:\n"
        "    import json as _json\n"
        "    info = {'id': vid, 'title': 'Fake video', 'duration': 40,\n"
        "            'is_live': False, '_type': 'video'}\n"
        "    print(_json.dumps(info))\n"
        "    raise SystemExit(0)\n"
        "out = args[args.index('-o') + 1].replace('%(id)s', vid).replace('%(ext)s', 'mp4')\n"
        "subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',\n"
        "    'testsrc=duration=40:size=320x240:rate=10', '-f', 'lavfi', '-i',\n"
        "    'sine=frequency=440:duration=40', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',\n"
        "    '-c:a', 'aac', '-b:a', '128k', '-shortest', out],\n"
        "    check=True, timeout=120)\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    # v0.10.3: ingest pre-flights the link with oEmbed first — this
    # module is hermetic ("no network"), so the check is stubbed to
    # "available" and the fake answers the metadata probe above.
    monkeypatch.setattr("clipforge.ingest._oembed_status", lambda url: "available")
    return str(shim)


def _wait_for_job(job_id: str, want: set[str], timeout_s: float = 180.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/jobs/{job_id}").json()
        if last["status"] in want:
            return last
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} never reached {sorted(want)}: {last}")


def _wait_for_agent_state(want: set[str], timeout_s: float = 60.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        last = client.get("/api/agent/status").json()
        if last["state"] in want:
            return last
        time.sleep(0.1)
    raise AssertionError(f"agent never reached {sorted(want)}: {last}")


def _put_agent_config(**overrides):
    """Exactly the dashboard's PUT /api/agent/config shape (v0.9.1):
    source + niche + rights — ``enabled`` only when the caller passes
    it, mirroring what the UI used to send (nothing) vs now sends."""
    payload = {
        "source_type": "video",
        "source_url": SOURCE_URL,
        "niche": "gaming",
        "rights_confirmed": True,
    }
    payload.update(overrides)
    return client.put("/api/agent/config", json=payload)


# ------------------------------------------------------- root cause A: agent off
def test_dashboard_agent_shape_without_enabled_cannot_start():
    """The v0.9.0 dashboard bug, pinned as the server contract: PUT
    the source/niche/rights WITHOUT ``enabled`` (a partial update
    leaves the stored config off) and POST /api/agent/start 422s
    "agent is off" — pasting any link started nothing. The dashboard
    must send enabled:true (see startAgentOnSource, web unit test).
    (Runs first in this module, against a fresh data dir, so the
    stored config starts enabled:false — no reset needed.)"""
    saved = _put_agent_config()
    assert saved.status_code == 200, saved.text
    assert saved.json()["enabled"] is False
    started = client.post("/api/agent/start")
    assert started.status_code == 422
    assert "agent is off" in started.json()["detail"].lower()
    assert client.get("/api/agent/status").json()["running"] is False


def test_dashboard_agent_shape_with_enabled_starts_and_finishes():
    """The fixed dashboard shape (PUT with enabled:true, then start)
    runs to done: 3 clips x 4 platforms queued for approval."""
    saved = _put_agent_config(enabled=True, transcript_text=DEMO_TRANSCRIPT)
    assert saved.status_code == 200, saved.text
    assert saved.json()["enabled"] is True
    started = client.post("/api/agent/start")
    assert started.status_code == 200, started.text
    assert started.json()["ok"] is True
    done = _wait_for_agent_state({"done", "error"})
    assert done["state"] == "done", done.get("last_error")
    assert done["queued"] == 12  # 3 clips x 4 platforms
    assert done["demo"] is True
    assert done["current_job"]
    assert client.post("/api/agent/stop").status_code == 200


# ------------------------------------------- root cause B: pasted link runs
def test_own_content_youtube_job_runs_past_ingest_with_ytdlp(fake_ytdlp):
    """POST /jobs for a pasted YouTube link with the own-content
    declaration (the path the dashboard now exposes without a licence
    check) auto-runs through every stage to review: ingest (fake
    yt-dlp download) -> transcribe (supplied text) -> score -> render
    -> SEO. Proves the run-start path does real work once it can start."""
    resp = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": CC_URL,
            "title": "Own video",
            "own_content_declaration": True,
            "transcript_text": TRANSCRIPT_TEXT,
            "top_n": 3,
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]
    _CREATED_JOBS.append(job_id)
    body = _wait_for_job(job_id, {"review", "failed", "blocked"})
    assert body["status"] == "review", body
    assert body["demo"] is True
    assert body["progress_pct"] == 95.0
    assert body["failed_stage"] is None
    assert body["candidates"] >= 1
    stages = {s["stage"]: s for s in body["stages"]}
    assert {"ingest", "transcribe", "score", "render", "seo"} <= set(stages)
    assert all(s["status"] == "done" for s in stages.values())
    assert stages["ingest"]["detail"]["source"]["source_kind"] == "youtube"
    candidates = client.get(f"/jobs/{job_id}/candidates").json()
    assert len(candidates) >= 1
    assert Path(candidates[0]["file_path"]).is_file()


def test_agent_real_video_starts_with_ytdlp_and_fails_loudly(fake_ytdlp):
    """The agent on a REAL video link (no demo transcript): with
    yt-dlp present the run starts, ingest succeeds, and the missing
    transcription backend blocks the job with a plain-language error
    surfaced on the agent state — the run does work and fails loudly,
    never sits looking idle."""
    if importlib.util.find_spec("faster_whisper") is not None or os.environ.get("OPENAI_API_KEY"):
        pytest.skip("a transcription backend is installed — this test needs none")
    # Clear the demo transcript the previous test saved: PUT is a
    # partial update and never clears transcript_text (explicit null
    # is ignored), so a stale demo transcript would hijack this real
    # run into the demo path. Test-only store write; the UI never sets
    # transcript_text, so real dashboard runs are unaffected.
    from clipforge.agent import AgentStore  # noqa: PLC0415

    store = AgentStore(os.environ["CLIPFORGE_DATA_DIR"])
    cfg = store.load_config()
    cfg.transcript_text = None
    store.save_config(cfg)
    saved = _put_agent_config(enabled=True)  # no transcript_text: real path
    assert saved.status_code == 200, saved.text
    started = client.post("/api/agent/start")
    assert started.status_code == 200, started.text
    state = _wait_for_agent_state({"error", "done"})
    assert state["state"] == "error", state
    assert state["last_error"] is not None
    assert "transcri" in state["last_error"].lower() or "whisper" in state["last_error"].lower()
    assert "Traceback" not in state["last_error"]
    # The job got past ingest: the download is not where it died.
    job_id = state["current_job"]
    assert job_id
    _CREATED_JOBS.append(job_id)
    body = _wait_for_job(job_id, {"blocked", "failed"})
    assert body["status"] == "blocked"
    assert body["blocked_by"]["code"] == "transcription_backend_missing"
    stages = {s["stage"]: s for s in body["stages"]}
    assert stages["ingest"]["status"] == "done"
    assert stages["transcribe"]["status"] == "blocked"
    assert client.post("/api/agent/stop").status_code == 200


# --------------------------------------- failed stages are loud, not silent
def test_blocked_youtube_job_surfaces_plain_error_and_logs(monkeypatch):
    """No yt-dlp at all: the auto-run blocks at ingest with a
    plain-language error naming yt-dlp + the next step, on the job,
    in blocked_by, and in the per-stage log — the job never sits in
    pending/queued looking idle, and nothing shows a traceback."""
    # v0.9.2: the worker gate uses the verified probe
    # (clipforge.requirements.probe_yt_dlp — the binary must *run*),
    # not shutil.which: a bare which-stub no longer hides a real
    # yt-dlp (e.g. ~/.local/bin), so stub the probe to report
    # "missing" instead.
    import clipforge.requirements as requirements_mod

    monkeypatch.setattr(
        requirements_mod,
        "probe_yt_dlp",
        lambda *args, **kwargs: {
            "found": False,
            "path": "",
            "version": "",
            "broken": False,
            "broken_reason": "",
            "source": "",
        },
    )
    resp = client.post(
        "/jobs",
        json={
            "source_type": "youtube",
            "source_url": SOURCE_URL,
            "title": "Own video",
            "own_content_declaration": True,
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]
    _CREATED_JOBS.append(job_id)
    body = _wait_for_job(job_id, {"blocked", "failed"})
    assert body["status"] == "blocked"
    assert "yt-dlp" in (body["error"] or "")
    assert body["blocked_by"]["dependency"] == "yt-dlp"
    assert body["blocked_by"]["required_action"]
    assert "Install yt-dlp" in body["blocked_by"]["required_action"]
    assert body["failed_stage"] == "ingest"
    assert "Traceback" not in str(body)
    logs = client.get(f"/jobs/{job_id}/logs").json()["logs"]
    error_entries = [e for e in logs if e["level"] == "error"]
    assert error_entries and error_entries[0]["stage"] == "ingest"
    assert "yt-dlp" in error_entries[0]["message"]
