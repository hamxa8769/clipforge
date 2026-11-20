#!/usr/bin/env python3
"""Generate a 60 s 1280x720 test video locally for ClipForge smoke tests.

Uses only FFmpeg's built-in sources — no downloaded media:

* video: ``testsrc2`` (moving colour pattern) at 1280x720, 30 fps
* audio: 440 Hz sine tone
* burned-in text: a rotating spoken-style line every 10 s, via drawtext
  (DejaVu Sans, which ships on this system), so the frames look like
  someone talking — enough to exercise ingest / reframe / captions /
  render end to end.

Usage:
    python3 scripts/make_sample_video.py [--out PATH] [--duration SECONDS]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Six spoken-style lines, one per 10 s segment. The pipeline's fallback
# transcript should use the same text (see --print-transcript).
LINES = [
    "Welcome back. Today I will show you the one mistake everyone makes.",
    "Stop doing this right now. Here is the secret nobody tells you.",
    "I discovered something amazing and honestly it changed everything.",
    "The truth is simple. Most people fail because they skip step one.",
    "Here is how to fix it in three easy steps starting today.",
    "That is the whole story. Try it yourself and tell me what happens.",
]


def build_drawtext(duration: int) -> str:
    """One drawtext filter per 10 s segment (enable between t ranges)."""
    parts = []
    for i, line in enumerate(LINES):
        start = i * 10
        end = min(start + 10, duration)
        if start >= duration:
            break
        safe = line.replace("'", "").replace(":", "\\:")
        parts.append(
            f"drawtext=fontfile={FONT}:text='{safe}':fontsize=36:"
            f"fontcolor=white:borderw=2:bordercolor=black:"
            f"x=(w-text_w)/2:y=h-120:enable='between(t,{start},{end})'"
        )
    return ",".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/uploads/sample_60s.mp4")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument(
        "--print-transcript",
        action="store_true",
        help="print the spoken text (for --transcript-text)",
    )
    args = parser.parse_args()

    if args.print_transcript:
        print(" ".join(LINES))
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    video_filter = f"testsrc2=size=1280x720:rate=30:duration={args.duration}"
    if Path(FONT).exists():
        video_filter += f",{build_drawtext(args.duration)}"
    else:
        print(f"warning: font not found at {FONT}, skipping drawtext", file=sys.stderr)
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_filter,
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={args.duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"ffmpeg failed: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    print("transcript text (for the fallback engine):")
    print(" ".join(LINES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
