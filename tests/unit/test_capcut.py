"""Unit tests — CapCut draft export (ClipForge v0.10.0).

The draft writer is ClipForge's own minimal implementation (see
``src/clipforge/capcut.py``). These tests prove the folder it writes
is well-formed: the JSON parses, tracks reference materials that
exist, caption segments do not overlap, and every media path
resolves. Opening the draft in real CapCut Desktop still needs a
Windows/macOS check — this suite cannot do that on Linux.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.capcut import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    DRAFT_VERSION,
    CapCutError,
    build_draft_document,
    chunk_words,
    export_clip_to_capcut,
    get_capcut_drafts_dir,
    validate_draft,
    write_draft,
)

WORDS = [
    {"text": "stop", "start": 10.0, "end": 10.3},
    {"text": "making", "start": 10.3, "end": 10.6},
    {"text": "this", "start": 10.6, "end": 10.8},
    {"text": "mistake", "start": 10.9, "end": 11.3},
    {"text": "in", "start": 11.3, "end": 11.5},
    {"text": "ranked", "start": 11.5, "end": 11.9},
]


def _doc(**overrides):
    kwargs = {
        "mp4_path": "/tmp/clips/clip_01.mp4",
        "duration_s": 30.0,
        "title": "Ranked games clutch",
        "hook": "Stop making this mistake",
        "words": WORDS,
        "clip_start_s": 10.0,
    }
    kwargs.update(overrides)
    return build_draft_document(**kwargs)


def test_document_shape_matches_capcut_format():
    doc = _doc()
    assert doc["version"] == DRAFT_VERSION
    assert doc["canvas_config"] == {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT, "ratio": "9:16"}
    assert doc["fps"] == 30.0
    assert doc["duration"] == 30_000_000
    assert doc["name"] == "Ranked games clutch"
    # Every materials key CapCut expects is present.
    assert set(doc["materials"]) >= {"videos", "texts", "speeds", "audios", "effects"}
    track_names = [t["name"] for t in doc["tracks"]]
    assert track_names == ["ClipForge Video", "Hook Title", "Captions"]
    assert [t["type"] for t in doc["tracks"]] == ["video", "text", "text"]


def test_video_material_references_the_mp4():
    doc = _doc()
    videos = doc["materials"]["videos"]
    assert len(videos) == 1
    assert videos[0]["path"] == "/tmp/clips/clip_01.mp4"
    assert videos[0]["type"] == "video"
    assert videos[0]["duration"] == 30_000_000
    assert videos[0]["width"] == CANVAS_WIDTH
    video_track = doc["tracks"][0]
    assert len(video_track["segments"]) == 1
    seg = video_track["segments"][0]
    assert seg["material_id"] == videos[0]["id"]
    assert seg["target_timerange"] == {"start": 0, "duration": 30_000_000}
    assert seg["source_timerange"] == {"start": 0, "duration": 30_000_000}


def test_hook_title_covers_first_seconds_only():
    doc = _doc()
    hook_track = doc["tracks"][1]
    assert len(hook_track["segments"]) == 1
    seg = hook_track["segments"][0]
    assert seg["target_timerange"] == {"start": 0, "duration": 3_500_000}
    material = doc["materials"]["texts"][0]
    assert material["id"] == seg["material_id"]
    content = json.loads(material["content"])
    assert content["text"] == "Stop making this mistake"


def test_captions_chunk_words_and_shift_to_clip_time():
    doc = _doc()
    caption_track = doc["tracks"][2]
    assert len(caption_track["segments"]) == 2
    texts = {t["id"]: json.loads(t["content"])["text"] for t in doc["materials"]["texts"]}
    first, second = caption_track["segments"]
    assert texts[first["material_id"]] == "stop making this"
    assert texts[second["material_id"]] == "mistake in ranked"
    # Words at source 10.0-10.8s become clip 0.0-0.8s.
    assert first["target_timerange"]["start"] == 0
    assert first["target_timerange"]["duration"] == 800_000
    assert second["target_timerange"]["start"] == 900_000


def test_chunk_words_groups_three_and_stretches_short_chunks():
    chunks = chunk_words(
        [
            {"w": "a", "s": 0.0, "e": 0.1},
            {"w": "b", "s": 0.1, "e": 0.2},
            {"w": "c", "s": 0.2, "e": 0.25},
            {"w": "d", "s": 1.0, "e": 1.2},
        ]
    )
    assert chunks[0]["text"] == "a b c"
    assert chunks[0]["end_s"] - chunks[0]["start_s"] >= 0.3
    assert chunks[1]["text"] == "d"


def test_no_words_means_no_captions_track():
    doc = _doc(words=[])
    assert [t["name"] for t in doc["tracks"]] == ["ClipForge Video", "Hook Title"]


def test_words_outside_clip_are_dropped():
    doc = _doc(words=[{"text": "late", "start": 99.0, "end": 99.5}])
    assert [t["name"] for t in doc["tracks"]] == ["ClipForge Video", "Hook Title"]


def test_zero_duration_is_a_plain_error():
    with pytest.raises(CapCutError, match="no length"):
        _doc(duration_s=0)


def test_write_draft_creates_every_file(tmp_path):
    draft_dir = tmp_path / "ClipForge_test"
    files = write_draft(draft_dir, "ClipForge_test", _doc())
    assert set(files) == {
        "draft_content.json",
        "draft_content.json.bak",
        "template-2.tmp",
        "draft_info.json",
        "draft_meta_info.json",
        "draft_settings",
    }
    content = (draft_dir / "draft_content.json").read_text(encoding="utf-8")
    # The mirrors carry the same project.
    assert (draft_dir / "draft_content.json.bak").read_text(encoding="utf-8") == content
    assert (draft_dir / "draft_info.json").read_text(encoding="utf-8") == content
    meta = json.loads((draft_dir / "draft_meta_info.json").read_text(encoding="utf-8"))
    assert meta["draft_name"] == "ClipForge_test"
    assert meta["tm_duration"] == 30_000_000
    assert meta["draft_fold_path"] == str(draft_dir)


def test_export_writes_valid_draft_and_validates(tmp_path, monkeypatch):
    mp4 = tmp_path / "clip_01.mp4"
    mp4.write_bytes(b"fake mp4 - ffprobe will fail, duration falls back")
    result = export_clip_to_capcut(
        mp4_path=mp4,
        duration_s=12.5,
        title="Test clip",
        hook="Watch this",
        words=[{"text": "hello", "start": 0.0, "end": 0.5}],
        data_dir=tmp_path,
    )
    assert result["ok"] is True
    draft_dir = Path(result["draft_path"])
    assert draft_dir.is_dir()
    assert "CapCut draft ready" in result["message"]
    assert str(draft_dir) in result["message"]
    assert result["tracks"] == ["ClipForge Video", "Hook Title", "Captions"]
    validation = validate_draft(draft_dir)
    assert validation["ok"] is True, validation["errors"]
    assert validation["duration_s"] == pytest.approx(12.5)
    assert validation["tracks"] == ["ClipForge Video", "Hook Title", "Captions"]
    # The video material points at the real mp4 by absolute path.
    doc = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert doc["materials"]["videos"][0]["path"] == str(mp4.resolve())


def test_export_missing_mp4_is_a_plain_error(tmp_path):
    with pytest.raises(CapCutError, match="missing"):
        export_clip_to_capcut(
            mp4_path=tmp_path / "nope.mp4",
            duration_s=10.0,
            data_dir=tmp_path,
        )


def test_validate_draft_catches_missing_media(tmp_path):
    mp4 = tmp_path / "clip_01.mp4"
    mp4.write_bytes(b"fake")
    result = export_clip_to_capcut(mp4_path=mp4, duration_s=5.0, data_dir=tmp_path)
    mp4.unlink()
    validation = validate_draft(Path(result["draft_path"]))
    assert validation["ok"] is False
    assert validation["media_missing"]
    assert any("missing" in e for e in validation["errors"])


def test_validate_draft_rejects_unparseable_content(tmp_path):
    draft_dir = tmp_path / "broken"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text("{not json", encoding="utf-8")
    validation = validate_draft(draft_dir)
    assert validation["ok"] is False
    assert "does not parse" in validation["errors"][0]


def test_drafts_dir_falls_back_without_capcut(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    root, in_capcut = get_capcut_drafts_dir(tmp_path)
    assert root == tmp_path / "capcut_drafts"
    assert in_capcut is False


def test_drafts_dir_uses_capcut_folder_when_installed(tmp_path, monkeypatch):
    capcut_root = tmp_path / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    capcut_root.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root, in_capcut = get_capcut_drafts_dir(tmp_path)
    assert root == capcut_root
    assert in_capcut is True


def test_re_export_replaces_previous_draft(tmp_path):
    mp4 = tmp_path / "clip_01.mp4"
    mp4.write_bytes(b"fake")
    first = export_clip_to_capcut(mp4_path=mp4, duration_s=5.0, title="Same", data_dir=tmp_path)
    marker = Path(first["draft_path"]) / "stale.txt"
    marker.write_text("old", encoding="utf-8")
    second = export_clip_to_capcut(mp4_path=mp4, duration_s=5.0, title="Same", data_dir=tmp_path)
    assert first["draft_path"] == second["draft_path"]
    assert not marker.exists()
