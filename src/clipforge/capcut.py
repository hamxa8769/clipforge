"""CapCut draft export for ClipForge (v0.10.0).

Exports a finished clip as a native CapCut Desktop project folder that
CapCut opens from its drafts list: one video track with the rendered
9:16 mp4, a hook-title text track, and a captions text track built from
the transcript words.

The draft format here is ClipForge's own minimal writer. It was built
by studying how CapCut drafts are structured — the public
`pyJianYingDraft` library (MIT, used by the ClipNova-AI reference
project) and a real CapCut draft folder — and re-implemented from
scratch for ClipForge's data model. No code or files were copied from
either source; the structure below (``draft_content.json`` keys,
material/segment/track shapes, the ``360000`` format version) is what
CapCut itself reads.

What a draft folder contains (modern CapCut Desktop, 6.x):

* ``draft_content.json`` — the project: canvas, tracks, materials.
  Mirrored to ``draft_content.json.bak`` and ``template-2.tmp`` the way
  CapCut keeps its own backups, plus a legacy ``draft_info.json`` copy
  for older CapCut builds that still read that name.
* ``draft_meta_info.json`` — name, duration, create/modify times, and
  the folder paths CapCut shows in its project list.
* ``draft_settings`` — a small INI file with the draft timings.

The video is referenced by absolute path — the mp4 is NOT copied into
the draft, so the draft stays small and the clip file must stay where
it is. Times are microseconds, like CapCut's own files.

Honest limit: this writer is validated structurally on Linux (see
:func:`validate_draft` and ``tests/unit/test_capcut.py``) — CapCut
Desktop itself only runs on Windows/macOS, so opening the draft in
the real app still needs a check on the user's PC. The endpoint
reports the folder path plainly so that check is one click away.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

# CapCut draft format version observed in real draft folders.
DRAFT_VERSION = 360000
DRAFT_NEW_VERSION = "110.0.0"

# Canvas for every ClipForge clip: 1080x1920 vertical, 30 fps.
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
CANVAS_FPS = 30.0

# Platform block CapCut writes into a draft it last saved. CapCut
# rewrites this on open; it only identifies which app made the file.
CAPCUT_PLATFORM = {
    "app_id": 359289,
    "app_source": "cc",
    "app_version": "6.5.0",
    "os": "windows",
}

# Render order inside CapCut: the video track is the base layer,
# text tracks sit above it.
_RENDER_INDEX_VIDEO = 0
_RENDER_INDEX_TEXT = 15000

# The hook title shows for the first few seconds of the clip.
HOOK_DURATION_S = 3.5
# Caption chunks: this many transcript words per subtitle segment.
CAPTION_WORDS_PER_CHUNK = 3

# Every key CapCut expects under "materials", in the order real
# drafts list them. Only "videos", "texts" and "speeds" ever carry
# entries from this writer; the rest stay empty.
_MATERIAL_KEYS = (
    "ai_translates", "audio_balances", "audio_effects", "audio_fades",
    "audio_track_indexes", "audios", "beats", "canvases", "chromas",
    "color_curves", "digital_humans", "drafts", "effects", "flowers",
    "green_screens", "handwrites", "hsl", "images", "log_color_wheels",
    "loudnesses", "manual_deformations", "material_animations",
    "material_colors", "multi_language_refs", "placeholders",
    "plugin_effects", "primary_color_wheels", "realtime_denoises",
    "shapes", "smart_crops", "smart_relights", "sound_channel_mappings",
    "speeds", "stickers", "tail_leaders", "text_templates", "texts",
    "time_marks", "transitions", "video_effects", "video_trackings",
    "videos", "vocal_beautifys", "vocal_separations",
)

# Default project config, as observed in a real CapCut draft.
_DEFAULT_CONFIG = {
    "adjust_max_index": 1,
    "attachment_info": [],
    "combination_max_index": 1,
    "export_range": None,
    "extract_audio_last_index": 1,
    "lyrics_recognition_id": "",
    "lyrics_sync": True,
    "lyrics_taskinfo": [],
    "maintrack_adsorb": True,
    "material_save_mode": 0,
    "multi_language_current": "none",
    "multi_language_list": [],
    "multi_language_main": "none",
    "multi_language_mode": "none",
    "original_sound_last_index": 1,
    "record_audio_last_index": 1,
    "sticker_max_index": 1,
    "subtitle_keywords_config": None,
    "subtitle_recognition_id": "",
    "subtitle_sync": True,
    "subtitle_taskinfo": [],
    "system_font_list": [],
    "video_mute": False,
    "zoom_info_params": None,
}


class CapCutError(ValueError):
    """Plain-language CapCut export failure — safe to show as-is."""


def _new_id() -> str:
    """A CapCut-style id: uuid hex for materials/segments/tracks."""
    return uuid.uuid4().hex


def _new_draft_id() -> str:
    """The draft's own id: uppercase uuid with dashes, as CapCut writes."""
    return str(uuid.uuid4()).upper()


def _us(seconds: float) -> int:
    """Seconds -> microseconds, CapCut's time unit."""
    return max(0, int(round(float(seconds) * 1_000_000)))


def _slug(text: str) -> str:
    slug = re.sub(r"[-\s]+", "_", re.sub(r"[^\w\s-]", "", str(text or "")).strip())
    return slug[:32] or "clip"


def get_capcut_drafts_dir(data_dir: str | Path) -> tuple[Path, bool]:
    """Where to put the draft folder.

    When CapCut Desktop is installed on this machine its own drafts
    folder exists, and a draft placed there shows up in CapCut's
    project list with no import step. Otherwise the draft goes under
    ``<data_dir>/capcut_drafts`` and the user opens it from CapCut
    with File > Open project (or by moving the folder). Returns
    ``(folder, in_capcut_folder)``.
    """
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidate = (
            Path(local_app) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
        )
        if candidate.is_dir():
            return candidate, True
    fallback = Path(data_dir) / "capcut_drafts"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, False


def _timerange(start_us: int, duration_us: int) -> dict:
    return {"start": int(start_us), "duration": max(0, int(duration_us))}


def _clip_settings() -> dict:
    """Identity image adjustments (no crop, no rotation, no move)."""
    return {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": 1.0, "y": 1.0},
        "transform": {"x": 0.0, "y": 0.0},
    }


def _base_segment(segment_id: str, material_id: str, target: dict) -> dict:
    """Fields every CapCut segment carries, whatever its type."""
    return {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "visible": True,
        "id": segment_id,
        "material_id": material_id,
        "target_timerange": target,
        "common_keyframes": [],
        "keyframe_refs": [],
    }


def _video_material(
    path: str, duration_us: int, width: int, height: int, name: str
) -> tuple[dict, str]:
    """A local video material entry; returns (entry, material_id)."""
    material_id = _new_id()
    entry = {
        "audio_fade": None,
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "crop": {
            "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 0.0,
            "lower_left_x": 0.0, "lower_left_y": 1.0,
            "lower_right_x": 1.0, "lower_right_y": 1.0,
        },
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "duration": duration_us,
        "height": height,
        "id": material_id,
        "local_material_id": material_id,
        "material_id": material_id,
        "material_name": name,
        "media_path": "",
        "path": path,
        "remote_url": None,
        "type": "video",
        "width": width,
    }
    return entry, material_id


def _video_segment(
    material_id: str, duration_us: int, speed_id: str
) -> dict:
    """The video segment on the main track (full clip, from 0)."""
    seg = _base_segment(_new_id(), material_id, _timerange(0, duration_us))
    seg.update(
        {
            "source_timerange": _timerange(0, duration_us),
            "speed": 1.0,
            "volume": 1.0,
            "extra_material_refs": [speed_id],
            "clip": _clip_settings(),
            "uniform_scale": {"on": True, "value": 1.0},
            "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        }
    )
    return seg


def _text_style(size: float, bold: bool, text_len: int) -> dict:
    """One style run: white text with a black stroke, so it reads over video."""
    return {
        "fill": {
            "alpha": 1.0,
            "content": {
                "render_type": "solid",
                "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]},
            },
        },
        "range": [0, text_len],
        "size": size,
        "bold": bold,
        "italic": False,
        "underline": False,
        "strokes": [
            {
                "content": {"solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}},
                "width": 0.08,
            }
        ],
    }


def _text_material(text: str, *, size: float, bold: bool) -> tuple[dict, str]:
    """A text material entry; returns (entry, material_id)."""
    material_id = _new_id()
    content = {"styles": [_text_style(size, bold, len(text))], "text": text}
    entry = {
        "id": material_id,
        "content": json.dumps(content, ensure_ascii=False),
        "typesetting": 0,
        "alignment": 1,
        "letter_spacing": 0,
        "line_spacing": 0.02,
        "line_feed": 1,
        "line_max_width": 0.82,
        "force_apply_line_max_width": False,
        "check_flag": 7,
        "type": "text",
        "fixed_width": -1,
        "fixed_height": -1,
        "font_category_id": "",
        "font_category_name": "",
        "font_id": "",
        "font_name": "",
        "font_path": "",
        "font_resource_id": "",
        "font_size": 15.0,
        "font_source_platform": 0,
        "font_team_id": "",
        "font_title": "none",
        "font_url": "",
        "fonts": [],
    }
    return entry, material_id


def _text_segment(material_id: str, start_us: int, duration_us: int) -> dict:
    """A text segment on a text track."""
    speed_id = _new_id()
    seg = _base_segment(
        _new_id(), material_id, _timerange(start_us, duration_us)
    )
    seg.update(
        {
            "source_timerange": None,
            "speed": 1.0,
            "volume": 1.0,
            "extra_material_refs": [speed_id],
            "clip": _clip_settings(),
            "uniform_scale": {"on": True, "value": 1.0},
        }
    )
    return seg, speed_id


def _track(track_type: str, name: str, render_index: int, segments: list[dict]) -> dict:
    for seg in segments:
        seg["render_index"] = render_index
    return {
        "attribute": 0,
        "flag": 0,
        "id": _new_id(),
        "is_default_name": False,
        "name": name,
        "segments": segments,
        "type": track_type,
    }


def chunk_words(words: list[dict], per_chunk: int = CAPTION_WORDS_PER_CHUNK) -> list[dict]:
    """Group timed words into caption chunks: {text, start_s, end_s}.

    Words look like ``{"w": "hello", "s": 1.2, "e": 1.5}`` (the shape
    ``transcript.json`` stores) or ``{"text": ..., "start": ...,
    "end": ...}``. Chunks shorter than 0.3 s are stretched so CapCut
    shows them; chunks are clipped to the clip, never past its end.
    """
    chunks: list[dict] = []
    for i in range(0, len(words), per_chunk):
        group = words[i : i + per_chunk]
        parts = []
        start = None
        end = None
        for word in group:
            text = str(word.get("w") or word.get("text") or "").strip()
            if not text:
                continue
            parts.append(text)
            s = word.get("s", word.get("start"))
            e = word.get("e", word.get("end"))
            if s is not None:
                start = float(s) if start is None else min(start, float(s))
            if e is not None:
                end = float(e) if end is None else max(end, float(e))
        if not parts or start is None:
            continue
        end = end if end is not None else start + 0.5
        end = max(end, start + 0.3)
        chunks.append({"text": " ".join(parts), "start_s": start, "end_s": end})
    return chunks


def build_draft_document(
    *,
    mp4_path: str,
    duration_s: float,
    width: int = CANVAS_WIDTH,
    height: int = CANVAS_HEIGHT,
    title: str = "",
    hook: str = "",
    words: list[dict] | None = None,
    clip_start_s: float = 0.0,
) -> dict:
    """Build the ``draft_content.json`` document for one clip.

    ``words`` are transcript words in *source* time; ``clip_start_s``
    shifts them into clip time (0 = the clip's first frame). Words
    outside the clip are dropped. The video material spans the whole
    clip; the hook title covers the first seconds; captions cover the
    rest in short chunks.
    """
    duration_us = _us(duration_s)
    if duration_us <= 0:
        raise CapCutError("The clip has no length — nothing to export to CapCut.")
    now_us = int(time.time() * 1_000_000)
    name = title.strip() or "ClipForge clip"

    materials: dict[str, list] = {key: [] for key in _MATERIAL_KEYS}
    tracks: list[dict] = []

    # 1. Main video track.
    video_entry, video_id = _video_material(
        mp4_path, duration_us, width, height, Path(mp4_path).name
    )
    materials["videos"].append(video_entry)
    speed_id = _new_id()
    materials["speeds"].append(
        {"curve_speed": None, "id": speed_id, "mode": 0, "speed": 1.0, "type": "speed"}
    )
    tracks.append(
        _track(
            "video",
            "ClipForge Video",
            _RENDER_INDEX_VIDEO,
            [_video_segment(video_id, duration_us, speed_id)],
        )
    )

    # 2. Hook title track (first seconds).
    hook_text = (hook or title).strip()
    if hook_text:
        hook_dur_us = min(duration_us, _us(HOOK_DURATION_S))
        entry, mat_id = _text_material(hook_text, size=10.0, bold=True)
        materials["texts"].append(entry)
        seg, seg_speed_id = _text_segment(mat_id, 0, hook_dur_us)
        materials["speeds"].append(
            {"curve_speed": None, "id": seg_speed_id, "mode": 0, "speed": 1.0, "type": "speed"}
        )
        tracks.append(
            _track("text", "Hook Title", _RENDER_INDEX_TEXT, [seg])
        )

    # 3. Captions track (3-word chunks from the transcript).
    caption_segments: list[dict] = []
    for chunk in chunk_words(words or []):
        start_s = chunk["start_s"] - clip_start_s
        end_s = chunk["end_s"] - clip_start_s
        if end_s <= 0 or start_s >= duration_s:
            continue
        start_s = max(0.0, start_s)
        end_s = min(duration_s, end_s)
        if end_s - start_s < 0.2:
            continue
        entry, mat_id = _text_material(chunk["text"], size=8.0, bold=False)
        materials["texts"].append(entry)
        seg, seg_speed_id = _text_segment(
            mat_id, _us(start_s), _us(end_s - start_s)
        )
        materials["speeds"].append(
            {"curve_speed": None, "id": seg_speed_id, "mode": 0, "speed": 1.0, "type": "speed"}
        )
        caption_segments.append(seg)
    if caption_segments:
        tracks.append(
            _track("text", "Captions", _RENDER_INDEX_TEXT, caption_segments)
        )

    tracks.sort(key=lambda t: t["segments"][0]["render_index"] if t["segments"] else 0)

    return {
        "canvas_config": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT, "ratio": "9:16"},
        "color_space": 0,
        "config": dict(_DEFAULT_CONFIG),
        "cover": None,
        "create_time": now_us,
        "duration": duration_us,
        "extra_info": None,
        "fps": CANVAS_FPS,
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": _new_draft_id(),
        "keyframe_graph_list": [],
        "keyframes": [],
        "last_modified_platform": dict(CAPCUT_PLATFORM),
        "materials": materials,
        "mutable_config": None,
        "name": name,
        "new_version": DRAFT_NEW_VERSION,
        "platform": dict(CAPCUT_PLATFORM),
        "relationships": [],
        "render_index_track_mode_on": True,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": tracks,
        "update_time": now_us,
        "version": DRAFT_VERSION,
    }


def _draft_meta_info(draft_name: str, draft_dir: Path, drafts_root: Path, duration_us: int) -> dict:
    now_us = int(time.time() * 1_000_000)
    return {
        "cloud_draft_cover": True,
        "cloud_draft_sync": True,
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_package_type": "",
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "",
            "draft_enterprise_id": "",
            "draft_enterprise_name": "",
            "enterprise_material": [],
        },
        "draft_fold_path": str(draft_dir),
        "draft_id": _new_draft_id(),
        "draft_is_ae_produce": False,
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": [
            {"type": t, "value": []} for t in (0, 1, 2, 3, 6, 7, 8)
        ],
        "draft_materials_copied_info": [],
        "draft_name": draft_name,
        "draft_need_rename_folder": False,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": str(drafts_root),
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size_": 0,
        "draft_type": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1,
        "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "tm_duration": duration_us,
    }


def _draft_settings_ini(draft_name: str, duration_us: int) -> str:
    now_s = int(time.time())
    return (
        "[General]\n"
        "cloud_last_modify_platform=windows\n"
        f"draft_create_time={now_s}\n"
        f"draft_last_edit_time={now_s}\n"
        "real_edit_keys=1\n"
        "real_edit_seconds=1\n"
    )


def write_draft(draft_dir: Path, draft_name: str, document: dict) -> list[str]:
    """Write every file of a draft folder. Returns the written filenames."""
    draft_dir.mkdir(parents=True, exist_ok=True)
    content = json.dumps(document, ensure_ascii=False, indent=2)
    written = []
    # Modern CapCut reads draft_content.json (with its own .bak and
    # template-2.tmp mirrors); older builds read draft_info.json.
    for filename in ("draft_content.json", "draft_content.json.bak", "template-2.tmp", "draft_info.json"):
        (draft_dir / filename).write_text(content, encoding="utf-8")
        written.append(filename)
    meta = _draft_meta_info(
        draft_name, draft_dir, draft_dir.parent, int(document["duration"])
    )
    (draft_dir / "draft_meta_info.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    written.append("draft_meta_info.json")
    (draft_dir / "draft_settings").write_text(
        _draft_settings_ini(draft_name, int(document["duration"])), encoding="utf-8"
    )
    written.append("draft_settings")
    return written


def probe_video(mp4_path: Path) -> tuple[float, int, int]:
    """Duration (s) and width/height of an mp4 via ffprobe.

    Falls back to (0.0, 1080, 1920) when ffprobe is missing or the
    file cannot be probed — the caller then uses the clip's stored
    duration instead.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-show_entries", "format=duration",
                "-of", "json", str(mp4_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or [{}]
        width = int(streams[0].get("width") or CANVAS_WIDTH)
        height = int(streams[0].get("height") or CANVAS_HEIGHT)
        duration = float(
            streams[0].get("duration")
            or (data.get("format") or {}).get("duration")
            or 0.0
        )
        return duration, width, height
    except Exception:
        return 0.0, CANVAS_WIDTH, CANVAS_HEIGHT


def export_clip_to_capcut(
    *,
    mp4_path: str | Path,
    duration_s: float,
    title: str = "",
    hook: str = "",
    words: list[dict] | None = None,
    clip_start_s: float = 0.0,
    draft_name: str = "",
    data_dir: str | Path,
) -> dict:
    """Export one rendered clip as a CapCut draft folder.

    Returns ``{ok, draft_path, draft_name, message, in_capcut_folder,
    tracks}``. The mp4 is linked by absolute path, not copied.
    Raises :class:`CapCutError` (plain language) when the mp4 is
    missing or the clip has no length.
    """
    mp4 = Path(mp4_path)
    if not mp4.is_file():
        raise CapCutError(
            "The rendered clip file is missing, so there is nothing to "
            f"export to CapCut ({mp4.name}). Render the clip again first."
        )
    probed_duration, width, height = probe_video(mp4)
    duration = probed_duration or float(duration_s)
    if duration <= 0:
        raise CapCutError("The clip has no length — nothing to export to CapCut.")

    drafts_root, in_capcut = get_capcut_drafts_dir(data_dir)
    name = draft_name or f"ClipForge_{_slug(title or mp4.stem)}"
    draft_dir = drafts_root / name
    if draft_dir.exists():
        # A re-export replaces the old draft of the same name.
        import shutil

        shutil.rmtree(draft_dir, ignore_errors=True)

    document = build_draft_document(
        mp4_path=str(mp4.resolve()),
        duration_s=duration,
        width=width,
        height=height,
        title=title or mp4.stem,
        hook=hook,
        words=words,
        clip_start_s=clip_start_s,
    )
    files = write_draft(draft_dir, name, document)
    track_names = [t["name"] for t in document["tracks"]]
    where = (
        "in your CapCut drafts folder — it will show in CapCut's project list"
        if in_capcut
        else "in ClipForge's CapCut drafts folder — open it in CapCut with File > Open project"
    )
    return {
        "ok": True,
        "draft_path": str(draft_dir),
        "draft_name": name,
        "message": (
            f"CapCut draft ready: {name} ({', '.join(track_names)}). "
            f"The project folder is {draft_dir}, {where}."
        ),
        "in_capcut_folder": in_capcut,
        "tracks": track_names,
        "files": files,
    }


def validate_draft(draft_dir: str | Path) -> dict:
    """Structural check of a draft folder: parse + cross-check.

    Returns ``{ok, errors, warnings, tracks, duration_s,
    media_missing}``. This is what the tests run — it proves the
    folder is well-formed and every media path resolves, not that
    CapCut itself opened it (that needs Windows/macOS).
    """
    draft_dir = Path(draft_dir)
    errors: list[str] = []
    warnings: list[str] = []
    content_path = draft_dir / "draft_content.json"
    if not content_path.is_file():
        return {
            "ok": False,
            "errors": ["draft_content.json is missing"],
            "warnings": warnings,
            "tracks": [],
            "duration_s": 0.0,
            "media_missing": [],
        }
    try:
        doc = json.loads(content_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {
            "ok": False,
            "errors": [f"draft_content.json does not parse: {exc}"],
            "warnings": warnings,
            "tracks": [],
            "duration_s": 0.0,
            "media_missing": [],
        }
    if doc.get("version") != DRAFT_VERSION:
        warnings.append(f"unexpected draft version {doc.get('version')!r}")
    canvas = doc.get("canvas_config") or {}
    if (canvas.get("width"), canvas.get("height")) != (CANVAS_WIDTH, CANVAS_HEIGHT):
        errors.append(
            f"canvas is {canvas.get('width')}x{canvas.get('height')}, expected "
            f"{CANVAS_WIDTH}x{CANVAS_HEIGHT}"
        )
    materials = doc.get("materials") or {}
    video_ids = {v.get("id") for v in materials.get("videos") or []}
    text_ids = {t.get("id") for t in materials.get("texts") or []}
    media_missing = []
    for video in materials.get("videos") or []:
        path = video.get("path") or ""
        if not path or not Path(path).is_file():
            media_missing.append(path or "(empty video path)")
    tracks = doc.get("tracks") or []
    if not tracks:
        errors.append("no tracks in the draft")
    track_names = []
    for track in tracks:
        track_names.append(track.get("name") or track.get("type") or "?")
        for seg in track.get("segments") or []:
            mid = seg.get("material_id")
            if track.get("type") == "video" and mid not in video_ids:
                errors.append(f"video segment references unknown material {mid}")
            if track.get("type") == "text" and mid not in text_ids:
                errors.append(f"text segment references unknown material {mid}")
            target = seg.get("target_timerange") or {}
            if int(target.get("duration") or 0) <= 0:
                errors.append(f"segment {seg.get('id')} has no duration")
    # Caption segments on one track must not overlap.
    for track in tracks:
        spans = sorted(
            (
                int((s.get("target_timerange") or {}).get("start") or 0),
                int((s.get("target_timerange") or {}).get("duration") or 0),
            )
            for s in track.get("segments") or []
        )
        for (s1, d1), (s2, _d2) in zip(spans, spans[1:]):
            if s2 < s1 + d1:
                errors.append(f"overlapping segments on track {track.get('name')}")
                break
    if media_missing:
        errors.append(f"media files missing: {media_missing}")
    if not (draft_dir / "draft_meta_info.json").is_file():
        warnings.append("draft_meta_info.json is missing")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "tracks": track_names,
        "duration_s": (doc.get("duration") or 0) / 1_000_000,
        "media_missing": media_missing,
    }


__all__ = [
    "CANVAS_FPS",
    "CANVAS_HEIGHT",
    "CANVAS_WIDTH",
    "CapCutError",
    "DRAFT_VERSION",
    "build_draft_document",
    "chunk_words",
    "export_clip_to_capcut",
    "get_capcut_drafts_dir",
    "probe_video",
    "validate_draft",
    "write_draft",
]
