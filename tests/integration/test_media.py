"""Media endpoint tests — GET /media/clips/{job_id}/{filename} and
GET /media/uploads/{filename} (added for the web player's <video> tag).

The media root is read per request from CLIPFORGE_DATA_DIR, so tests point
it at a temp dir; nothing under the repo's real ./data is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
from fastapi.testclient import TestClient  # noqa: E402

try:
    from clipforge_api.app import _media_file, app
except ImportError:
    pytest.skip(
        "clipforge_api.app not implemented yet — see docs/api.md",
        allow_module_level=True,
    )

client = TestClient(app)

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture()
def media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "clips" / "job-1").mkdir(parents=True)
    (root / "uploads").mkdir(parents=True)
    (root / "clips" / "job-1" / "clip_01.mp4").write_bytes(FAKE_MP4)
    (root / "uploads" / "sample.mp4").write_bytes(FAKE_MP4)
    (root / "clips" / "job-1" / "clip_01.txt").write_text("not a video")
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(root))
    return root


def test_media_clip_serves_rendered_clip(media_root: Path):
    res = client.get("/media/clips/job-1/clip_01.mp4")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert res.content == FAKE_MP4


def test_media_upload_serves_source(media_root: Path):
    res = client.get("/media/uploads/sample.mp4")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert res.content == FAKE_MP4


def test_media_missing_file_is_404(media_root: Path):
    assert client.get("/media/clips/job-1/nope.mp4").status_code == 404
    assert client.get("/media/clips/job-2/clip_01.mp4").status_code == 404
    assert client.get("/media/uploads/nope.mp4").status_code == 404


def test_media_non_video_extension_is_404(media_root: Path):
    assert client.get("/media/clips/job-1/clip_01.txt").status_code == 404


def test_media_file_rejects_traversal(media_root: Path):
    # Direct helper test: traversal must 404, never resolve outside the root.
    with pytest.raises(Exception) as exc:
        _media_file("clips", "..", "..", "secret.mp4")
    assert exc.value.status_code == 404
    with pytest.raises(Exception) as exc:
        _media_file("uploads", "..", "clips", "job-1", "clip_01.mp4")
    assert exc.value.status_code == 404
