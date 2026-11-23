"""Facecam Studio API integration tests (v0.10.0).

Covers ``GET /facecam/styles``, the preset CRUD, ``POST /jobs`` with
a facecam layout, and ``POST /facecam/render`` end to end (a real
ffmpeg render of a locally generated source, verified with ffprobe
by render_clip itself). The API app is imported as
``clipforge_api.app`` (see ``tests/conftest.py``); each test gets an
isolated data dir via CLIPFORGE_DATA_DIR.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
from clipforge_api.app import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    return tmp_path


def _make_source(path: Path, duration: float = 6.0) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1280x720:rate=30:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)


def test_styles_lists_animated_captions_and_modes():
    resp = client.get("/facecam/styles")
    assert resp.status_code == 200
    body = resp.json()
    ids = {s["id"] for s in body["caption_styles"]}
    assert {"bounce", "hormozi", "mrbeast", "karaoke", "hype", "clean"} <= ids
    animated = {s["id"] for s in body["caption_styles"] if s["animated"]}
    assert animated == {"bounce", "hormozi", "mrbeast", "karaoke", "hype", "clean"}
    assert [m["id"] for m in body["layout_modes"]] == [
        "split",
        "pip",
        "zoom",
        "blur_fill",
        "fullscreen",
    ]


def test_preset_crud_round_trip():
    layout = {"mode": "pip", "pip_pos": "bottom-left", "pip_size_frac": 0.4}
    resp = client.post(
        "/facecam/presets",
        json={"creator": "speed", "name": "gta-run", "layout": layout},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["layout"]["mode"] == "pip"
    assert resp.json()["layout"]["pip_pos"] == "bottom-left"

    resp = client.get("/facecam/presets", params={"creator": "speed"})
    assert resp.status_code == 200
    assert resp.json()["presets"]["gta-run"]["pip_size_frac"] == 0.4

    resp = client.delete("/facecam/presets/speed/gta-run")
    assert resp.status_code == 200
    assert client.get("/facecam/presets", params={"creator": "speed"}).json()[
        "presets"
    ] == {}
    assert client.delete("/facecam/presets/speed/gta-run").status_code == 404


def test_preset_rejects_bad_layout_and_bad_name():
    resp = client.post(
        "/facecam/presets",
        json={"creator": "speed", "name": "x", "layout": {"mode": "diagonal"}},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/facecam/presets",
        json={"creator": "speed", "name": "a/b", "layout": {"mode": "split"}},
    )
    assert resp.status_code == 422


def test_jobs_accept_facecam_layout(tmp_path):
    # The job is enqueued (auto-run) but ffmpeg/yt-dlp are not needed
    # to check the layout landed in meta_json: the run fails later on
    # the missing source, which is fine — creation itself is the test.
    resp = client.post(
        "/jobs",
        json={
            "source_type": "local",
            "source_path": "/nonexistent/source.mp4",
            "caption_style": "hype",
            "facecam_layout": {"mode": "split", "split_ratio": 0.5},
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]
    from clipforge.db import get_session

    session = get_session()
    try:
        from clipforge.db import Job

        job = session.query(Job).filter(Job.id == job_id).one()
        assert job.meta_json["facecam_layout"]["mode"] == "split"
        assert job.meta_json["facecam_layout"]["split_ratio"] == 0.5
        assert job.meta_json["caption_style"] == "hype"
    finally:
        session.close()


def test_jobs_reject_bad_facecam_layout():
    resp = client.post(
        "/jobs",
        json={
            "source_type": "local",
            "source_path": "/nonexistent/source.mp4",
            "facecam_layout": {"mode": "diagonal"},
        },
    )
    assert resp.status_code == 422


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_facecam_render_end_to_end(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    src = uploads / "source.mp4"
    _make_source(src)
    resp = client.post(
        "/facecam/render",
        json={
            "source_path": "uploads/source.mp4",
            "start_s": 0.0,
            "end_s": 4.0,
            "layout": {
                "mode": "split",
                "facecam": {"x": 0, "y": 0, "w": 1, "h": 0.5},
                "gameplay": {"x": 0, "y": 0.5, "w": 1, "h": 0.5},
                "split_ratio": 0.5,
            },
            "caption_style": "mrbeast",
            "words": [
                {"text": "this", "start": 0.5, "end": 0.9},
                {"text": "is", "start": 0.9, "end": 1.2},
                {"text": "insane", "start": 1.2, "end": 1.8},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["render"]["width"] == 1080
    assert body["render"]["height"] == 1920
    assert body["render"]["video_codec"] == "h264"
    assert abs(body["render"]["duration_s"] - 4.0) < 1.5
    assert body["layout"]["mode"] == "split"
    assert body["caption_style"] == "mrbeast"
    assert body["ass_file"] is not None
    ass_text = Path(body["ass_file"]).read_text(encoding="utf-8")
    assert "MrBeast" in ass_text
    assert "\\t(0,60,\\fscx" in ass_text  # bounce pop animation
    # The rendered file is served back by the media route.
    media = client.get(body["media_url"])
    assert media.status_code == 200
    assert media.headers["content-type"] == "video/mp4"
    assert len(media.content) > 1000


def test_facecam_render_rejects_bad_input(tmp_path):
    resp = client.post(
        "/facecam/render",
        json={"start_s": 0, "end_s": 4, "layout": {"mode": "split"}},
    )
    assert resp.status_code == 422  # neither source_path nor source_url
    resp = client.post(
        "/facecam/render",
        json={
            "source_path": "uploads/source.mp4",
            "start_s": 0,
            "end_s": 400,
            "layout": {"mode": "split"},
        },
    )
    assert resp.status_code == 422  # over the 300 s cap
    resp = client.post(
        "/facecam/render",
        json={
            "source_path": "../outside.mp4",
            "start_s": 0,
            "end_s": 4,
            "layout": {"mode": "split"},
        },
    )
    assert resp.status_code == 422  # outside the data dir
