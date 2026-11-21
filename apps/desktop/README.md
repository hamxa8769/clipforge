# ClipForge desktop app (Tauri v2)

One double-click app, no terminal window. The Tauri shell launches the
`clipforge-server` sidecar (the Python/FastAPI server, already a PyInstaller
one-file exe), waits for `GET /health`, then shows the web UI — which the
server itself serves from its static export — in the main window.

**Architecture**

```
ClipForge (Tauri window, Rust)
  └─ sidecar: clipforge-server[.exe] --host 127.0.0.1 --port <port>
       ├─ FastAPI on http://127.0.0.1:<port>/  (/health, /docs, /jobs, …)
       └─ static web UI served at /  (from <base>/web/out, or the
           CLIPFORGE_WEB_OUT path the shell passes — see below)
```

The Rust shell never re-implements the Python engine: it only manages the
sidecar's lifecycle, port, data dir, logs, FFmpeg check, tray and settings.

**What the shell does**

- **Sidecar lifecycle** (`src-tauri/src/sidecar.rs`): picks a free loopback
  port (prefers 8000), resolves the sidecar (see below), spawns it hidden
  (Windows: `CREATE_NO_WINDOW`; the shell itself is built with
  `windows_subsystem = "windows"`, so no console window ever appears),
  with `CLIPFORGE_DATA_DIR` + `DATABASE_URL` pointed at the app-data dir.
  Polls `/health` every 250 ms for up to 30 s, then loads the main window.
  Stdout/stderr go to `<data>/logs/clipforge-server.log` (rotated past 5 MiB,
  one backup kept); the last lines are visible from the splash page and the
  status window. On quit the sidecar gets SIGTERM (Unix) / terminate
  (Windows), a 5 s grace period, then a force-kill — it is never orphaned.
  That covers forced kills too: a `ctrlc` handler (with the `termination`
  feature — without it, `ctrlc` only catches SIGINT, not SIGTERM) stops the
  sidecar on SIGINT/SIGTERM/SIGHUP, in addition to the clean-exit hook.
  Verified under Xvfb: SIGTERM to the app stops the sidecar within ~1 s
  and leaves no `clipforge-server` process behind.
- **Tray**: Show / Server status & settings / Open data folder / Quit.
  Closing the main window hides to tray (first time, the status window opens
  once with a hint). Quit from the tray stops the sidecar and exits.
- **FFmpeg check** (`src-tauri/src/ffmpeg.rs`): runs `ffmpeg -version`
  (PATH, plus common Windows install locations). If missing, the splash page
  shows a banner with the download link
  (https://ffmpeg.org/download.html) and a "Continue without clipping
  (review / SEO only)" path. The app never blocks on a missing FFmpeg and
  never fails silently.
- **Settings** (`src-tauri/src/settings.rs`, shell window): data-folder
  location, server port (auto / manual), API key **names** only. Key *values*
  are never written by the shell — put them in a `.env` file next to the
  `clipforge-server` binary (the server loads it on start) or in the
  environment. Data-folder / port changes apply after a restart.
- **Shell frontend** (`src/`): plain HTML/CSS/JS, no build step, no npm
  dependencies. `index.html` is the loading/splash page; `shell.html` is the
  status / logs / settings window. Note the honest trade-off: once the main
  window navigates to the server URL, the Tauri JS bridge is no longer
  available inside the web UI — shell features (tray, status window,
  settings) live in Rust and in the separate shell window.

**Sidecar resolution order** (see `sidecar.rs`)

1. `CLIPFORGE_SIDECAR_PATH` env override.
2. Tauri `externalBin` `clipforge-server` —
   `<resources>/binaries/clipforge-server-<target-triple>[.exe]`
   (declared in `tauri.conf.json`; the release packager stages the
   per-platform server binary there).
3. A plain `clipforge-server[.exe]` next to the app resources / exe.
4. **Dev only:** `<repo>/dist/clipforge-server[.exe]` (PyInstaller build).
5. **Dev only:** `python -m clipforge_api` from the repo root
   (needs the API deps installed; clearly labelled "dev python" in the UI).

**Prerequisites**

| Need | Notes |
| --- | --- |
| Rust stable | `rustup` toolchain; `cargo` on PATH (`~/.cargo/bin`). |
| Linux build deps | `libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev libssl-dev patchelf build-essential` (Debian/Ubuntu). |
| Node.js 20+ | Only for the web static export (Worker A), not for the shell itself. |
| Python + PyInstaller | Only to build the `clipforge-server` sidecar (`scripts/build_exe.py`). |
| FFmpeg | Runtime, for clipping. The app runs without it (review/SEO only). |

This VM quirk (recorded 2026-11-20): the stock `libwebkit2gtk-4.1-dev`
install failed with "held broken packages" because the VM's `libzstd1`
was a custom `1.5.5+dfsg2-2build1.1+hatch1` build while `libzstd-dev`
requires exactly `1.5.5+dfsg2-2build1.1`. Fixed by downgrading `libzstd1`
to the stock Ubuntu build (`apt-get install --allow-downgrades
libzstd1=1.5.5+dfsg2-2build1.1`); the old `.so` was backed up to
`/tmp/zstd-backup/` first and `apt-get check` is clean afterwards.
A second quirk: a runtime restart rolled back **all** of those system
(`apt`) installs while `~` survived — the dev packages had to be
reinstalled afterwards. The `.deb` cache in `/var/cache/apt/archives/`
survives restarts, so the reinstall is fast and mostly offline.
Also note: the first `cargo install tauri-cli` failed only because
`/tmp` is a 512 MB tmpfs that filled up; retry with
`CARGO_TARGET_DIR=$HOME/.cargo/tmp-install-target`.

**Dev flow**

```sh
# 1. Build the server exe + static web export:
python3 scripts/build_exe.py --with-web-static   # dist/ClipForge-v<ver>-Linux/
# 2. Stage them for the Tauri sidecar (externalBin) layout:
cp dist/clipforge-server src-tauri/binaries/clipforge-server-x86_64-unknown-linux-gnu
cp -r dist/ClipForge-v<ver>-Linux/web/out src-tauri/binaries/web/out
#    (binaries/ is gitignored; staged files are local build copies, not committed)
# 3. Run:
cargo tauri dev
#    The main window shows the splash page, starts the sidecar from
#    dist/ (or the staged binary), waits for /health, then loads the web UI.
#    Without a GUI (headless VM), use `cargo test` / `cargo check` instead.
```

**Build flow**

```sh
# Stage the per-platform server binary first (name MUST match the triple):
#   Linux:   src-tauri/binaries/clipforge-server-x86_64-unknown-linux-gnu
#   Windows: src-tauri/binaries/clipforge-server-x86_64-pc-windows-msvc.exe
#   macOS:   src-tauri/binaries/clipforge-server-aarch64-apple-darwin
# Also stage the static web export at src-tauri/binaries/web/out
# (from `scripts/build_exe.py --with-web-static`); tauri.conf.json
# `bundle.resources: ["binaries/web/**/*"]` bundles it under
# <resources>/binaries/web/out. The shell passes that path to the server
# as CLIPFORGE_WEB_OUT (see below) — do not skip this step, or the app
# shows the API-only status page instead of the dashboard.
cargo tauri build
# Output: src-tauri/target/release/bundle/ (deb/appimage on Linux,
# nsis/msi on Windows).
```

**Where the sidecar + web UI land in a bundle (verified 2026-11-20)**

Linux `.deb`/`.AppImage` split them apart: the sidecar is installed to
`/usr/bin/clipforge-server` (Tauri puts `externalBin` binaries on PATH),
while the web export lands at
`<resources>/binaries/web/out` (`/usr/lib/ClipForge/binaries/web/out`
in the .deb). The server's default "web/out next to the exe" therefore
does NOT hold there — so the shell sets `CLIPFORGE_WEB_OUT` to the
bundled export (`sidecar::bundled_web_out()`, only when
`binaries/web/out/index.html` exists under the resource dir) and the
server honours it (`apps/api/clipforge_api/__main__.py`).

Windows MSI (verified in CI, run 44813285416): the same split — the
sidecar is installed as `<install>\clipforge-server.exe` (the
`-<target-triple>` suffix is stripped when bundling) while the export
is at `<install>\binaries\web\out\index.html`. So `CLIPFORGE_WEB_OUT`
is needed on Windows too; the release workflow proves both files are
inside the MSI (admin-install extraction) before publishing.

**Windows installer: kill-before-install (v0.8.1)**

The app hides to the tray on close, so `clipforge-server.exe` can still
be running when the user runs a new installer — and a running exe is
locked by Windows. The v0.8.0 install failed exactly this way (NSIS
"Error opening file for writing: ...\clipforge-server.exe"). Two layers
guard against it now:

1. **NSIS hook (installer-side, always runs).**
   `src-tauri/installer-hooks.nsh`, wired in via
   `tauri.conf.json > bundle.windows.nsis.installerHooks` (Tauri v2
   mechanism; the bundler `!include`s it and expands
   `NSIS_HOOK_PREINSTALL` before the first `File` instruction).
   Before any file is written it force-kills `clipforge-server.exe`,
   `clipforge-desktop.exe` and `ClipForge.exe` with
   `taskkill /F /IM <name> /T`, waits up to ~10 s for each to disappear
   from `tasklist`, and if one is still locked aborts with a clear
   message ("close ClipForge or end <name> in Task Manager") instead of
   the raw NSIS error. The same hook runs as `NSIS_HOOK_PREUNINSTALL`
   before uninstall deletes files. Note Tauri's own template only
   guards the *main* binary (`CheckIfAppIsRunning` on
   `${MAINBINARYNAME}.exe`) — it never checks the sidecar, which is
   the gap this hook closes.
2. **Updater ordering (app-side, makes updates smooth).** The in-app
   updater must stop the server and verify it is gone *before*
   launching the installer: `stop_sidecar_if_running` →
   `updater::wait_for_process_gone("clipforge-server.exe", …)` (and the
   app exe, see `INSTALLER_GUARD_PROCESSES` in `src/updater.rs`) →
   `updater::launch_installer` → `app.exit(0)` at once. Launching
   first and quitting afterwards is the bug — the hook is only the
   backstop. Regression tests:
   `tests/unit/test_desktop_installer_hooks.py`.

**Headless smoke test (installed .deb, Xvfb — verified 2026-11-20)**

```sh
sudo dpkg -i src-tauri/target/release/bundle/deb/ClipForge_0.2.1_amd64.deb
Xvfb :99 -screen 0 1280x800x24 &
XDG_DATA_HOME=/tmp/cf-appdata-v02 DISPLAY=:99 /usr/bin/clipforge-desktop &
# wait ~25 s, then: curl 127.0.0.1:8000/ must return the dashboard HTML
# (contains "Dashboard", no "API-only"), /health reports version 0.3.0,
# and the server log (<data>/logs/clipforge-server.log) shows the
# WebView fetching / + the JS chunks + /jobs/view/index.txt prefetch.
# SIGTERM the app: it exits in ~1 s and the sidecar is gone (no orphan).
```

A Windows build cannot be produced from Linux — build the `.exe` on
Windows (or in CI on `windows-latest`).

**Tests**

```sh
cargo test    # port-picking, sidecar resolution, health-wait timeout,
              # log tailing, FFmpeg version parsing, settings round-trip
cargo check   # typecheck without linking the WebKit shell
```

**Where logs / data live**

| | Windows | Linux |
| --- | --- | --- |
| App data (settings `clipforge-desktop.json`, DB `clipforge.db`) | `%APPDATA%\com.clipforge.app\` | `~/.local/share/com.clipforge.app/` |
| Server log | `<data>\logs\clipforge-server.log` | `<data>/logs/clipforge-server.log` |
| Settings file | `<data>\clipforge-desktop.json` | `<data>/clipforge-desktop.json` |

The data folder can be overridden in the settings window (applies on restart).
