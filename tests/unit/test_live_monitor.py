"""Unit tests — live stream monitoring (v0.10.0, Worker A).

No test hits a real live stream or the real YouTube API: the
detector is driven with synthetic loudness / chat series, and the
replay end-to-end test feeds a generated fixture video (quiet sine
with one loud burst) through the real ffmpeg ingest at full speed
(``pace_replay=False``) with a stub transcriber. The peak job is
created for real in the test DB, but the queue runs a fake runner
so no pipeline work happens inside the test.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from clipforge.db import Job, init_db, session_factory
from clipforge.job_queue import JobQueue, reset_job_queue
from clipforge.live_monitor import (
    LiveMonitorError,
    PeakDetector,
    detect_platform,
    platform_capabilities,
    start_monitor,
    stop_monitor,
)

FIXTURE_DIR = Path("apps/api/data/uploads")
FIXTURE_MP4 = FIXTURE_DIR / "live_monitor_replay.mp4"


def _ensure_fixture_mp4() -> Path:
    """90s fixture: quiet sine, one loud burst at 65–70s.

    The detector needs a 60s baseline before it can fire, so the
    burst sits after the warm-up. Skip plainly when FFmpeg is absent.
    """
    if FIXTURE_MP4.exists():
        return FIXTURE_MP4
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available — cannot generate the live-monitor fixture")
    FIXTURE_MP4.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=90",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=90",
            "-map",
            "0:v",
            "-map",
            "1:a",
            # Quiet sine, with an 18x (+25 dB) burst at 65–70s. The
            # burst is built with timeline `enable=` — a `volume`
            # expression on `t` does not evaluate in a lavfi input
            # graph (verified: the whole file came out flat).
            "-af",
            "volume=0.05,volume=18:enable='between(t,65,70)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(FIXTURE_MP4),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"ffmpeg could not generate the fixture: {proc.stderr}"
    assert FIXTURE_MP4.exists(), "live-monitor fixture missing after generation"
    return FIXTURE_MP4


# ------------------------------------------------------------------ platforms
def test_detect_platform():
    assert detect_platform("https://www.youtube.com/watch?v=abc12345678") == "youtube"
    assert detect_platform("https://youtu.be/abc12345678") == "youtube"
    assert detect_platform("https://www.twitch.tv/somechannel") == "twitch"
    assert detect_platform("https://kick.com/somechannel") == "kick"
    assert detect_platform("https://example.com/video.mp4") is None
    assert detect_platform("") is None


def test_platform_capabilities_are_honest():
    yt = platform_capabilities("youtube")
    assert yt["audio"] is True
    assert yt["chat"] == "oauth"  # an API key alone cannot read live chat
    tw = platform_capabilities("twitch")
    assert tw["chat"] is True  # anonymous IRC, no key
    kick = platform_capabilities("kick")
    assert kick["audio"] is True
    assert kick["chat"] is False  # no public chat API without auth
    assert "audio-only" in kick["chat_note"]


# ------------------------------------------------------------------ detector: audio
def _quiet_detector(seconds: int = 75, db: float = -30.0) -> PeakDetector:
    det = PeakDetector()
    for _ in range(seconds):
        det.add_audio_second(db)
    return det


def test_flat_audio_never_peaks():
    det = _quiet_detector(120)
    assert det.check() is None
    assert det.base["warming"] is False


def test_detector_warms_up_for_a_minute():
    det = PeakDetector()
    for _ in range(30):
        det.add_audio_second(-30.0)
    assert det.check() is None
    assert det.base["warming"] is True


def test_loud_burst_after_baseline_is_a_peak():
    det = _quiet_detector(70)
    for _ in range(5):
        det.add_audio_second(-2.0)  # ~28 dB louder than the -30 dB baseline
    peak = det.check()
    assert peak is not None
    assert "loudness" in peak.reasons[0]
    assert peak.start_s == pytest.approx(peak.trigger_s - 12.0)
    assert peak.end_s == pytest.approx(peak.trigger_s + 28.0)
    assert 50 <= peak.score <= 95


def test_small_loudness_rise_is_not_a_peak():
    det = _quiet_detector(70)
    for _ in range(5):
        det.add_audio_second(-27.0)  # only +3 dB over baseline: below the 60% jump
    assert det.check() is None


def test_peak_cooldown_suppresses_a_second_burst():
    det = _quiet_detector(70)
    for _ in range(5):
        det.add_audio_second(-2.0)
    assert det.check() is not None
    for _ in range(5):
        det.add_audio_second(-2.0)
    # 5s later, still inside the 60s cooldown: no second peak.
    assert det.check() is None


# ------------------------------------------------------------------ detector: chat
def test_chat_burst_from_many_people_is_a_peak():
    det = PeakDetector()
    for second in range(80):
        det.add_audio_second(-30.0)
        if second < 75:
            det.add_chat(second, user=f"user{second % 2}")  # ~2 people/s baseline
    for second in range(75, 80):
        for i in range(10):
            det.add_chat(second, user=f"chatter{i}")  # 10 people/s now
    peak = det.check()
    assert peak is not None
    assert any("chat" in r for r in peak.reasons)


def test_one_person_spamming_is_not_a_chat_peak():
    det = PeakDetector()
    for second in range(80):
        det.add_audio_second(-30.0)
        if second < 75:
            det.add_chat(second, user=f"user{second % 2}")
    for second in range(75, 80):
        for _ in range(10):
            det.add_chat(second, user="spammer")  # 10 msgs/s, one person
    assert det.check() is None


# ------------------------------------------------------------------ detector: keywords
def test_clip_it_in_transcript_is_a_keyword_trigger():
    det = PeakDetector()
    words = [
        {"w": w, "s": 10.0 + i * 0.4, "e": 10.0 + i * 0.4 + 0.3}
        for i, w in enumerate("that was insane clip it right now".split())
    ]
    det.add_words(words)
    pending = det.drain_keyword_triggers()
    assert len(pending) == 1
    assert "clip it" in pending[0]["why"]
    peak = det.keyword_peak(pending[0])
    assert peak.kind == "keyword"
    # trigger at 11.2s: start clamps to 0 (trigger - 12s would be negative)
    assert peak.start_s == 0.0
    assert peak.end_s == pytest.approx(pending[0]["trigger_s"] + 28.0)


def test_custom_trigger_phrase_fires():
    det = PeakDetector(trigger_words=["let's gooo"])
    words = [
        {"w": w, "s": 5.0 + i * 0.4, "e": 5.0 + i * 0.4 + 0.3}
        for i, w in enumerate("and he scores let's gooo".split())
    ]
    det.add_words(words)
    assert len(det.drain_keyword_triggers()) == 1


def test_lone_word_clip_does_not_trigger():
    det = PeakDetector()
    words = [
        {"w": w, "s": 5.0 + i * 0.4, "e": 5.0 + i * 0.4 + 0.3}
        for i, w in enumerate("here is a clip of the game".split())
    ]
    det.add_words(words)
    assert det.drain_keyword_triggers() == []


# ------------------------------------------------------------------ session validation
def test_start_monitor_rejects_unknown_platform(tmp_path):
    with pytest.raises(LiveMonitorError, match="YouTube, Twitch or Kick"):
        start_monitor(url="https://example.com/stream", data_dir=tmp_path)


def test_start_monitor_rejects_missing_replay(tmp_path):
    with pytest.raises(LiveMonitorError, match="Replay file not found"):
        start_monitor(replay_path=tmp_path / "nope.mp4", data_dir=tmp_path)


def test_start_monitor_rejects_empty_url(tmp_path):
    with pytest.raises(LiveMonitorError, match="live stream URL"):
        start_monitor(url="", data_dir=tmp_path)


# ------------------------------------------------------------------ replay end-to-end
def test_replay_detects_burst_and_creates_peak_job(tmp_path, monkeypatch):
    """A replayed fixture with one loud burst yields one peak job.

    The detector, the ffmpeg ingest, the peak cut and the real
    ``create_peak_job`` row all run; the queue's runner is a fake
    so the pipeline itself does not run inside the test. The job
    row proves the peak entered the existing queue.
    """
    fixture = _ensure_fixture_mp4()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    init_db()
    reset_job_queue()
    fake_queue = JobQueue(data_dir=tmp_path, runner=lambda *a, **k: None)
    monkeypatch.setattr("clipforge.job_queue.get_job_queue", lambda data_dir=None: fake_queue)
    try:
        session = start_monitor(
            replay_path=fixture,
            data_dir=tmp_path,
            transcriber=lambda raw, n: [],
            pace_replay=False,
        )
        try:
            deadline = time.time() + 120
            job_id = None
            while time.time() < deadline:
                status = session.status()
                done_peaks = [p for p in status["peaks"] if p.get("job_id")]
                if done_peaks:
                    job_id = done_peaks[0]["job_id"]
                    break
                if status["status"] == "failed":
                    pytest.fail(f"replay monitor failed: {status.get('error')}")
                if status["status"] == "ended" and status["peaks"] and not done_peaks:
                    time.sleep(1)
                    continue
                time.sleep(1)
            assert job_id, f"no peak job created; peaks={session.status()['peaks']}"
            db = session_factory()()
            try:
                job = db.get(Job, job_id)
                assert job is not None
                assert job.source_type == "local"
                assert job.source_path and Path(job.source_path).is_file()
                meta = job.meta_json or {}
                assert meta.get("live_peak") is True
                assert meta.get("live_peak_kind") == "spike"
                assert "loudness" in " ".join(meta.get("live_peak_reasons") or [])
                assert meta.get("live_peak_trigger_s", 0) >= 60
                # The cut window is ~40s of the 90s fixture.
                window = meta["live_peak_end_s"] - meta["live_peak_start_s"]
                assert window == pytest.approx(40.0, abs=1.0)
            finally:
                db.close()
        finally:
            stop_monitor(session.info.session_id)
            # Remove the peak job row so the shared test DB is clean
            # for the next test (the fake runner left it RUNNING).
            db = session_factory()()
            try:
                for job in db.query(Job).all():
                    if (job.meta_json or {}).get("live_peak"):
                        db.delete(job)
                db.commit()
            finally:
                db.close()
    finally:
        reset_job_queue()
