# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ``clipforge-server`` — one-file API server executable.

Build (from the repo root):

    python scripts/build_exe.py [--with-web]
    pyinstaller clipforge-server.spec --noconfirm --clean

The spec works on Windows and Linux, but an executable only runs on the
platform it was built on: build the Windows .exe on a Windows machine
(or the windows-latest CI runner) and the Linux binary on Linux. Never
try to build a Windows .exe on Linux.

What gets bundled:

* ``apps/api/clipforge_api/__main__.py`` — the launcher (loads .env,
  pins the data dir next to the exe, serves a static web export if
  present, then runs uvicorn).
* The whole ``clipforge`` package from ``src/`` plus FastAPI / uvicorn /
  SQLAlchemy / pydantic. Optional heavy backends (faster-whisper, cv2)
  are lazy imports in the source, so they are NOT bundled — the exe
  runs the deterministic / API fallbacks without them.
* faster-whisper is DELIBERATELY not bundled (v0.10.2): ctranslate2
  brings large native DLLs, an AVX2 CPU requirement and a big size
  increase, and bundling would ignore the user's own install and
  pre-downloaded model. Instead the server detects a system-Python
  install at runtime (``clipforge.whisper_system``: it probes the
  Pythons on PATH / the ``py`` launcher) and transcribes in that
  Python as a subprocess — so ``python -m pip install faster-whisper``
  in PowerShell is picked up on Re-check with no restart, and both
  paths share the same Hugging Face model cache. A frozen exe can
  never see the system Python's site-packages, so any future change
  that goes back to an in-process-only import WILL re-break this.
* FFmpeg is NOT bundled: it is a separate prerequisite for clipping
  (see README.txt in the release zip).

Output: ``dist/clipforge-server`` (``clipforge-server.exe`` on Windows).
"""

import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = globals().get("SPECDIR", os.getcwd())

block_cipher = None

a = Analysis(
    [os.path.join(ROOT, "apps", "api", "clipforge_api", "__main__.py")],
    pathex=[
        os.path.join(ROOT, "apps", "api"),
        os.path.join(ROOT, "src"),
    ],
    binaries=[],
    datas=[],
    hiddenimports=[
        # uvicorn picks its loop / http / websocket implementations at
        # runtime, so static analysis cannot see them.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # SQLAlchemy resolves the sqlite dialect dynamically.
        "sqlalchemy.dialects.sqlite.pysqlite",
        # FastAPI form parsing imports python-multipart lazily.
        "multipart",
        "multipart.multipart",
        "multipart.exceptions",
        # pydantic-settings (optional .env loader) and static files.
        "pydantic_settings",
        "starlette.staticfiles",
    ]
    + collect_submodules("clipforge"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="clipforge-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # console app: prints its URLs and logs to stdout
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # TODO: add a ClipForge .ico for the Windows build
)
