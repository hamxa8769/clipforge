"""Unit tests — live video clipping (v0.7.0, Worker C).

No test hits the real YouTube API or runs a real yt-dlp recording:
YouTube HTTP is a fake httpx client, and capture is driven with a
fake Popen / fake clock / stub validator. The worker tests drive a
real ``run_job`` on a recorded-video fixture with the capture step
mocked (yt-dlp itself is absent in this environment, so a stub
``yt-dlp`` on PATH satisfies the tool check) — the transcribe step
uses the deterministic ``transcript_text`` fallback and rendering
uses the real FFmpeg, so the captured segment really flows through
score -> clip -> SEO -> review.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import threading
from pathlib import Path

import httpx
import pytest

from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenRecord, TokenStore
from clipforge.db import Job, JobStatus, init_db, session_factory
from clipforge.ingest import SourceInfo
from clipforge.live import (
    LIVE_DEFAULT_DURATION_S,
    LiveCaptureCancelled,
    LiveCaptureError,
    LiveError,
    LiveVideo,
    canonical_source_license,
    check_can_clip,
    clip_decision,
    list_own_live,
    resolve_own_channel_id,
    search_live,
    validate_live_duration,
    verify_live_video,
)
from clipforge.trending import TrendingCredentials
from tests.unit.test_agent_niches import GAMING_TEXT

OWN_CHANNEL = "UCAAAAAAAAA"
OTHER_CHANNEL = "UCBBBBBBBBB"
LIVE_ID_CC = "L1v3Str34mA"
LIVE_ID_STD = "L1v3Str34mB"
LIVE_URL_CC = f"https://www.youtube.com/watch?v={LIVE_ID_CC}"
FIXTURE_MP4 = Path("apps/api/data/uploads/7594885dc7dc_up.mp4")


# ------------------------------------------------------------------ clip decision
def test_clip_decision_own_live_always_clippable():
    can_clip, reason = clip_decision("youtube", is_own=True)
    assert can_clip is True
    assert "own" in reason.lower()


def test_clip_decision_cc_live_clippable():
    can_clip, reason = clip_decision("creativeCommon", is_own=False)
    assert can_clip is True
    assert "Creative Commons" in reason


def test_clip_decision_standard_third_party_refused_plainly():
    can_clip, reason = clip_decision("youtube", is_own=False)
    assert can_clip is False
    assert "standard" in reason.lower()
    assert "cannot clip" in reason.lower()


def test_clip_decision_unknown_licence_refused():
    can_clip, reason = clip_decision("unknown", is_own=False)
    assert can_clip is False
    assert "could not be confirmed" in reason


def test_check_can_clip_raises_plain_message():
    with pytest.raises(LiveCaptureError, match="cannot clip"):
        check_can_clip("youtube", False)
    check_can_clip("creativeCommon", False)  # no raise
    check_can_clip("youtube", True)  # own: no raise


def test_canonical_source_license_maps_youtube_cc_to_cc_by():
    assert canonical_source_license("creativeCommon", False) == "cc-by-3.0"
    assert canonical_source_license("youtube", True) == "own"
    assert canonical_source_license("youtube", False) is None
    assert canonical_source_license("unknown", False) is None


def test_validate_live_duration_bounds():
    assert validate_live_duration(120) == 120
    assert validate_live_duration(60) == 60
    assert validate_live_duration(300) == 300
    assert validate_live_duration(LIVE_DEFAULT_DURATION_S) == 120
    with pytest.raises(LiveCaptureError, match="between 60 and 300"):
        validate_live_duration(30)
    with pytest.raises(LiveCaptureError, match="between 60 and 300"):
        validate_live_duration(301)


# ------------------------------------------------------------------ fake YouTube
def _live_search_items():
    return [
        {"id": {"videoId": LIVE_ID_CC}, "snippet": {"title": "CC live", "channelTitle": "CC Chan"}},
        {
            "id": {"videoId": LIVE_ID_STD},
            "snippet": {"title": "Std live", "channelTitle": "Std Chan"},
        },
    ]


def _live_video_items():
    return [
        {
            "id": LIVE_ID_CC,
            "snippet": {
                "title": "CC live",
                "channelTitle": "CC Chan",
                "channelId": OTHER_CHANNEL,
                "publishedAt": "2026-11-21T10:00:00Z",
            },
            "status": {"license": "creativeCommon"},
            "statistics": {"viewCount": "5000"},
            "liveStreamingDetails": {
                "concurrentViewers": "1234",
                "actualStartTime": "2026-11-21T09:30:00Z",
            },
        },
        {
            "id": LIVE_ID_STD,
            "snippet": {
                "title": "Std live",
                "channelTitle": "Std Chan",
                "channelId": OTHER_CHANNEL,
                "publishedAt": "2026-11-21T10:00:00Z",
            },
            "status": {"license": "youtube"},
            "statistics": {"viewCount": "9000"},
            "liveStreamingDetails": {
                "concurrentViewers": "5678",
                "actualStartTime": "2026-11-21T09:00:00Z",
            },
        },
    ]


def _fake_live_client(
    seen: dict | None = None,
    *,
    broadcasts_status: int = 200,
    std_channel: str = OTHER_CHANNEL,
):
    """Fake YouTube API for live tests.

    ``/videos`` honours the requested ``id`` list, and ``std_channel``
    sets the channel of the standard-licence stream — the broadcasts
    test passes the own channel there (a ``liveBroadcasts`` result is
    the user's own stream by construction).
    """
    seen = seen if seen is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            seen["search_params"] = dict(request.url.params)
            return httpx.Response(200, json={"items": _live_search_items()})
        if path.endswith("/videos"):
            seen["videos_part"] = request.url.params.get("part")
            wanted = set((request.url.params.get("id") or "").split(","))
            items = []
            for item in _live_video_items():
                if item["id"] not in wanted:
                    continue
                if item["id"] == LIVE_ID_STD:
                    item = dict(item)
                    item["snippet"] = {**item["snippet"], "channelId": std_channel}
                items.append(item)
            return httpx.Response(200, json={"items": items})
        if path.endswith("/liveBroadcasts"):
            if broadcasts_status != 200:
                return httpx.Response(broadcasts_status, json={"error": {"message": "forbidden"}})
            return httpx.Response(200, json={"items": [{"id": LIVE_ID_STD}]})
        if path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": OWN_CHANNEL}]})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _api_key_creds():
    return TrendingCredentials(kind="api_key", token="<redacted>")


def _oauth_creds():
    return TrendingCredentials(kind="oauth", token="oauth-token", account_name="My Channel")


# ------------------------------------------------------------------ live discovery
def test_search_live_marks_cc_clippable_and_standard_not():
    client = _fake_live_client()
    results = search_live("gaming", credentials=_api_key_creds(), client=client)
    assert len(results) == 2
    cc, std = results
    assert cc.video_id == LIVE_ID_CC
    assert cc.can_clip is True
    assert cc.licence == "creativeCommon"
    assert cc.viewers == 1234
    assert cc.started_at == "2026-11-21T09:30:00Z"
    assert cc.url == LIVE_URL_CC
    assert std.can_clip is False
    assert std.licence == "youtube"
    assert "cannot clip" in std.clip_reason.lower()


def test_search_live_uses_event_type_live():
    seen: dict = {}
    client = _fake_live_client(seen)
    search_live("gaming", credentials=_api_key_creds(), client=client)
    assert seen["search_params"]["eventType"] == "live"
    assert seen["search_params"]["type"] == "video"
    assert "videoLicense" not in seen["search_params"]


def test_search_live_licensed_only_passes_video_license():
    seen: dict = {}
    client = _fake_live_client(seen)
    search_live("gaming", credentials=_api_key_creds(), licensed_only=True, client=client)
    assert seen["search_params"]["videoLicense"] == "creativeCommon"


def test_search_live_own_channel_marked_own_and_clippable():
    client = _fake_live_client()
    results = search_live(
        "gaming",
        credentials=_api_key_creds(),
        own_channel_id=OTHER_CHANNEL,
        client=client,
    )
    # Both streams are on OTHER_CHANNEL here: both are "own", even the
    # standard-licence one — own content is always clippable.
    assert all(v.is_own for v in results)
    assert all(v.can_clip for v in results)


def test_search_live_empty_is_empty_not_invented():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert search_live("gaming", credentials=_api_key_creds(), client=client) == []


def test_search_live_youtube_error_is_plain_language():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "quota exceeded"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LiveError, match="quota exceeded"):
        search_live("gaming", credentials=_api_key_creds(), client=client)


def test_live_video_to_dict_includes_clip_fields():
    video = LiveVideo(
        video_id=LIVE_ID_CC,
        title="T",
        channel_title="C",
        viewers=10,
        licence="creativeCommon",
        can_clip=True,
        clip_reason="ok",
    )
    data = video.to_dict()
    assert data["url"] == LIVE_URL_CC
    assert data["can_clip"] is True
    assert data["viewers"] == 10


# ------------------------------------------------------------------ own live
def test_list_own_live_via_broadcasts():
    client = _fake_live_client(std_channel=OWN_CHANNEL)
    results = list_own_live(credentials=_oauth_creds(), own_channel_id=OWN_CHANNEL, client=client)
    assert len(results) == 1
    assert results[0].video_id == LIVE_ID_STD
    assert results[0].is_own is True
    assert results[0].can_clip is True  # own live stream, even standard licence


def test_list_own_live_broadcasts_403_falls_back_to_channel_search():
    seen: dict = {}
    client = _fake_live_client(seen, broadcasts_status=403)
    results = list_own_live(credentials=_oauth_creds(), own_channel_id=OTHER_CHANNEL, client=client)
    assert seen["search_params"]["channelId"] == OTHER_CHANNEL
    assert seen["search_params"]["eventType"] == "live"
    assert all(v.is_own for v in results)
    assert all(v.can_clip for v in results)


def test_list_own_live_api_key_without_channel_is_plain_error():
    client = _fake_live_client()
    with pytest.raises(LiveError, match="connect your YouTube channel"):
        list_own_live(credentials=_api_key_creds(), own_channel_id="", client=client)


def test_resolve_own_channel_id_from_stored_token(tmp_path):
    secrets = SecretStore(tmp_path)
    TokenStore(secrets).save(
        TokenRecord(
            platform="youtube",
            access_token="tok",
            account_name="My Channel",
            account_ref=OWN_CHANNEL,
        )
    )
    assert resolve_own_channel_id(secrets) == OWN_CHANNEL


def test_resolve_own_channel_id_falls_back_to_channels_list():
    client = _fake_live_client()
    secrets = SecretStore("/nonexistent-dir")
    assert resolve_own_channel_id(secrets, credentials=_oauth_creds(), client=client) == OWN_CHANNEL


def test_resolve_own_channel_id_api_key_only_is_empty():
    secrets = SecretStore("/nonexistent-dir")
    assert resolve_own_channel_id(secrets, credentials=_api_key_creds()) == ""


# ------------------------------------------------------------------ verify before capture
def test_verify_live_video_cc_is_clippable():
    client = _fake_live_client()
    video = verify_live_video(LIVE_ID_CC, credentials=_api_key_creds(), client=client)
    assert video.video_id == LIVE_ID_CC
    assert video.licence == "creativeCommon"
    assert video.can_clip is True


def test_verify_live_video_standard_third_party_is_not():
    client = _fake_live_client()
    video = verify_live_video(LIVE_ID_STD, credentials=_api_key_creds(), client=client)
    assert video.can_clip is False
    assert "standard" in video.clip_reason.lower()
    assert "cannot be re-uploaded" in video.clip_reason.lower()


def test_verify_live_video_missing_video_is_plain_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(LiveError, match="could not find that video"):
        verify_live_video(LIVE_ID_CC, credentials=_api_key_creds(), client=client)


# ------------------------------------------------------------------ capture
class _FakeProc:
    """Stand-in for subprocess.Popen: exit code + stderr, optional hang."""

    def __init__(self, *, returncode: int = 0, stderr: str = "", never_exits: bool = False):
        self._returncode = returncode
        self._stderr = stderr
        self._never_exits = never_exits
        self.returncode: int | None = None if never_exits else returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._never_exits else self._returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self._never_exits and not self.terminated and not self.killed:
            raise subprocess.TimeoutExpired(cmd="yt-dlp", timeout=timeout or 0)
        return self.returncode

    def communicate(self, timeout=None):
        return "", self._stderr


def _fake_popen(tmp_path: Path, proc: _FakeProc, *, write_file: bool = True):
    def factory(cmd, **kwargs):
        assert cmd[0].endswith("yt-dlp")
        assert "--no-part" in cmd  # partial file must be directly usable
        assert cmd[-1].startswith("http")
        if write_file:
            (tmp_path / "live_capture.mp4").write_bytes(b"fake-video-bytes")
        return proc

    return factory


def _stub_source(path: Path) -> SourceInfo:
    return SourceInfo(
        path=Path(path),
        sha256="a" * 64,
        duration_s=120.0,
        width=1280,
        height=720,
        video_codec="h264",
        audio_codec="aac",
        size_bytes=123,
        format_name="mp4",
    )


def _capture_kwargs(tmp_path: Path, proc: _FakeProc, **overrides):
    from clipforge.live import capture_live_stream  # noqa: PLC0415 (test helper)

    write_file = overrides.pop("write_file", True)
    kwargs = {
        "out_dir": tmp_path,
        "duration_s": 120,
        "popen": _fake_popen(tmp_path, proc, write_file=write_file),
        "which": lambda name: "/usr/bin/yt-dlp",
        "sleep": lambda seconds: None,
        "validate": _stub_source,
    }
    kwargs.update(overrides)
    return capture_live_stream, kwargs


def test_capture_success_returns_validated_source(tmp_path):
    proc = _FakeProc(returncode=0)
    fn, kwargs = _capture_kwargs(tmp_path, proc)
    progress: list[float] = []
    info = fn(LIVE_URL_CC, on_progress=progress.append, **kwargs)
    assert info.duration_s == 120.0
    assert info.source_kind == "youtube"
    assert info.source_url == LIVE_URL_CC
    assert info.own_content is True
    assert proc.terminated is False  # exited on its own before the deadline


def test_capture_stops_after_bounded_duration(tmp_path):
    proc = _FakeProc(never_exits=True)
    clock_values = itertools.count(start=1000, step=30)
    fn, kwargs = _capture_kwargs(
        tmp_path,
        proc,
        monotonic=lambda: next(clock_values),
        duration_s=120,
    )
    info = fn(LIVE_URL_CC, **kwargs)
    assert proc.terminated is True  # stopped at the deadline, not left running
    assert info.source_kind == "youtube"
    assert (tmp_path / "live_capture.mp4").exists()


def test_capture_cancel_deletes_partial_and_raises(tmp_path):
    proc = _FakeProc(never_exits=True)
    event = threading.Event()
    event.set()  # cancelled before the capture even starts
    fn, kwargs = _capture_kwargs(tmp_path, proc, cancel_event=event)
    with pytest.raises(LiveCaptureCancelled, match="cancelled"):
        fn(LIVE_URL_CC, **kwargs)
    assert not (tmp_path / "live_capture.mp4").exists()


def test_capture_cancel_mid_recording(tmp_path):
    proc = _FakeProc(never_exits=True)
    event = threading.Event()
    calls = {"n": 0}

    def _sleep(seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            event.set()

    fn, kwargs = _capture_kwargs(tmp_path, proc, cancel_event=event, sleep=_sleep)
    with pytest.raises(LiveCaptureCancelled, match="cancelled"):
        fn(LIVE_URL_CC, **kwargs)
    assert proc.terminated is True
    assert not (tmp_path / "live_capture.mp4").exists()


@pytest.mark.parametrize(
    "stderr,match",
    [
        ("ERROR: This live stream has ended", "has ended"),
        ("ERROR: Sign in to confirm your age", "age-restricted"),
        ("ERROR: This video is not available in your country", "not available in your country"),
        ("ERROR: This video is DRM-protected", "DRM-protected"),
        ("ERROR: Private video", "private"),
        ("ERROR: Unsupported URL", "cannot be recorded"),
    ],
)
def test_capture_failure_is_plain_language(tmp_path, stderr, match):
    proc = _FakeProc(returncode=1, stderr=stderr, never_exits=False)
    fn, kwargs = _capture_kwargs(tmp_path, proc, write_file=False)
    with pytest.raises(LiveCaptureError, match=match):
        fn(LIVE_URL_CC, **kwargs)


def test_capture_stream_ended_before_anything_recorded(tmp_path):
    proc = _FakeProc(returncode=0)  # yt-dlp "succeeded" but wrote nothing
    fn, kwargs = _capture_kwargs(tmp_path, proc, write_file=False)
    with pytest.raises(LiveCaptureError, match="nothing could be recorded|Could not record"):
        fn(LIVE_URL_CC, **kwargs)


def test_capture_without_yt_dlp_is_plain_error(tmp_path):
    fn, kwargs = _capture_kwargs(tmp_path, _FakeProc(), which=lambda name: None)
    with pytest.raises(LiveCaptureError, match="yt-dlp is not installed"):
        fn(LIVE_URL_CC, **kwargs)


def test_capture_unreadable_file_is_plain_error(tmp_path):
    def _bad_validate(path):
        raise ValueError("not a video file")

    fn, kwargs = _capture_kwargs(tmp_path, _FakeProc(), validate=_bad_validate)
    with pytest.raises(LiveCaptureError, match="could not be read as a video"):
        fn(LIVE_URL_CC, **kwargs)
    assert not (tmp_path / "live_capture.mp4").exists()


# ------------------------------------------------------------------ worker wiring
@pytest.fixture()
def _worker_env(tmp_path, monkeypatch):
    """Temp DB + temp data dir; a stub yt-dlp on PATH for the tool check."""
    init_db()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "yt-dlp"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _live_job(session, tmp_path, **meta):
    job = Job(
        source_type="live",
        source_url=LIVE_URL_CC,
        title="Live capture — gaming",
        own_content=True,
        status=JobStatus.PENDING.value,
        meta_json={
            "live": True,
            "live_video_id": LIVE_ID_CC,
            "live_licence": "creativeCommon",
            "live_is_own": False,
            "live_duration_s": 60,
            "top_n": 1,
            "transcript_text": (GAMING_TEXT + " ") * 3,
            **meta,
        },
    )
    session.add(job)
    session.commit()
    return job


def test_worker_live_capture_success_flows_into_pipeline(_worker_env, monkeypatch):
    """Capture -> transcribe -> score -> clip -> SEO -> review, no new pipeline."""
    from clipforge import worker as worker_mod  # noqa: PLC0415
    from clipforge.ingest import validate_local_file  # noqa: PLC0415

    tmp_path = _worker_env
    assert FIXTURE_MP4.exists(), "recorded-video fixture missing"
    session = session_factory()()
    job = _live_job(session, tmp_path)
    monkeypatch.setattr(
        worker_mod,
        "capture_live_stream",
        lambda *args, **kwargs: validate_local_file(FIXTURE_MP4),
    )
    try:
        summary = worker_mod.run_job(session, job.id, data_dir=tmp_path / "data")
    finally:
        session.close()
    assert summary["status"] in {JobStatus.REVIEW.value, JobStatus.READY.value}, summary
    assert summary.get("candidate_count", 0) >= 1
    assert summary.get("rendered_count", 0) >= 1
    session = session_factory()()
    try:
        refreshed = session.get(Job, job.id)
        assert refreshed.status in {JobStatus.REVIEW.value, JobStatus.READY.value}
        assert refreshed.error is None
        assert (tmp_path / "data" / "clips" / job.id).exists()
    finally:
        session.close()


def test_worker_live_capture_failure_is_plain_and_failed(_worker_env, monkeypatch):
    from clipforge import worker as worker_mod  # noqa: PLC0415

    tmp_path = _worker_env
    session = session_factory()()
    job = _live_job(session, tmp_path)
    monkeypatch.setattr(
        worker_mod,
        "capture_live_stream",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LiveCaptureError("This live stream has ended, so there is nothing left to record.")
        ),
    )
    try:
        summary = worker_mod.run_job(session, job.id, data_dir=tmp_path / "data")
        refreshed = session.get(Job, job.id)
        assert summary["status"] == JobStatus.FAILED.value
        assert refreshed.status == JobStatus.FAILED.value
        assert "has ended" in (refreshed.error or "")
        assert "LiveCaptureError" not in (refreshed.error or "")
    finally:
        session.close()


def test_worker_live_capture_cancel_is_cancelled_not_failed(_worker_env, monkeypatch):
    from clipforge import worker as worker_mod  # noqa: PLC0415

    tmp_path = _worker_env
    session = session_factory()()
    job = _live_job(session, tmp_path)
    monkeypatch.setattr(
        worker_mod,
        "capture_live_stream",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LiveCaptureCancelled("Live capture cancelled — the recording was stopped.")
        ),
    )
    try:
        summary = worker_mod.run_job(session, job.id, data_dir=tmp_path / "data")
        refreshed = session.get(Job, job.id)
        assert summary["status"] == JobStatus.CANCELLED.value
        assert refreshed.status == JobStatus.CANCELLED.value
        assert "cancelled" in (refreshed.error or "").lower()
    finally:
        session.close()


def test_worker_live_standard_licence_never_captured(_worker_env, monkeypatch):
    """Defence in depth: a standard-licence third-party job fails
    before capture is even attempted, whatever the job row claims."""
    from clipforge import worker as worker_mod  # noqa: PLC0415

    tmp_path = _worker_env
    session = session_factory()()
    job = _live_job(
        session,
        tmp_path,
        live_licence="youtube",
        live_is_own=False,
    )
    called = {"n": 0}

    def _capture(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("capture must not run for a standard-licence stream")

    monkeypatch.setattr(worker_mod, "capture_live_stream", _capture)
    try:
        summary = worker_mod.run_job(session, job.id, data_dir=tmp_path / "data")
        assert summary["status"] == JobStatus.FAILED.value
        assert called["n"] == 0
        refreshed = session.get(Job, job.id)
        assert "cannot clip" in (refreshed.error or "").lower()
    finally:
        session.close()


def test_worker_live_own_stream_is_captured(_worker_env, monkeypatch):
    """An own standard-licence live stream passes the worker's licence
    re-check (ownership wins over the standard licence)."""
    from clipforge import worker as worker_mod  # noqa: PLC0415
    from clipforge.ingest import validate_local_file  # noqa: PLC0415

    tmp_path = _worker_env
    session = session_factory()()
    job = _live_job(session, tmp_path, live_licence="youtube", live_is_own=True)
    monkeypatch.setattr(
        worker_mod,
        "capture_live_stream",
        lambda *args, **kwargs: validate_local_file(FIXTURE_MP4),
    )
    try:
        summary = worker_mod.run_job(session, job.id, data_dir=tmp_path / "data")
    finally:
        session.close()
    # Own standard-licence live stream: passes the worker's licence
    # re-check and runs the pipeline (READY when the high-score rule
    # auto-approves, REVIEW otherwise).
    assert summary["status"] in {JobStatus.REVIEW.value, JobStatus.READY.value}, summary
