#!/usr/bin/env python3
"""Install FFmpeg for ClipForge from the command line (v0.4.1).

Same installer the app's one-click "Install FFmpeg" button uses
(``clipforge.ffmpeg_install``) — official build for the platform,
checksum-checked when the checksum file is reachable, extracted to
``<data_dir>/ffmpeg/`` and verified with ``ffmpeg -version``. On
Windows the folder is also added to the user PATH (registry
``HKCU\\Environment``).

Usage:
    python scripts/install-ffmpeg.py [--data-dir ./data] [--force]

    --data-dir   ClipForge data dir (default: $CLIPFORGE_DATA_DIR or ./data)
    --force      Re-install even if FFmpeg is already installed

Exit code 0 when FFmpeg is installed and verified, 1 otherwise.
No secrets, no network calls besides the official FFmpeg download.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Make `src/clipforge/...` importable from the repo root or anywhere else.
for _candidate in (
    Path(__file__).resolve().parents[1] / "src",
    Path.cwd() / "src",
):
    if (_candidate / "clipforge").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from clipforge.ffmpeg_install import (  # noqa: E402
    FFMPEG_DOWNLOAD_PAGE,
    FfmpegInstaller,
    download_url_for_platform,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install FFmpeg for ClipForge (one-click installer, CLI form)."
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("CLIPFORGE_DATA_DIR", "./data"),
        help="ClipForge data dir (default: $CLIPFORGE_DATA_DIR or ./data)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-install even if FFmpeg is already installed"
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    url = download_url_for_platform()
    if url is None:
        print("One-click install is not available on macOS yet.")
        print("Install FFmpeg with: brew install ffmpeg")
        print(f"(or download it from {FFMPEG_DOWNLOAD_PAGE})")
        return 1

    print(f"FFmpeg source: {url}")
    installer = FfmpegInstaller(data_dir)
    status = installer.start_install(force=args.force)
    if status["state"] == "done":
        print(f"FFmpeg is already installed: {status['path']}")
        if status.get("version"):
            print(status["version"])
        return 0

    last_pct = -1.0
    while installer.is_running:
        status = installer.status()
        pct = status["progress_pct"]
        if pct != last_pct:
            extra = ""
            if status["total_bytes"]:
                down_mb = status["downloaded_bytes"] / 1_000_000
                total_mb = status["total_bytes"] / 1_000_000
                extra = f" ({down_mb:.1f} / {total_mb:.1f} MB)"
            print(f"\r{status['message']} {pct:.0f}%{extra}", end="", flush=True)
            last_pct = pct
        time.sleep(0.5)
    print()
    status = installer.status()
    if status["state"] == "done":
        print(f"FFmpeg installed: {status['path']}")
        if status.get("version"):
            print(status["version"])
        if status.get("path_added"):
            print("Added to your Windows PATH — new terminals will find ffmpeg too.")
        return 0
    print(f"Install failed: {status['error'] or 'unknown error'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
