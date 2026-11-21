"""Unit tests — ClipForge Agent config, persistence, runs (v0.5.0).

Runs use the demo transcript path (no network, no FFmpeg, no keys):
the agent scores supplied text, writes niche SEO, and queues posts
for approval. Live publishing is never attempted — dry-run only.
"""

from __future__ import annotations

import shutil
import threading
import time

import pytest

from clipforge.agent import (
    RIGHTS_NOTICE,
    Agent,
    AgentConfig,
    AgentError,
    AgentStore,
    get_agent,
)
from clipforge.db import ClipCandidate, Job, Post, init_db, session_factory
from clipforge.llm import resolve_engine
from tests.unit.test_agent_niches import GAMING_TEXT

# The tables are created by the API app at import; unit tests that use
# the DB directly must create them (conftest already points the URL
# at the throwaway test database).
init_db()

DEMO_TRANSCRIPT = (GAMING_TEXT + " ") * 3
SOURCE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _config(**overrides) -> AgentConfig:
    base = {
        "enabled": True,
        "source_type": "video",
        "source_url": SOURCE_URL,
        "niche": "gaming",
        "auto_post": False,
        "rights_confirmed": True,
        "transcript_text": DEMO_TRANSCRIPT,
        "top_n": 3,
    }
    base.update(overrides)
    return AgentConfig(**base)


def _agent(tmp_path) -> Agent:
    return Agent(tmp_path, session_factory())


@pytest.fixture(autouse=True)
def _clean_agent_rows():
    """The suite shares one throwaway DB per pytest process: remove
    exactly the rows this agent created (jobs flagged agent in meta),
    so later tests see the DB as they left it."""
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


# ---------------------------------------------------------------- config
def test_config_validate_ok():
    _config().validate()


def test_config_validate_requires_rights():
    with pytest.raises(AgentError, match="confirm the source is yours"):
        _config(rights_confirmed=False).validate()
    assert "own" in RIGHTS_NOTICE.lower()


def test_config_validate_bad_url():
    with pytest.raises(AgentError, match="http"):
        _config(source_url="not-a-url").validate()


def test_config_validate_channel_must_be_youtube():
    with pytest.raises(AgentError, match="YouTube channel"):
        _config(source_type="channel", source_url="https://example.com/@me").validate()
    _config(source_type="channel", source_url="https://www.youtube.com/@me").validate()


def test_config_validate_structure_allows_unconfirmed_rights():
    # The settings page must be able to SAVE a config before the user
    # ticks the rights box; only running requires it.
    _config(rights_confirmed=False).validate_structure()


def test_config_validate_bad_platform_and_top_n():
    with pytest.raises(AgentError, match="Unknown platforms"):
        _config(platforms=["youtube", "myspace"]).validate()
    with pytest.raises(AgentError, match="between 1 and 10"):
        _config(top_n=99).validate()


def test_agent_store_round_trip(tmp_path):
    store = AgentStore(tmp_path)
    config = _config()
    store.save_config(config)
    loaded = store.load_config()
    assert loaded.source_url == SOURCE_URL
    assert loaded.niche == "gaming"
    assert loaded.auto_post is False  # default OFF, persisted
    assert loaded.rights_confirmed is True
    state = store.load_state()
    assert state.status == "idle"
    store.save_state(state)
    assert AgentStore(tmp_path).load_state().status == "idle"


def test_get_agent_is_shared_per_data_dir(tmp_path):
    a1 = get_agent(tmp_path, session_factory())
    a2 = get_agent(tmp_path, session_factory())
    assert a1 is a2


# ---------------------------------------------------------------- run (demo)
def test_run_once_end_to_end_rules_no_keys(tmp_path):
    agent = _agent(tmp_path)
    assert resolve_engine(agent._settings())[0] == "rules"
    state = agent.run_once(_config())
    assert state["state"] == "done"
    assert state["engine"] == "rules"
    assert state["demo"] is True
    assert state["queued"] == 3 * 4  # top_n clips x 4 platforms
    assert state["approved"] == 0
    assert state["published"] == 0
    assert state["current_job"]

    session = session_factory()()
    try:
        job = session.get(Job, state["current_job"])
        assert job is not None
        assert job.own_content is True
        assert job.meta_json["demo"] is True
        assert job.meta_json["niche"] == "gaming"
        assert job.meta_json["engine"] == "rules"
        cands = session.query(ClipCandidate).filter_by(job_id=job.id).all()
        assert len(cands) == 3
        assert all(c.score > 0 for c in cands)
        posts = session.query(Post).filter_by().all()
        agent_posts = [p for p in posts if p.clip_candidate_id in {c.id for c in cands}]
        assert len(agent_posts) == 12
        # Approval-first: everything waits for approval, nothing published.
        assert all(p.status == "pending_approval" for p in agent_posts)
        assert all((p.seo_json or {}).get("engine") == "rules" for p in agent_posts)
        # SEO is niche-shaped: gaming hashtags on every platform.
        assert all("gaming" in (p.seo_json or {}).get("hashtags", []) for p in agent_posts)
    finally:
        session.close()


def test_run_once_requires_rights(tmp_path):
    agent = _agent(tmp_path)
    session = session_factory()()
    try:
        jobs_before = session.query(Job).count()
    finally:
        session.close()
    with pytest.raises(AgentError, match="confirm the source is yours"):
        agent.run_once(_config(rights_confirmed=False))
    session = session_factory()()
    try:
        assert session.query(Job).count() == jobs_before
    finally:
        session.close()


def test_run_once_requires_enabled(tmp_path):
    agent = _agent(tmp_path)
    with pytest.raises(AgentError, match="agent is off"):
        agent.run_once(_config(enabled=False))


def test_run_once_channel_without_ytdlp_plain_error(tmp_path):
    import shutil

    if shutil.which("yt-dlp"):
        pytest.skip("yt-dlp is installed here — channel path is live")
    agent = _agent(tmp_path)
    with pytest.raises(AgentError, match="yt-dlp"):
        agent.run_once(
            _config(
                source_type="channel",
                source_url="https://www.youtube.com/@me",
                transcript_text=None,
            )
        )


def test_run_once_auto_post_true_publishes_dry_run_only(tmp_path):
    agent = _agent(tmp_path)
    state = agent.run_once(_config(auto_post=True))
    assert state["state"] == "done"
    assert state["approved"] == 12
    # Dry-run publishes succeed for the URL-based adapters; YouTube
    # needs a rendered clip file, so it fails gracefully instead.
    assert state["published"] >= 1
    session = session_factory()()
    try:
        posts = (
            session.query(Post)
            .filter(
                Post.clip_candidate_id.in_(
                    session.query(ClipCandidate.id).filter(
                        ClipCandidate.job_id == state["current_job"]
                    )
                )
            )
            .all()
        )
        assert all(p.status in {"dry_run", "failed"} for p in posts)
        assert all(p.status != "pending_approval" for p in posts)
        yt = [p for p in posts if p.platform == "youtube"]
        assert yt and all(p.status == "failed" for p in yt)
        assert all("video_path" in (p.error or "") for p in yt)
    finally:
        session.close()


# ---------------------------------------------------------------- start/stop
def test_start_requires_enabled_and_rights(tmp_path):
    agent = _agent(tmp_path)
    with pytest.raises(AgentError, match="agent is off"):
        agent.start(_config(enabled=False))
    with pytest.raises(AgentError, match="confirm the source"):
        agent.start(_config(rights_confirmed=False))


def test_start_twice_is_rejected_while_running(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def _blocking_run(config):
        entered.set()
        release.wait(timeout=10)
        return agent.status()

    monkeypatch.setattr(agent, "run_once", _blocking_run)
    state = agent.start(_config())
    assert state["running"] is True
    assert entered.wait(timeout=5)
    with pytest.raises(AgentError, match="already running"):
        agent.start(_config())
    release.set()
    agent._thread.join(timeout=5)
    assert agent.stop()["state"] in {"stopped", "done", "idle", "error"}


def test_stop_on_idle_reports_stopped(tmp_path):
    agent = _agent(tmp_path)
    assert agent.stop()["state"] == "stopped"
    assert agent.status()["running"] is False


# ------------------------------------------------------- v0.8.1 toggle fix
def test_stop_persists_enabled_false(tmp_path):
    """OFF is stored server-side, not just in the UI. Before the fix
    stop() left config.enabled=True, so the toggle (and any restart)
    still showed the agent as on after it was turned off."""
    agent = _agent(tmp_path)
    agent.store.save_config(_config(enabled=True))
    assert agent.stop()["enabled"] is False
    assert agent.store.load_config().enabled is False
    # A fresh Agent on the same data dir (a restart) still sees OFF.
    assert _agent(tmp_path).store.load_config().enabled is False


def test_start_real_video_without_yt_dlp_fails_fast(tmp_path, monkeypatch):
    """A real-video run with no yt-dlp fails BEFORE any thread starts
    (plain AgentError at click time) instead of briefly reporting
    "running" and then silently landing in "error" with ON still shown."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    agent = _agent(tmp_path)
    with pytest.raises(AgentError, match="yt-dlp"):
        agent.start(_config(transcript_text=None))
    assert agent._thread is None
    state = agent.status()
    assert state["running"] is False
    assert state["state"] == "idle"


def test_stop_mid_run_ends_stopped_not_done(tmp_path, monkeypatch):
    """OFF mid-run ends the run "stopped" — the in-flight run must not
    overwrite it with "done" (or a misleading "error") afterwards, and
    the stored config must read OFF."""
    agent = _agent(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def _blocking_run(config):
        entered.set()
        release.wait(timeout=10)
        return agent.status()

    monkeypatch.setattr(agent, "run_once", _blocking_run)
    agent.start(_config())
    assert entered.wait(timeout=5)
    stopped = agent.stop()
    assert stopped["enabled"] is False
    assert stopped["state"] == "stopped"
    release.set()
    agent._thread.join(timeout=5)
    state = agent.status()
    assert state["state"] == "stopped"
    assert state["last_error"] is None
    assert state["enabled"] is False
    assert agent.store.load_config().enabled is False


def test_run_thread_maps_stop_to_stopped_state(tmp_path, monkeypatch):
    """A run that raises because it was stopped ends "stopped" with no
    last_error — not "error" with "Agent stopped while queueing posts."."""
    agent = _agent(tmp_path)

    def _stopped_run(config):
        raise AgentError("Agent stopped while queueing posts.")

    monkeypatch.setattr(agent, "run_once", _stopped_run)
    agent._stop.set()
    agent._run_thread(_config())
    state = agent.status()
    assert state["state"] == "stopped"
    assert state["last_error"] is None


def test_run_once_after_stop_reports_stopped(tmp_path):
    """A stop requested before the thread got going is never overwritten
    with a fresh "running" by the late thread."""
    agent = _agent(tmp_path)
    agent._stop.set()
    state = agent.run_once(_config())
    assert state["state"] == "stopped"
    assert state["running"] is False


def test_start_runs_to_done_in_background(tmp_path):
    agent = _agent(tmp_path)
    agent.start(_config())
    deadline = time.time() + 15
    while time.time() < deadline:
        if agent.status()["state"] == "done":
            break
        time.sleep(0.05)
    state = agent.status()
    assert state["state"] == "done"
    assert state["queued"] == 12
