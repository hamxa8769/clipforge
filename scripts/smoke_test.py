#!/usr/bin/env python3
"""ClipForge end-to-end smoke test — v0.1.0.

Steps:
  1. ffmpeg / ffprobe are on PATH.
  2. Generate a 40 s sample video via ``scripts/make_sample_video.py``
     (1280x720 16:9 source with burned-in spoken lines) when it exists,
     otherwise ffmpeg ``testsrc2`` directly (1080x1920). 40 s (not 5 s)
     because the highlight engine only keeps spans >= 15 s, so a 5 s
     sample can never produce a clip.
  3. ffprobe the sample: must match its generated size, H.264, ~40 s.
  4. Run the pipeline — ``clipforge.pipeline.run_pipeline(source_path,
     out_dir=..., data_dir=..., transcript_text=...,
     transcribe_engine="fallback-text", top_n=1)`` — and ffprobe the
     rendered clip for 1080x1920. The fallback transcript supplies the
     words (the test tone has no speech, and no Whisper backend is
     required for a smoke run).

Prints PASS / FAIL / BLOCKER per step and exits non-zero on any failure,
so CI and humans can rely on the exit code.

Usage:  python3 scripts/smoke_test.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLE_WIDTH, SAMPLE_HEIGHT = 1080, 1920
SAMPLE_DURATION_S = 40
TOP_N = 1

# Words rich in virality signals, spread evenly over the sample by the
# fallback transcript (one word every 0.25 s), so the heuristic engine
# finds a ~30 s candidate.
TRANSCRIPT_TEXT = " ".join(
    [
        "why",
        "stop",
        "doing",
        "this",
        "amazing",
        "secret",
        "revealed",
        "question",
        "what",
        "happens",
        "next",
        "will",
        "shock",
        "you",
        "incredible",
        "story",
        "finally",
        "truth",
        "mistake",
        "everyone",
    ]
    * 8
)

results: list[tuple[str, str, str]] = []


def report(step: str, status: str, detail: str = "") -> None:
    """Record and print one step result: PASS, FAIL, or BLOCKER."""
    results.append((step, status, detail))
    suffix = f" — {detail}" if detail else ""
    print(f"[{status}] {step}{suffix}")


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def make_sample_video(out_path: Path) -> tuple[bool, int, int]:
    """Generate the sample video.

    Uses ``scripts/make_sample_video.py`` when it exists (its CLI is
    ``--out PATH --duration SECONDS``; it makes a realistic 1280x720
    16:9 source with burned-in spoken lines), otherwise ffmpeg
    ``testsrc2`` directly (1080x1920). Returns (ok, width, height).
    """
    helper = ROOT / "scripts" / "make_sample_video.py"
    if helper.exists():
        proc = run(
            [
                sys.executable,
                str(helper),
                "--out",
                str(out_path),
                "--duration",
                str(SAMPLE_DURATION_S),
            ]
        )
        if proc.returncode != 0:
            print(proc.stderr[-2000:])
        return proc.returncode == 0, 1280, 720
    proc = run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={SAMPLE_WIDTH}x{SAMPLE_HEIGHT}:rate=30:duration={SAMPLE_DURATION_S}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={SAMPLE_DURATION_S}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out_path),
        ]
    )
    if proc.returncode != 0:
        print(proc.stderr[-2000:])
    return proc.returncode == 0, SAMPLE_WIDTH, SAMPLE_HEIGHT


def transcript_text() -> str:
    """Transcript matching the sample: the helper's spoken lines if present."""
    helper = ROOT / "scripts" / "make_sample_video.py"
    if helper.exists():
        proc = run([sys.executable, str(helper), "--print-transcript"])
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return TRANSCRIPT_TEXT


def probe(path: Path) -> dict | None:
    proc = run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def check_dimensions(
    path: Path, label: str, expected_duration: float, expected_w: int, expected_h: int
) -> bool:
    """Verify a file is an H.264 MP4 of the expected size and rough length."""
    info = probe(path)
    if info is None:
        report(label, "FAIL", f"ffprobe failed on {path}")
        return False
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        report(label, "FAIL", "no video stream found")
        return False
    width, height = video.get("width"), video.get("height")
    codec = video.get("codec_name")
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    if (width, height) != (expected_w, expected_h):
        report(label, "FAIL", f"expected {expected_w}x{expected_h}, got {width}x{height}")
        return False
    if codec != "h264":
        report(label, "FAIL", f"expected h264, got {codec}")
        return False
    if abs(duration - expected_duration) > 2.0:
        report(label, "FAIL", f"expected ~{expected_duration:.0f}s, got {duration:.1f}s")
        return False
    report(label, "PASS", f"{width}x{height} {codec} {duration:.1f}s")
    return True


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
        report("ffmpeg/ffprobe available", "FAIL", f"missing: {', '.join(missing)}")
        print("\nSMOKE TEST: FAIL (missing FFmpeg)")
        return 1
    report("ffmpeg/ffprobe available", "PASS", f"ffmpeg={ffmpeg}")

    with tempfile.TemporaryDirectory(prefix="clipforge-smoke-") as tmp:
        workdir = Path(tmp)
        sample = workdir / "sample_1080x1920.mp4"

        ok, sample_w, sample_h = make_sample_video(sample)
        if not ok or not sample.exists():
            report("generate sample video", "FAIL", "sample generation failed")
            print("\nSMOKE TEST: FAIL")
            return 1
        report("generate sample video", "PASS", f"{sample_w}x{sample_h} {str(sample)}")

        if not check_dimensions(
            sample, "sample dimensions (ffprobe)", SAMPLE_DURATION_S, sample_w, sample_h
        ):
            print("\nSMOKE TEST: FAIL")
            return 1

        # --- Pipeline step: contract is clipforge.pipeline.run_pipeline ---
        try:
            from clipforge.pipeline import run_pipeline
        except ImportError as exc:
            report(
                "pipeline run (clipforge.pipeline)",
                "BLOCKER",
                f"clipforge.pipeline not implemented yet ({exc}); "
                "cannot verify pipeline output. See ARCHITECTURE.md §7.",
            )
            print("\nSMOKE TEST: FAIL (pipeline BLOCKER)")
            return 1

        out_dir = workdir / "pipeline_out"
        data_dir = workdir / "data"
        out_dir.mkdir(exist_ok=True)
        try:
            result = run_pipeline(
                sample,
                out_dir=out_dir,
                data_dir=data_dir,
                transcript_text=transcript_text(),
                transcribe_engine="fallback-text",
                top_n=TOP_N,
            )
        except Exception as exc:  # noqa: BLE001 — smoke test must report, not crash
            report(
                "pipeline run (clipforge.pipeline)", "FAIL", f"raised {type(exc).__name__}: {exc}"
            )
            print("\nSMOKE TEST: FAIL")
            return 1
        rendered = [c for c in result.get("clips", []) if c.get("status") == "rendered"]
        if not rendered:
            failures = [c.get("error", "?") for c in result.get("clips", [])]
            report(
                "pipeline run (clipforge.pipeline)",
                "FAIL",
                f"rendered_count={result.get('rendered_count')}, errors={failures}",
            )
            print("\nSMOKE TEST: FAIL")
            return 1
        output_path = Path(rendered[0]["render"]["path"])
        report(
            "pipeline run (clipforge.pipeline)",
            "PASS",
            f"rendered {result['rendered_count']} clip(s), engine={result.get('engine')}",
        )

        if not check_dimensions(
            output_path,
            "pipeline output is 1080x1920 (ffprobe)",
            rendered[0]["render"]["duration_s"],
            SAMPLE_WIDTH,
            SAMPLE_HEIGHT,
        ):
            print("\nSMOKE TEST: FAIL")
            return 1

    failed = [r for r in results if r[1] in ("FAIL", "BLOCKER")]
    print(
        f"\nSMOKE TEST: {'FAIL' if failed else 'PASS'} "
        f"({len(results) - len(failed)}/{len(results)} steps passed)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
