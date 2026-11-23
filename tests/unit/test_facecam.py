"""Unit tests for Facecam Studio layouts (``clipforge.facecam``).

Pure-function tests: box math, layout geometry, filter-graph shape,
preset store round-trips. No FFmpeg needed; the real render is covered
by ``tests/unit/test_render_facecam.py``.
"""

from __future__ import annotations

import pytest

from clipforge.facecam import (
    Box,
    FacecamError,
    FacecamLayout,
    FacecamPresetStore,
    build_facecam_filter,
    layout_geometry,
)


def test_box_clamp_keeps_minimum_size_inside_frame():
    clamped = Box(x=-0.2, y=1.2, w=2.0, h=0.01).clamp()
    assert (clamped.x, clamped.y) == (0.0, 0.95)
    assert clamped.w == pytest.approx(1.0) and clamped.h == pytest.approx(0.05)
    edge = Box(x=0.9, y=0.9, w=0.5, h=0.5).clamp()
    assert edge.w == pytest.approx(0.1) and edge.h == pytest.approx(0.1)


def test_box_to_pixels_rounds_and_clamps():
    assert Box(0.25, 0.0, 0.5, 0.35).to_pixels(1920, 1080) == (480, 0, 960, 378)


def test_split_geometry_halves_come_from_ratio():
    layout = FacecamLayout(mode="split", split_ratio=0.35)
    geo = layout_geometry(layout, 1920, 1080)
    assert geo.cam_h == 672
    assert geo.game_h == 1920 - 672
    assert geo.cam_h % 2 == 0 and geo.game_h % 2 == 0
    # Captions sit on the gameplay half, below the divider.
    assert geo.cam_h < geo.caption_y < 1920


def test_split_geometry_ratio_bounds():
    with pytest.raises(FacecamError):
        FacecamLayout(mode="split", split_ratio=0.1).validate()
    with pytest.raises(FacecamError):
        FacecamLayout(mode="split", split_ratio=0.9).validate()


def test_pip_geometry_corners():
    layout = FacecamLayout(mode="pip", pip_pos="top-right", pip_size_frac=0.35)
    geo = layout_geometry(layout, 1920, 1080)
    assert geo.pip_w == 378
    assert geo.pip_x == 1080 - 378 - 26
    assert geo.pip_y == 26
    bottom = layout_geometry(
        FacecamLayout(mode="pip", pip_pos="bottom-left", pip_size_frac=0.35), 1920, 1080
    )
    assert bottom.pip_x == 26
    assert bottom.pip_y == 1920 - bottom.pip_h - 240
    # Bottom PiP pushes the caption line above the PiP overlay.
    assert bottom.caption_y < bottom.pip_y


def test_invalid_mode_and_pip_pos_rejected():
    with pytest.raises(FacecamError):
        FacecamLayout(mode="diagonal").validate()
    with pytest.raises(FacecamError):
        FacecamLayout(mode="pip", pip_pos="middle").validate()


def test_from_dict_defaults_and_unknown_keys():
    layout = FacecamLayout.from_dict({"mode": "zoom"})
    assert layout.mode == "zoom"
    assert layout.facecam == Box(x=0.0, y=0.0, w=1.0, h=0.35)
    assert layout.divider is True
    assert FacecamLayout.from_dict(None).mode == "split"
    with pytest.raises(FacecamError):
        FacecamLayout.from_dict("split")


def test_split_filter_uses_vstack_and_divider():
    graph, label = build_facecam_filter(FacecamLayout(mode="split"), 1920, 1080)
    assert label == "v0"
    assert "vstack=inputs=2" in graph
    assert "drawbox" in graph  # divider line
    assert "crop=1920:378:0:0" in graph  # facecam box -> top strip
    assert "scale=1080:672" in graph
    assert "scale=1080:1248" in graph


def test_split_filter_without_divider_has_no_drawbox():
    graph, _ = build_facecam_filter(
        FacecamLayout(mode="split", divider=False), 1920, 1080
    )
    assert "drawbox" not in graph


def test_pip_filter_overlays_corner_cam():
    graph, _ = build_facecam_filter(
        FacecamLayout(mode="pip", pip_pos="top-right"), 1920, 1080
    )
    assert "overlay=676:26" in graph
    assert "scale=1080:1920" in graph  # gameplay fills the frame


def test_zoom_and_blur_fill_use_blurred_background():
    zoom, _ = build_facecam_filter(FacecamLayout(mode="zoom"), 1920, 1080)
    assert "gblur=sigma=40" in zoom
    assert "overlay=0:(H-h)/2" in zoom
    blur, _ = build_facecam_filter(FacecamLayout(mode="blur_fill"), 1920, 1080)
    assert "gblur=sigma=40" in blur
    assert "overlay=(W-w)/2:(H-h)/2" in blur


def test_fullscreen_filter_is_a_plain_fill():
    graph, _ = build_facecam_filter(FacecamLayout(mode="fullscreen"), 1920, 1080)
    assert "scale=1080:1920" in graph
    assert "overlay" not in graph and "vstack" not in graph


def test_ass_path_adds_ass_filter_and_vout_label():
    graph, label = build_facecam_filter(
        FacecamLayout(mode="split"), 1920, 1080, ass_path="clip.ass"
    )
    assert label == "vout"
    assert "[v0]ass=clip.ass[vout]" in graph


def test_preset_store_round_trip(tmp_path):
    store = FacecamPresetStore(tmp_path)
    layout = FacecamLayout(mode="pip", pip_pos="bottom-left")
    store.save_preset("speed", "gta-run", layout)
    assert store.list_presets("speed")["gta-run"]["mode"] == "pip"
    assert store.load_preset("speed", "gta-run").pip_pos == "bottom-left"
    assert store.list_all()["speed"]["gta-run"]["pip_pos"] == "bottom-left"
    assert store.delete_preset("speed", "gta-run") is True
    assert store.list_presets("speed") == {}
    assert store.delete_preset("speed", "gta-run") is False


def test_preset_store_rejects_bad_names_and_missing(tmp_path):
    store = FacecamPresetStore(tmp_path)
    with pytest.raises(FacecamError):
        store.save_preset("speed", "a/b", FacecamLayout())
    with pytest.raises(FacecamError):
        store.save_preset("speed", "", FacecamLayout())
    with pytest.raises(FacecamError):
        store.load_preset("speed", "nope")
