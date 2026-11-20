#!/usr/bin/env python3
"""Run the face-tracked reframe on synthetic talking-head videos and
record, honestly, whether tracking engaged or the centre-crop fallback
ran. Writes ``docs/face-tracking-test.md`` (human-readable evidence)
and ``docs/face-tracking-test.json`` (machine-readable result).

This is a *measurement*, not a pass/fail gate: the script exits 0
whenever the reframe runs and produces a valid plan — the report
states which mode engaged. A synthetic cartoon face may or may not
trigger the Haar cascade; both outcomes are reported as-is.

Requires OpenCV (the ``vision`` extra) for tracking; without it the
reframe falls back to centre crop by design and the report says so.
Run with the venv that has cv2, e.g.::

    PYTHONPATH=src ~/workspace/.cf-face-venv/bin/python \\
        scripts/face_tracking_test.py --videos /tmp/face-test/*.mp4

Videos are generated first with scripts/make_talking_head_video.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def probe_dimensions(path: Path) -> tuple[int, int, float]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    info = json.loads(proc.stdout)
    stream = info["streams"][0]
    return int(stream["width"]), int(stream["height"]), float(info["format"]["duration"])


def run_one(path: Path) -> dict:
    from clipforge.reframe import compute_crop_path

    width, height, duration = probe_dimensions(path)
    try:
        import cv2

        cv2_version = cv2.__version__
        try:
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
            haar_available = not cascade.empty()
        except AttributeError:
            haar_available = False  # OpenCV 5.x headless: no CascadeClassifier
    except ImportError:
        cv2_version = None
        haar_available = False
    plan = compute_crop_path(str(path), duration, width, height)
    xs = [p.x for p in plan.crop_points]
    centre = (width - plan.crop_width) / 2.0
    return {
        "video": str(path),
        "width": width,
        "height": height,
        "duration_s": round(duration, 2),
        "opencv_version": cv2_version,
        "haar_cascade_available": haar_available,
        "face_tracked": plan.face_tracked,
        "mode": "face-tracked" if plan.face_tracked else "centre-crop fallback",
        "crop_points": len(plan.crop_points),
        "crop_x_min": round(min(xs), 1) if xs else None,
        "crop_x_max": round(max(xs), 1) if xs else None,
        "crop_x_centre": round(centre, 1),
        "crop_x_range": round((max(xs) - min(xs)) if xs else 0.0, 1),
        "shot_cuts_s": plan.shot_cuts_s,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--videos", nargs="+", required=True)
    parser.add_argument("--out-md", default=str(ROOT / "docs" / "face-tracking-test.md"))
    parser.add_argument("--out-json", default=str(ROOT / "docs" / "face-tracking-test.json"))
    args = parser.parse_args()

    results = [run_one(Path(v)) for v in args.videos]
    Path(args.out_json).write_text(json.dumps(results, indent=2) + "\n")

    lines = [
        "# Face-tracking test — synthetic talking-head video",
        "",
        "Generated locally with `scripts/make_talking_head_video.py` (OpenCV-drawn",
        "cartoon face: skin-tone ellipse, eyes, brows, talking mouth, shoulders —",
        "no real person, no photo, no downloaded media) and run through the",
        "existing face-tracked reframe (`clipforge.reframe.compute_crop_path`).",
        "Run with `scripts/face_tracking_test.py`. This is a measurement, not a",
        "pass/fail gate: the table states whether tracking engaged or the",
        "centre-crop fallback ran.",
        "",
        "| Video | OpenCV | Haar cascade | Mode | Crop points | Crop-x range | Centre x |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| `{Path(r['video']).name}` | {r['opencv_version'] or 'not installed'} | "
            f"{'yes' if r['haar_cascade_available'] else 'no'} | **{r['mode']}** | "
            f"{r['crop_points']} | {r['crop_x_range']} px | {r['crop_x_centre']} px |"
        )
    lines += [
        "",
        "## How to read it",
        "",
        "- **face-tracked**: the Haar cascade found a face in at least one sampled",
        "  frame and the crop window followed it. A large crop-x range on the",
        "  `moving` video (head swaying ±120 px) means the window actually moved",
        "  with the speaker; a range near 0 on the `still` video is expected.",
        "- **centre-crop fallback**: no face was detected (or no usable OpenCV),",
        "  so the crop stayed centred. The pipeline still renders a valid",
        "  1080x1920 clip — this is the designed fallback, not a failure.",
        "- OpenCV 5.x headless wheels ship no `CascadeClassifier` / haarcascade",
        "  data, so tracking cannot engage there even with cv2 installed; the",
        "  reframe module detects that and falls back. OpenCV 4.10 headless does",
        "  ship the cascade. The `vision` extra pins no major version, so",
        "  deployments should pin `opencv-python-headless==4.10.*` until the",
        "  reframe is moved to a DNN detector.",
        "",
        "## Raw results",
        "",
        "```json",
        json.dumps(results, indent=2),
        "```",
        "",
        "## What is still unverified",
        "",
        "- Real-human face tracking (a real talking-head recording) — no such",
        "  asset exists in this repo and none was downloaded for this test.",
        "- Whisper/LLM live paths and platform uploads — still deferred, no keys.",
        "",
        "## Follow-up probe (same videos, tuned detector settings)",
        "",
        "To check whether the reframe's fixed detector settings were the",
        "limiting factor, the first frame of the `moving` video was also run",
        "through the cascade directly with `scaleFactor` 1.05/1.1/1.2,",
        "`minNeighbors` 3/5, histogram equalisation on/off, and the `alt`,",
        "`alt2` and `profileface` cascades. Result: **0 faces in every",
        "combination**. The cartoon face does not trigger Haar at all — the",
        "cascade is trained on real faces — so the synthetic fixture cannot",
        "validate tracking engagement. It does validate the fallback contract:",
        "a valid, centred 9:16 plan is always produced.",
        "",
    ]
    Path(args.out_md).write_text("\n".join(lines))
    for r in results:
        print(
            f"{Path(r['video']).name}: opencv={r['opencv_version']} "
            f"haar={r['haar_cascade_available']} mode={r['mode']} "
            f"points={r['crop_points']} x_range={r['crop_x_range']}px"
        )
    print(f"wrote {args.out_md} and {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
