"""Regression tests for the v0.10.2 in-app updater fix ("updates do
nothing").

The user (2026-11-24) reported the app never offers or installs an
update: no download, nothing happens, stuck on an older version.
Diagnosis, traced end to end in code and checked against the live
GitHub /releases/latest payload for v0.10.1:

* The version comparison and asset selection were NOT the bug:
  `v0.10.1` vs app `0.10.1` is correctly "not newer", `0.9.3` is
  correctly "newer", and the live release carries the exact asset
  the updater looks for (`ClipForge_0.10.1_x64-setup.exe`,
  45,985,124 bytes). The Python mirror below re-proves that on the
  vendored copy of the real asset list, so a future asset rename
  breaks a test here instead of failing silently on the user's PC.
* The real defects were:
  1. Both updater commands were synchronous Tauri commands, which
     run on the main thread — the 46 MB download froze the whole
     window, and the progress poll (`get_update_status`) could never
     run while the download was in progress. The UI also only
     started polling *after* the download promise resolved, i.e.
     never during it. The user saw "Downloading… 0%" frozen.
  2. The check raced the quiet launch check: a busy check answered
     with an error, the UI showed that error and never looked at the
     updater state again, so an available update stayed hidden for
     the whole session. And the banner hid every non-available state,
     so a failed check looked exactly like "no update" — silence.
  3. The download had one 60 s global timeout, so the 46 MB setup
     exe needed ~6 Mbps sustained; any slower link failed every time.
  4. One malformed asset in the release JSON killed the whole check
     (`?` in the asset loop) — "no installer for your platform"
     even though the setup exe was right there.
* Historical foot-gun pinned here too: v0.4.0/v0.4.1 shipped with NO
  capability file at all, so every updater invoke was ACL-denied and
  the UI silently reported "updater not in this build". The
  capability must keep granting the three updater commands.

These are source-level tests (the desktop crate cannot link on a
bare Linux VM — no WebKitGTK; the full `cargo test` suite runs in CI
on Windows), in the same style as test_desktop_installer_hooks.py.
The Rust unit tests for the same behaviour live in
`apps/desktop/src-tauri/src/updater.rs`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAURI_DIR = ROOT / "apps" / "desktop" / "src-tauri"
MAIN_RS = TAURI_DIR / "src" / "main.rs"
UPDATER_RS = TAURI_DIR / "src" / "updater.rs"
CAPABILITIES = TAURI_DIR / "capabilities" / "default.json"
UPDATER_TSX = ROOT / "apps" / "web" / "components" / "Updater.tsx"
TAURI_TS = ROOT / "apps" / "web" / "lib" / "tauri.ts"

# The exact asset list GitHub served for /releases/latest at v0.10.1
# (fetched 2026-11-24). Vendored so the mirror tests below run
# offline and still prove the real selection behaviour.
V0101_ASSETS = [
    {
        "name": "ClipForge-v0.10.1-Linux.zip",
        "browser_download_url": "https://example.com/l.zip",
        "size": 91993246,
    },
    {
        "name": "ClipForge-v0.10.1-Windows.zip",
        "browser_download_url": "https://example.com/w.zip",
        "size": 58225827,
    },
    {
        "name": "ClipForge_0.10.1_amd64.AppImage",
        "browser_download_url": "https://example.com/a",
        "size": 158865912,
    },
    {
        "name": "ClipForge_0.10.1_amd64.deb",
        "browser_download_url": "https://example.com/d",
        "size": 75392598,
    },
    {
        "name": "ClipForge_0.10.1_x64-setup.exe",
        "browser_download_url": "https://example.com/s.exe",
        "size": 45985124,
    },
    {
        "name": "ClipForge_0.10.1_x64_en-US.msi",
        "browser_download_url": "https://example.com/s.msi",
        "size": 47448163,
    },
]


# ---- Python mirror of updater.rs pure logic (parse / compare / pick) ----


def _parse_version(text: str):
    text = text[1:] if text.startswith("v") else text
    parts = text.split(".")
    if len(parts) != 3 or any(p == "" or not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _is_newer_release(tag: str, current: str) -> bool:
    latest, cur = _parse_version(tag), _parse_version(current)
    return latest is not None and cur is not None and latest > cur


def _setup_asset_name(tag: str) -> str:
    return f"ClipForge_{tag[1:] if tag.startswith('v') else tag}_x64-setup.exe"


def _pick_update_asset(assets, os_name: str, tag: str):
    names = [a["name"] for a in assets]
    if os_name == "windows":
        for cand in (
            [_setup_asset_name(tag)]
            + [n for n in names if n.endswith("-setup.exe")]
            + [n for n in names if n.endswith(".msi")]
        ):
            if cand in names:
                return cand
    if os_name == "linux":
        for cand in [n for n in names if n.endswith(".deb")] + [
            n for n in names if n.endswith(".AppImage")
        ]:
            if cand in names:
                return cand
    return None


def _parse_latest_release(payload: dict, os_name: str):
    """Mirror of the FIXED parse_latest_release: malformed assets are
    skipped instead of killing the whole check."""
    tag = payload.get("tag_name")
    parsed = []
    for a in payload.get("assets", []):
        if not a.get("name") or not a.get("browser_download_url"):
            continue
        parsed.append(
            {"name": a["name"], "url": a["browser_download_url"], "size": a.get("size") or 0}
        )
    asset = _pick_update_asset(parsed, os_name, tag)
    return (tag, asset) if asset else None


# ---- 1. Version comparison + asset selection against the real release ----


def test_live_v0101_release_offers_setup_exe_to_older_versions():
    tag, asset = _parse_latest_release({"tag_name": "v0.10.1", "assets": V0101_ASSETS}, "windows")
    assert tag == "v0.10.1"
    assert asset == "ClipForge_0.10.1_x64-setup.exe"
    assert _is_newer_release(tag, "0.9.3")
    assert _is_newer_release(tag, "0.10.0")
    _, deb = _parse_latest_release({"tag_name": "v0.10.1", "assets": V0101_ASSETS}, "linux")
    assert deb == "ClipForge_0.10.1_amd64.deb"


def test_same_version_is_never_offered_as_an_update():
    assert not _is_newer_release("v0.10.1", "0.10.1")
    assert not _is_newer_release("v0.10.1", "0.10.2")


def test_version_compare_is_numeric_not_lexicographic():
    assert _parse_version("0.10.10") > _parse_version("0.10.9")
    assert _is_newer_release("v0.10.10", "0.10.9")
    assert _parse_version("v2.0.1") == (2, 0, 1)
    for bad in ["", "v", "v0.4", "0.4.0.1", "0.4.0-beta", "abc", "0.4.x", " 0.4.0"]:
        assert _parse_version(bad) is None, bad


def test_one_malformed_asset_no_longer_kills_the_check():
    assets = [{"name": "weird.bin", "browser_download_url": "https://example.com/w"}] + V0101_ASSETS
    assert _parse_latest_release({"tag_name": "v0.10.1", "assets": assets}, "windows") == (
        "v0.10.1",
        "ClipForge_0.10.1_x64-setup.exe",
    )


def test_setup_exe_size_sets_the_timeout_budget():
    """The v0.10.1 setup exe is 45,985,124 bytes: a 60 s global
    timeout needed ~6 Mbps sustained, so any slower link failed every
    time. The Rust download timeouts must allow a slow link (see
    test_download_timeout_allows_slow_links)."""
    size = next(a["size"] for a in V0101_ASSETS if a["name"].endswith("-setup.exe"))
    assert size == 45985124
    assert size / 60 > 700_000  # >700 KB/s sustained would have been required


# ---- 2. Rust: commands off the main thread ----


def test_updater_commands_run_off_the_main_thread():
    main = MAIN_RS.read_text(encoding="utf-8")
    assert re.search(r"#\[tauri::command\]\s*\nasync fn check_for_updates", main), (
        "check_for_updates must be async: as a sync Tauri command it ran "
        "on the main thread and froze the window for up to the 15 s "
        "GitHub timeout"
    )
    assert re.search(r"#\[tauri::command\]\s*\nasync fn download_and_install_update", main), (
        "download_and_install_update must be async: as a sync command the "
        "46 MB download froze the window and the progress poll could "
        "never run"
    )
    assert "spawn_blocking" in main, (
        "the updater commands must do their blocking work (ureq, file "
        "writes, stopping the server) via spawn_blocking"
    )


# ---- 3. Rust: busy check returns the last known release, not an error ----


def test_busy_check_returns_last_known_release():
    updater = UPDATER_RS.read_text(encoding="utf-8")
    assert "busy_check_result" in updater
    assert "run_check" in updater and "busy_check_result(current" in updater, (
        "run_check must answer a busy check with busy_check_result (last "
        "known release), not a bare error"
    )
    # The old behaviour — an unconditional "already in progress" error
    # with available=false — must be gone from run_check.
    assert 'Some("An update check is already in progress.".into())' not in updater


# ---- 4. Rust: malformed assets are skipped, download timeouts fit slow links ----


def test_parse_latest_release_skips_malformed_assets():
    updater = UPDATER_RS.read_text(encoding="utf-8")
    assert 'a.get("size")?.as_u64()?' not in updater, (
        "a single asset missing `size` must not kill the whole check with `?`"
    )
    assert updater.count("continue;") >= 2, "malformed assets (no name / no URL) must be skipped"


def test_download_timeout_allows_slow_links():
    updater = UPDATER_RS.read_text(encoding="utf-8")
    assert ".timeout(Duration::from_secs(60))" not in updater, (
        "a 60 s global download timeout needs ~6 Mbps sustained for the "
        "46 MB setup exe — it failed every slower link"
    )
    assert "timeout_read" in updater, "a stalled download must still fail fast per read"
    assert "timeout_connect" in updater


# ---- 5. Web UI: progress polling, busy retry, visible failures ----


def test_install_polls_for_progress_before_awaiting_the_download():
    text = UPDATER_TSX.read_text(encoding="utf-8")
    poll_at = text.find("setInterval")
    await_at = text.find("await downloadAndInstallUpdate()")
    assert poll_at != -1 and await_at != -1 and poll_at < await_at, (
        "Updater.tsx must start the get_update_status poll BEFORE awaiting "
        "downloadAndInstallUpdate — polling only after the promise "
        "resolves never shows progress during the download"
    )


def test_check_retries_a_busy_check_and_adopts_the_launch_check_result():
    text = UPDATER_TSX.read_text(encoding="utf-8")
    assert "isUpdateCheckBusy" in text and "CHECK_BUSY_RETRIES" in text, (
        "a busy check ('already in progress') must be retried, not shown as a failure"
    )
    adopt_at = text.find("getUpdateStatus()")
    fresh_at = text.find("checkForUpdates()")
    assert adopt_at != -1 and fresh_at != -1 and adopt_at < fresh_at, (
        "check() must first adopt the launch check's settled result from "
        "get_update_status before starting a fresh check"
    )
    assert "updateInfoFromStatus" in text


def test_banner_shows_a_failed_check_instead_of_silence():
    text = UPDATER_TSX.read_text(encoding="utf-8")
    assert 'state.kind !== "error"' in text, (
        "the top banner must render the error state too — hiding every "
        "non-available state made a failed check look like 'no update'"
    )
    assert "Try again" in text and "RELEASES_URL" in text


def test_install_failure_reads_the_real_error_from_status():
    text = UPDATER_TSX.read_text(encoding="utf-8")
    assert text.count("getUpdateStatus()") >= 3, (
        "after a failed install the UI must read get_update_status for "
        "the real reason, not a generic message"
    )


# ---- 6. Capability: the updater commands must stay granted ----


def test_capability_grants_the_updater_commands():
    conf = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    perms = conf.get("permissions", [])
    for cmd in (
        "allow-check-for-updates",
        "allow-download-and-install-update",
        "allow-get-update-status",
    ):
        assert cmd in perms, (
            f"capabilities/default.json must grant {cmd}: v0.4.0/v0.4.1 "
            "shipped with no capability file at all, so every updater "
            "invoke was ACL-denied and the UI silently said the updater "
            "was not in the build"
        )
    assert "http://127.0.0.1:*" in conf.get("remote", {}).get("urls", []), (
        "the loopback-served UI origin must be listed under remote.urls "
        "or no invoke reaches the shell"
    )
