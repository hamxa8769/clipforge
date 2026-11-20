"""Face-tracking contract tests (Phase 2, task 6).

These tests verify the *contract* of the reframe — a valid 9:16 plan
is always produced, crops stay in bounds, and the ``face_tracked``
flag honestly reports whether the detector engaged. They deliberately
do NOT assert that tracking engages on the synthetic video: the
synthetic cartoon face does not trigger the Haar cascade (see
``docs/face-tracking-test.md`` — 0 detections across all cascade
variants and tunings), so asserting engagement would be faking a pass.

The video-based test is skipped when OpenCV / the Haar cascade is
unavailable (system Python has no cv2; OpenCV 5.x headless ships no
CascadeClassifier). The pure-function tests always run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from clipforge.reframe import ReframePlan, _smooth_with_cut_resets, compute_crop_path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _haar_available() -> bool:
    try:
        import cv2

        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        return not cascade.empty()
    except (AttributeError, Exception):
        return False


def _make_talking_head(tmp_path, duration_s: float = 6.0):
    """Small synthetic talking-head video (same fixture as the script)."""
    import cv2
    import numpy as np
    from make_talking_head_video import draw_frame

    out = tmp_path / "talking_head.mp4"
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), 30, (1280, 720))
    for i in range(int(duration_s * 30)):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        draw_frame(frame, i / 30, duration_s, 1280, 720, "moving")
        writer.write(frame)
    writer.release()
    return out


@pytest.mark.skipif(not _haar_available(), reason="Haar cascade unavailable (no cv2 4.x)")
def test_reframe_on_synthetic_talking_head_reports_engagement_honestly(tmp_path):
    video = _make_talking_head(tmp_path)
    plan = compute_crop_path(str(video), 6.0, 1280, 720)
    assert plan.crop_width == int(round(720 * 9 / 16))
    assert plan.crop_height == 720
    assert len(plan.crop_points) > 0
    for point in plan.crop_points:
        assert 0.0 <= point.x <= 1280 - plan.crop_width
    # Honest reporting, not a faked pass: record which mode engaged.
    result = {
        "face_tracked": plan.face_tracked,
        "mode": "face-tracked" if plan.face_tracked else "centre-crop fallback",
        "crop_points": len(plan.crop_points),
        "crop_x_range": max(p.x for p in plan.crop_points) - min(p.x for p in plan.crop_points),
    }
    (tmp_path / "face_tracking_result.json").write_text(json.dumps(result, indent=2))
    assert isinstance(plan.face_tracked, bool)
    if not plan.face_tracked:
        # Fallback contract: the whole path is the centre crop.
        centre = (1280 - plan.crop_width) / 2.0
        assert all(p.x == pytest.approx(centre) for p in plan.crop_points)


def test_smooth_with_cut_resets_holds_last_value_and_resets_on_cut():
    # No face (None) holds the last good x; a cut clears the history.
    # History accumulates the held value each frame: [100], [100,110],
    # [100,110,110], [100,110,110,110], [100,110,110,110,120].
    xs = [100.0, 110.0, None, None, 120.0]
    cuts = [False, False, False, False, False]
    out = _smooth_with_cut_resets(xs, cuts, window=5)
    assert out[2] == pytest.approx(320 / 3)
    assert out[4] == pytest.approx(110.0)
    cut_out = _smooth_with_cut_resets([100.0, 110.0, 120.0], [False, False, True], window=5)
    assert cut_out[2] == pytest.approx(120.0)  # history cleared at the cut


def test_crop_x_at_centre_when_no_points():
    empty = ReframePlan(crop_width=405, crop_height=720, source_width=1280, source_height=720)
    assert empty.crop_x_at(5.0) == pytest.approx((1280 - 405) / 2.0)
    assert empty.to_dict()["face_tracked"] is False
