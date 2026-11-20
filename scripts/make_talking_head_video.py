#!/usr/bin/env python3
"""Generate a synthetic talking-head test video locally (no real faces).

Draws a moving, cartoon-style face with OpenCV: a skin-tone ellipse
that sways side to side, eyes, brows, a mouth that opens and closes
("talking"), and shoulders — on a plain background. It is a *test
fixture*, not a person: no photo, no downloaded media, no real face.

Two modes:
* ``moving`` — the head sways ±120 px so a working face tracker must
  move the 9:16 crop window with it (vs. a centre-crop fallback).
* ``still`` — the head stays near centre (control: the crop should
  stay near centre even when tracking engages).

Requires ``opencv-python-headless`` (the ``vision`` extra). The system
Python on this dev VM has no cv2; use a venv with it, e.g.::

    ~/workspace/.cf-face-venv/bin/python scripts/make_talking_head_video.py

Usage:
    python3 scripts/make_talking_head_video.py [--out PATH]
        [--duration SECONDS] [--mode moving|still] [--width W] [--height H]
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path


def draw_frame(frame, t: float, duration: float, width: int, height: int, mode: str):
    """Draw one frame of the synthetic talking head (mutates ``frame``)."""
    import cv2

    # Plain background (vertical gradient, wall-like).
    for y in range(height):
        shade = 200 - int(30 * y / height)
        frame[y, :] = (shade, shade - 10, shade - 25)  # BGR, warm grey
    # Head centre: sway for "moving", near-centre for "still".
    if mode == "moving":
        cx = int(width / 2 + 120 * math.sin(2 * math.pi * t / duration))
    else:
        cx = int(width / 2 + 10 * math.sin(2 * math.pi * t / duration))
    cy = int(height * 0.42 + 8 * math.sin(2 * math.pi * t / 2.0))
    rx, ry = int(width * 0.11), int(height * 0.22)

    # Shoulders / shirt.
    cv2.ellipse(
        frame,
        (cx, int(height * 1.05)),
        (int(rx * 2.1), int(height * 0.42)),
        0,
        0,
        360,
        (60, 60, 90),
        -1,
    )
    # Neck.
    cv2.rectangle(frame, (cx - 28, cy + ry - 20), (cx + 28, cy + ry + 70), (190, 150, 120), -1)
    # Face (skin tone ellipse) + ears.
    skin = (195, 165, 135)  # BGR skin tone
    cv2.ellipse(frame, (cx - rx, cy), (18, 30), 0, 0, 360, skin, -1)
    cv2.ellipse(frame, (cx + rx, cy), (18, 30), 0, 0, 360, skin, -1)
    cv2.ellipse(frame, (cx, cy), (rx, ry), 0, 0, 360, skin, -1)
    # Shading: darker jaw edge for contrast (helps Haar).
    cv2.ellipse(frame, (cx, cy + 30), (rx - 8, ry - 30), 0, 0, 360, (175, 145, 115), 2)
    dark = (30, 30, 30)
    white = (245, 245, 245)
    # Brows.
    cv2.ellipse(frame, (cx - 45, cy - 55), (32, 8), -8, 0, 360, dark, -1)
    cv2.ellipse(frame, (cx + 45, cy - 55), (32, 8), 8, 0, 360, dark, -1)
    # Eyes: white sclera + dark pupils (pupils look slightly sideways).
    for ex in (cx - 45, cx + 45):
        cv2.ellipse(frame, (ex, cy - 30), (20, 12), 0, 0, 360, white, -1)
        cv2.circle(frame, (ex + 4, cy - 30), 7, dark, -1)
    # Nose: shadow line.
    cv2.line(frame, (cx, cy - 20), (cx - 6, cy + 25), (150, 115, 85), 3)
    # Mouth: opens/closes while "talking" (2 Hz), dark interior + teeth line.
    open_h = int(6 + 14 * abs(math.sin(2 * math.pi * t * 2.0)))
    cv2.ellipse(frame, (cx, cy + 75), (34, open_h), 0, 0, 360, (40, 30, 60), -1)
    if open_h > 10:
        cv2.ellipse(frame, (cx, cy + 75 - open_h + 6), (28, 5), 0, 0, 360, white, -1)
    # Foreground label (not part of the face).
    cv2.putText(
        frame,
        "synthetic talking-head fixture",
        (20, height - 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (40, 40, 40),
        2,
    )
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/face-test/talking_head_moving.mp4")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--mode", choices=("moving", "still"), default="moving")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    try:
        import cv2
        import numpy as np
    except ImportError:
        print(
            "error: OpenCV (cv2) is required — install the 'vision' extra "
            "(opencv-python-headless) in a venv and re-run.",
            flush=True,
        )
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(out), cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (args.width, args.height)
    )
    if not writer.isOpened():
        print(f"error: could not open VideoWriter for {out}", flush=True)
        return 1
    n_frames = int(args.duration * args.fps)
    for i in range(n_frames):
        frame = np.zeros((args.height, args.width, 3), dtype=np.uint8)
        draw_frame(frame, i / args.fps, args.duration, args.width, args.height, args.mode)
        writer.write(frame)
    writer.release()
    print(f"wrote {out} ({out.stat().st_size} bytes, {args.duration}s, mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
