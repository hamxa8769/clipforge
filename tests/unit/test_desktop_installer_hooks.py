"""Regression tests for the v0.8.0 stuck-install bug (NSIS kill-before-install).

The user's v0.8.0 install failed with NSIS "Error opening file for
writing: ...\\clipforge-server.exe" because clipforge-server.exe was
still running (the app hides to the tray on close, so the server keeps
running and locks its own exe). These tests prove the installer-side
safeguard exists — they read the NSIS hook file and the Tauri bundle
config and assert the kill-before-install behaviour is wired up:

* ``tauri.conf.json`` points ``bundle.windows.nsis.installerHooks`` at
  the hook file (the Tauri v2 mechanism; the bundler ``!include``s it
  into the generated installer.nsi and expands
  ``NSIS_HOOK_PREINSTALL`` before the first ``File`` instruction).
* The hook force-kills the sidecar AND the app exe (both spellings),
  waits/retries until they are gone, ignores "not running", and aborts
  with a clear user-facing message instead of the raw NSIS error if a
  file is still locked.
* ``updater.rs`` exposes the matching install-ordering guard
  (``INSTALLER_GUARD_PROCESSES`` / ``wait_for_process_gone``) so the
  in-app updater can stop the server and verify it is gone *before*
  launching the installer.

No Windows build runs here — a real NSIS build needs a Windows runner
(the release workflow builds it with ``cargo tauri build``). These
tests are the Linux-side proof that the hook is present, referenced,
and shaped correctly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAURI_DIR = ROOT / "apps" / "desktop" / "src-tauri"
CONF_PATH = TAURI_DIR / "tauri.conf.json"
UPDATER_RS = TAURI_DIR / "src" / "updater.rs"

# The processes the hook must stop before the installer writes files.
# clipforge-desktop.exe is the app exe as built (the Cargo binary name);
# ClipForge.exe is the product-name spelling, killed as a safety net.
GUARD_PROCESSES = ("clipforge-server.exe", "clipforge-desktop.exe", "ClipForge.exe")


def _hook_path() -> Path:
    conf = json.loads(CONF_PATH.read_text(encoding="utf-8"))
    rel = conf["bundle"]["windows"]["nsis"]["installerHooks"]
    path = (TAURI_DIR / rel).resolve()
    assert path.is_file(), f"installerHooks points at a missing file: {rel}"
    return path


def _hook_text() -> str:
    return _hook_path().read_text(encoding="utf-8")


def test_tauri_conf_references_installer_hooks():
    conf = json.loads(CONF_PATH.read_text(encoding="utf-8"))
    nsis = conf["bundle"]["windows"]["nsis"]
    assert "installerHooks" in nsis, (
        "tauri.conf.json must set bundle.windows.nsis.installerHooks so the "
        "kill-before-install hook is picked up by the Windows NSIS build"
    )
    assert nsis["installerHooks"].endswith(".nsh")
    assert _hook_path().is_file()


def test_hook_defines_preinstall_and_preuninstall():
    text = _hook_text()
    assert "!macro NSIS_HOOK_PREINSTALL" in text
    assert "!macro NSIS_HOOK_PREUNINSTALL" in text
    # The hook file must only define macros (it is !included at the top
    # of the generated installer.nsi); it must not declare a section or
    # function of its own, or run code on include.
    assert not re.search(r"^Section\s", text, re.MULTILINE)
    assert not re.search(r"^Function\s", text, re.MULTILINE)


def test_hook_kills_server_and_app_exes():
    text = _hook_text()
    # The kill itself lives in the shared CLIPFORGE_STOP_PROCESS macro
    # (parameterised by image name); each guarded process must be passed
    # to it from NSIS_HOOK_PREINSTALL.
    assert "taskkill /F /IM ${IMAGE_NAME} /T" in text
    pre = text.split("!macro NSIS_HOOK_PREINSTALL", 1)[1].split("!macroend", 1)[0]
    for name in GUARD_PROCESSES:
        assert f'CLIPFORGE_STOP_PROCESS "{name}"' in pre, (
            f"hook must force-kill {name} before install; "
            "clipforge-server.exe is the file that locked the v0.8.0 install"
        )
    # The sidecar is killed before the app exe: it is the process holding
    # clipforge-server.exe open.
    assert pre.index("clipforge-server.exe") < pre.index("clipforge-desktop.exe")


def test_hook_waits_and_retries_until_processes_are_gone():
    text = _hook_text()
    # Poll tasklist until the image disappears, sleeping between retries.
    assert "tasklist" in text
    assert "Sleep 500" in text
    # Bounded retry loop (~10 s), not a single blind taskkill.
    assert re.search(r"IntCmp \$R0 20", text), "hook must cap the wait loop"


def test_hook_aborts_cleanly_with_a_clear_message_when_still_locked():
    text = _hook_text()
    # Abort runs before any File instruction, so the install is cancelled
    # cleanly instead of failing halfway through with the raw NSIS error.
    assert "Abort" in text
    assert "still running" in text
    assert "Task Manager" in text
    # The raw NSIS dialog text must not be what the user is left with.
    assert "Error opening file for writing" not in text


def test_updater_exposes_install_ordering_guard():
    src = UPDATER_RS.read_text(encoding="utf-8")
    assert "INSTALLER_GUARD_PROCESSES" in src
    assert "wait_for_process_gone" in src
    for name in GUARD_PROCESSES:
        assert f'"{name}"' in src, f"updater guard must list {name}"
    # The launch contract must be documented at the call site: stop the
    # server and verify it is gone BEFORE launching the installer.
    assert "wait_for_process_gone" in src
    assert "stop_sidecar_if_running" in src
    assert "NSIS_HOOK_PREINSTALL" in src
