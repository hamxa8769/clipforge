#!/usr/bin/env python3
"""Build the ``clipforge-server`` executable and the release zip.

Usage (from the repo root):

    python scripts/build_exe.py [--with-web]

* Builds the one-file PyInstaller executable for the current platform:
  ``dist/clipforge-server`` (``clipforge-server.exe`` on Windows).
* ``--with-web`` also builds the Next.js app (``output: "standalone"``)
  and includes it in the zip under ``web/``.
* Stages everything under ``dist/ClipForge-v<version>-<Windows|Linux>/``
  and zips it to ``dist/ClipForge-v<version>-<Windows|Linux>.zip``:

      clipforge-server(.exe)   one-file API server, no Python needed
      web/                     Next.js standalone build (needs Node 20+)
      start-server.bat/.sh     start the API server
      start-web.bat/.sh        start the web UI (needs Node 20+)
      README.txt               setup instructions for the zip
      .env.example             copy to .env and fill in what you need

The zip is what the release workflow uploads as a GitHub Release asset.
FFmpeg is NOT bundled — it stays a separate prerequisite for clipping
(see README.txt).

Requires: Python 3.11+ (PyInstaller is installed automatically if it is
missing) and, with ``--with-web``, Node 20+ with ``npm``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PACKAGING = ROOT / "packaging"


def _version() -> str:
    text = (ROOT / "src" / "clipforge" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("could not read __version__ from src/clipforge/__init__.py")
    return match.group(1)


def _platform() -> str:
    return "Windows" if sys.platform == "win32" else "Linux"


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(cmd)}", flush=True)
    merged = dict(os.environ)
    if env:
        merged.update(env)
    subprocess.run(cmd, cwd=cwd, env=merged, check=True)


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found — installing it...", flush=True)
        _run([sys.executable, "-m", "pip", "install", "pyinstaller"])


def _build_exe() -> Path:
    _ensure_pyinstaller()
    _run(
        [sys.executable, "-m", "PyInstaller", "clipforge-server.spec", "--noconfirm", "--clean"],
        cwd=ROOT,
    )
    exe_name = "clipforge-server.exe" if sys.platform == "win32" else "clipforge-server"
    exe = DIST / exe_name
    if not exe.is_file():
        raise SystemExit(f"PyInstaller did not produce {exe}")
    print(f"Built {exe} ({exe.stat().st_size / 1_000_000:.1f} MB)", flush=True)
    return exe


def _npm() -> str:
    # On Windows `npm` is npm.cmd — subprocess cannot run a bare "npm"
    # (CreateProcess does not resolve .cmd the way a shell does), so
    # resolve the real executable path first.
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise SystemExit("npm not found — Node.js 20+ is required to build the web UI")
    return npm


def _build_web(stage_web: Path) -> None:
    web = ROOT / "apps" / "web"
    if not (web / "package.json").is_file():
        raise SystemExit("apps/web/package.json not found")
    npm = _npm()
    # The web UI calls the API on localhost:8000 by default, which is
    # where the packaged exe listens; bake that in explicitly.
    _run([npm, "ci"], cwd=web)
    _run([npm, "run", "build"], cwd=web, env={"NEXT_PUBLIC_API_URL": "http://localhost:8000"})
    standalone = web / ".next" / "standalone"
    if not (standalone / "server.js").is_file():
        raise SystemExit(
            "Next.js standalone output not found at apps/web/.next/standalone/server.js — "
            'is `output: "standalone"` set in apps/web/next.config.ts?'
        )
    shutil.copytree(standalone, stage_web, dirs_exist_ok=True)
    # Static assets live outside the standalone folder.
    static_src = web / ".next" / "static"
    if static_src.is_dir():
        shutil.copytree(static_src, stage_web / ".next" / "static", dirs_exist_ok=True)
    public_src = web / "public"
    if public_src.is_dir():
        shutil.copytree(public_src, stage_web / "public", dirs_exist_ok=True)
    print(f"Staged web UI at {stage_web}", flush=True)


def _stage(exe: Path, with_web: bool) -> tuple[Path, Path]:
    version = _version()
    platform = _platform()
    stage = DIST / f"ClipForge-v{version}-{platform}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    shutil.copy2(exe, stage / exe.name)
    for name in (
        "README.txt",
        "start-server.bat",
        "start-server.sh",
        "start-web.bat",
        "start-web.sh",
    ):
        src = PACKAGING / name
        if src.is_file():
            shutil.copy2(src, stage / name)
    env_example = ROOT / ".env.example"
    if env_example.is_file():
        shutil.copy2(env_example, stage / ".env.example")
    if with_web:
        _build_web(stage / "web")

    zip_path = DIST / f"ClipForge-v{version}-{platform}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(DIST))
    print(f"Staged {stage}", flush=True)
    print(f"Zip: {zip_path} ({zip_path.stat().st_size / 1_000_000:.1f} MB)", flush=True)
    return stage, zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-web",
        action="store_true",
        help="also build the Next.js UI and include it in the zip",
    )
    args = parser.parse_args(argv)

    if sys.version_info < (3, 11):  # noqa: UP036  (guard for pre-3.11 interpreters)
        raise SystemExit("ClipForge needs Python 3.11+ to build the executable")
    DIST.mkdir(parents=True, exist_ok=True)
    exe = _build_exe()
    stage, zip_path = _stage(exe, with_web=args.with_web)
    print()
    print("Done.")
    print(f"  Executable: {exe}")
    print(f"  Stage dir:  {stage}")
    print(f"  Zip:        {zip_path}")
    print(f"  Smoke-test the exe with:  python scripts/smoke_exe.py {exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
