//! In-app updater for the ClipForge desktop app (adapted from the Muuk
//! updater pattern — same shape, written fresh in Rust for this repo).
//!
//! * Source is fixed: the public repo `hamxa8769/clipforge`, no auth, no
//!   user-entered URL. The check is a plain GET of
//!   `https://api.github.com/repos/hamxa8769/clipforge/releases/latest`.
//! * Never auto-downloads. The check runs when the user presses the
//!   button (`check_for_updates`) and once quietly on launch (it only
//!   sets the state — never downloads).
//! * A failed check is NOT silent (v0.10.2): the error is stored in
//!   state and the web UI shows it in Settings → Updates and in the
//!   top banner, with a retry button and a link to the releases page.
//!   Before v0.10.2 the banner hid every non-available state, so a
//!   failed check looked exactly like "no update" and the user saw
//!   nothing at all.
//! * Windows: the update asset is the NSIS setup exe
//!   (`ClipForge_<tag>_x64-setup.exe`). It installs over the app folder;
//!   the user's data dir lives in the OS app-data dir
//!   (`%APPDATA%/com.clipforge.app` on Windows, `~/.local/share` /
//!   `~/.config` on Linux), and the installer never touches it — so
//!   `.env`, API keys, the database and logs survive the update.
//! * Linux: the `.deb` / `.AppImage` is downloaded and its path is
//!   reported; the user installs it manually. The updater never pretends
//!   to install on Linux.
//!
//! Threading (v0.10.2): the two updater commands are `async` and do
//! their blocking network/file work on a worker thread
//! (`spawn_blocking` in `main.rs`). Before v0.10.2 they were plain
//! synchronous Tauri commands, which run on the main thread — so the
//! 46 MB download froze the whole window and the progress poll
//! (`get_update_status`) could never run while the download was in
//! progress: the user saw "Downloading… 0%" frozen, i.e. nothing
//! happening. The version parsing/comparison and the release-JSON
//! parsing are pure and unit-tested below.

use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

pub const RELEASES_LATEST_URL: &str =
    "https://api.github.com/repos/hamxa8769/clipforge/releases/latest";
pub const USER_AGENT: &str = "ClipForge-Updater";

/// The version this binary was built as (from `Cargo.toml`).
pub fn current_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

// ---- pure, platform-independent logic (unit-tested) ----

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
}

/// `"v0.4.0"` or `"0.4.0"` -> `Version { 0, 4, 0 }`. Anything else
/// (`"v0.4"`, `"abc"`, `""`, `"0.4.0-beta"`, `"0.4.0.1"`) is malformed
/// and returns `None`.
pub fn parse_version(text: &str) -> Option<Version> {
    let text = text.strip_prefix('v').unwrap_or(text);
    let mut parts = text.split('.');
    let major = parts.next()?.parse::<u64>().ok()?;
    let minor = parts.next()?.parse::<u64>().ok()?;
    let patch = parts.next()?.parse::<u64>().ok()?;
    if parts.next().is_some() {
        return None;
    }
    // Reject empty pieces and non-numeric junk (`parse` already rejects
    // those, but also reject a leading `+`/space that `parse` tolerates
    // in no case — `u64::from_str` is strict, so this is belt-and-braces).
    if text
        .split('.')
        .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return None;
    }
    Some(Version {
        major,
        minor,
        patch,
    })
}

/// Numeric comparison, so `0.4.10 > 0.4.9`.
pub fn compare_versions(a: &Version, b: &Version) -> Ordering {
    a.cmp(b)
}

/// True when the release tag names a version newer than the running app.
/// A malformed tag or current version means "no update", never an error.
pub fn is_newer_release(latest_tag: &str, current: &str) -> bool {
    match (parse_version(latest_tag), parse_version(current)) {
        (Some(latest), Some(cur)) => compare_versions(&latest, &cur) == Ordering::Greater,
        _ => false,
    }
}

/// The Windows setup asset name for a tag: `ClipForge_0.4.0_x64-setup.exe`.
pub fn setup_asset_name(tag: &str) -> String {
    format!(
        "ClipForge_{}_x64-setup.exe",
        tag.strip_prefix('v').unwrap_or(tag)
    )
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReleaseAsset {
    pub name: String,
    pub download_url: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReleaseInfo {
    pub tag: String,
    pub asset: ReleaseAsset,
}

/// Pick the update asset for this platform from a release's asset list.
///
/// * Windows: prefer the exact `ClipForge_<tag>_x64-setup.exe`, then any
///   `*-setup.exe`, then a `.msi`.
/// * Linux: prefer a `.deb`, then a `.AppImage`.
/// * macOS: prefer a `.dmg` (not built by CI yet — returns `None`).
/// Returns `None` when the release has no asset for the platform.
pub fn pick_update_asset(assets: &[ReleaseAsset], os: &str, tag: &str) -> Option<ReleaseAsset> {
    match os {
        "windows" => {
            let exact = setup_asset_name(tag);
            assets
                .iter()
                .find(|a| a.name == exact)
                .or_else(|| assets.iter().find(|a| a.name.ends_with("-setup.exe")))
                .or_else(|| assets.iter().find(|a| a.name.ends_with(".msi")))
                .cloned()
        }
        "linux" => assets
            .iter()
            .find(|a| a.name.ends_with(".deb"))
            .or_else(|| assets.iter().find(|a| a.name.ends_with(".AppImage")))
            .cloned(),
        "macos" => assets.iter().find(|a| a.name.ends_with(".dmg")).cloned(),
        _ => None,
    }
}

/// Parse a GitHub `/releases/latest` response. `None` when the JSON is
/// malformed or holds no update asset for this platform.
///
/// One malformed asset must not kill the whole check (v0.10.2): an
/// asset missing its name/URL is skipped, and a missing `size` is
/// treated as unknown (0) instead of failing the parse — before
/// v0.10.2 a single odd asset made `parse_latest_release` return
/// `None` and the check failed with "no installer for your platform"
/// even though the setup exe was right there.
pub fn parse_latest_release(json_text: &str, os: &str) -> Option<ReleaseInfo> {
    let value: serde_json::Value = serde_json::from_str(json_text).ok()?;
    let tag = value.get("tag_name")?.as_str()?.to_string();
    let assets = value.get("assets")?.as_array()?;
    let mut parsed: Vec<ReleaseAsset> = Vec::new();
    for a in assets {
        let Some(name) = a.get("name").and_then(|v| v.as_str()) else {
            continue;
        };
        let Some(download_url) = a.get("browser_download_url").and_then(|v| v.as_str()) else {
            continue;
        };
        let size = a.get("size").and_then(|v| v.as_u64()).unwrap_or(0);
        parsed.push(ReleaseAsset {
            name: name.to_string(),
            download_url: download_url.to_string(),
            size,
        });
    }
    let asset = pick_update_asset(&parsed, os, &tag)?;
    Some(ReleaseInfo { tag, asset })
}

// ---- state machine ----

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UpdatePhase {
    /// Up to date, never checked, or the last check failed quietly.
    Idle,
    /// Asking GitHub for the latest release.
    Checking,
    /// A newer release exists — the update button is shown.
    Available,
    /// The installer is downloading (`progress` is 0..1).
    Downloading,
    /// Downloaded and verified; on Windows the installer was launched,
    /// on Linux the file waits for a manual install.
    Ready,
    /// Something went wrong; `error` says what (shown, not alarming).
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateStatus {
    /// One of `idle | checking | available | downloading | ready | failed`.
    pub state: UpdatePhase,
    /// Download progress 0..1.
    pub progress: f64,
    /// Same progress as 0..100 (the web UI reads this one).
    pub progress_pct: f64,
    pub error: Option<String>,
    pub current_version: String,
    pub latest_tag: Option<String>,
    /// Latest version without the leading `v` (the web UI reads this one).
    pub version: Option<String>,
    pub asset_name: Option<String>,
    pub asset_size: Option<u64>,
    /// Same as `asset_size` (the web UI reads this one).
    pub size_bytes: Option<u64>,
    pub download_url: Option<String>,
    pub downloaded_path: Option<String>,
    /// True on Linux/macOS: the file was downloaded, install it manually.
    pub manual_install: bool,
}

impl Default for UpdateStatus {
    fn default() -> Self {
        UpdateStatus {
            state: UpdatePhase::Idle,
            progress: 0.0,
            progress_pct: 0.0,
            error: None,
            current_version: current_version(),
            latest_tag: None,
            version: None,
            asset_name: None,
            asset_size: None,
            size_bytes: None,
            download_url: None,
            downloaded_path: None,
            manual_install: false,
        }
    }
}

/// Mutable updater state, held in Tauri managed state (`AppState.update`).
#[derive(Debug, Default)]
pub struct UpdaterState {
    pub status: UpdateStatus,
    pub release: Option<ReleaseInfo>,
}

pub fn snapshot(state: &Mutex<UpdaterState>) -> UpdateStatus {
    state.lock().map(|g| g.status.clone()).unwrap_or_default()
}

pub fn set_phase(state: &Mutex<UpdaterState>, phase: UpdatePhase, error: Option<String>) {
    if let Ok(mut g) = state.lock() {
        g.status.state = phase;
        if phase != UpdatePhase::Downloading {
            g.status.progress = if phase == UpdatePhase::Ready {
                1.0
            } else {
                0.0
            };
            g.status.progress_pct = g.status.progress * 100.0;
        }
        if error.is_some() {
            g.status.error = error;
        }
    }
}

pub fn set_progress(state: &Mutex<UpdaterState>, progress: f64) {
    if let Ok(mut g) = state.lock() {
        g.status.progress = progress.clamp(0.0, 1.0);
        g.status.progress_pct = g.status.progress * 100.0;
    }
}

pub fn set_downloaded_path(state: &Mutex<UpdaterState>, path: Option<String>) {
    if let Ok(mut g) = state.lock() {
        g.status.downloaded_path = path;
    }
}

pub fn fail(state: &Mutex<UpdaterState>, why: &str) {
    set_phase(state, UpdatePhase::Failed, Some(why.to_string()));
}

// ---- commands' return shapes ----

#[derive(Debug, Clone, Serialize)]
pub struct UpdateCheckResult {
    pub current_version: String,
    pub latest_tag: Option<String>,
    /// Latest version without the leading `v` (the web UI reads this one).
    pub version: Option<String>,
    pub available: bool,
    pub asset_name: Option<String>,
    pub asset_size: Option<u64>,
    /// Same as `asset_size` (the web UI reads this one).
    pub size_bytes: Option<u64>,
    pub download_url: Option<String>,
    pub error: Option<String>,
}

/// Result for a check that arrives while another check (or a
/// download) is already running (v0.10.2).
///
/// Before v0.10.2 this was always an error ("An update check is
/// already in progress."), even when the launch check had already
/// found a newer release — and the web UI then showed that error and
/// never looked at the state again, so the available update stayed
/// hidden for the whole session. Now the last known release is
/// returned instead, with the correct `available` flag, so the UI can
/// offer the update it already knows about. Only when nothing is
/// known yet is it still an error, and the UI retries that case
/// automatically after a short wait.
pub fn busy_check_result(current: String, release: Option<&ReleaseInfo>) -> UpdateCheckResult {
    match release {
        Some(r) => {
            let available = is_newer_release(&r.tag, &current);
            check_result(current, Some(r), available, None)
        }
        None => check_result(
            current,
            None,
            false,
            Some("An update check is already in progress — try again in a few seconds.".into()),
        ),
    }
}

/// Result when the worker thread running a check died before it could
/// answer (the `async` command's `spawn_blocking` join failed). The
/// UI treats it like any other check failure and offers a retry.
pub fn interrupted_check_result() -> UpdateCheckResult {
    check_result(
        current_version(),
        None,
        false,
        Some("The update check was interrupted before it could finish — try again.".into()),
    )
}

fn check_result(
    current: String,
    release: Option<&ReleaseInfo>,
    available: bool,
    error: Option<String>,
) -> UpdateCheckResult {
    UpdateCheckResult {
        current_version: current,
        latest_tag: release.map(|r| r.tag.clone()),
        version: release.map(|r| r.tag.strip_prefix('v').unwrap_or(&r.tag).to_string()),
        available,
        asset_name: release.map(|r| r.asset.name.clone()),
        asset_size: release.map(|r| r.asset.size),
        size_bytes: release.map(|r| r.asset.size),
        download_url: release.map(|r| r.asset.download_url.clone()),
        error,
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct UpdateInstallResult {
    pub downloaded_path: String,
    pub manual_install: bool,
    pub message: String,
}

// ---- network / download work (blocking; call on a worker thread) ----

/// GET the latest release from GitHub and parse it. `Err` is a short
/// human-readable reason (offline, rate-limited, no asset, ...).
fn fetch_latest_release() -> Result<ReleaseInfo, String> {
    let resp = ureq::get(RELEASES_LATEST_URL)
        .set("User-Agent", USER_AGENT)
        .set("Accept", "application/vnd.github+json")
        .timeout(Duration::from_secs(15))
        .call()
        .map_err(|e| match e {
            ureq::Error::Status(403, _) | ureq::Error::Status(429, _) => {
                "GitHub rate limit hit — try again later.".to_string()
            }
            ureq::Error::Status(code, _) => format!("GitHub answered HTTP {code}."),
            ureq::Error::Transport(t) => format!("Could not reach GitHub: {t}"),
        })?;
    let text = resp
        .into_string()
        .map_err(|e| format!("Could not read GitHub's answer: {e}"))?;
    parse_latest_release(&text, std::env::consts::OS)
        .ok_or_else(|| "This release has no installer for your platform.".to_string())
}

/// Run a check and record the outcome in `state`. Returns the same
/// result the `check_for_updates` command hands to the web UI. A check
/// already running (or a download in progress) returns the last known
/// release instead of an error (see `busy_check_result`) — the caller
/// must not treat a busy check as a failure. Never downloads.
pub fn run_check(state: &Mutex<UpdaterState>) -> UpdateCheckResult {
    let current = current_version();
    {
        let guard = match state.lock() {
            Ok(g) => g,
            Err(_) => {
                return check_result(
                    current,
                    None,
                    false,
                    Some("Updater state is unavailable.".into()),
                );
            }
        };
        match guard.status.state {
            UpdatePhase::Checking | UpdatePhase::Downloading => {
                return busy_check_result(current, guard.release.as_ref());
            }
            _ => {}
        }
    }
    set_phase(state, UpdatePhase::Checking, None);
    match fetch_latest_release() {
        Ok(release) => {
            let available = is_newer_release(&release.tag, &current);
            let result = check_result(current.clone(), Some(&release), available, None);
            if let Ok(mut g) = state.lock() {
                g.status.state = if available {
                    UpdatePhase::Available
                } else {
                    UpdatePhase::Idle
                };
                g.status.error = None;
                g.status.latest_tag = Some(release.tag.clone());
                g.status.version = Some(
                    release
                        .tag
                        .strip_prefix('v')
                        .unwrap_or(&release.tag)
                        .to_string(),
                );
                g.status.asset_name = Some(release.asset.name.clone());
                g.status.asset_size = Some(release.asset.size);
                g.status.size_bytes = Some(release.asset.size);
                g.status.download_url = Some(release.asset.download_url.clone());
                g.release = Some(release);
            }
            result
        }
        Err(e) => {
            fail(state, &e);
            check_result(current, None, false, Some(e))
        }
    }
}

/// Download `release.asset` into `dest_dir`, reporting progress 0..1.
/// Verifies the downloaded size against the size GitHub reported for
/// the asset; a mismatch is an error and the partial file is removed.
///
/// Timeouts (v0.10.2): the old code set one 60 s global timeout on
/// the whole download, so the 46 MB setup exe needed ~6 Mbps
/// sustained — any slower link failed every time with a bare timeout.
/// Now the per-chunk read timeout is 120 s (a stalled connection
/// still fails fast) and the global cap is 15 minutes, so a slow but
/// working download finishes instead of being cut off. (In ureq 2
/// the per-phase timeouts live on the `AgentBuilder`, not the
/// `Request` — the global timeout is the only one set per request.)
pub fn download_asset(
    release: &ReleaseInfo,
    dest_dir: &Path,
    on_progress: &dyn Fn(f64),
) -> Result<PathBuf, String> {
    let dest = dest_dir.join(&release.asset.name);
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(15))
        .timeout_read(Duration::from_secs(120))
        .build();
    let resp = agent
        .get(&release.asset.download_url)
        .set("User-Agent", USER_AGENT)
        .timeout(Duration::from_secs(15 * 60))
        .call()
        .map_err(|e| match e {
            ureq::Error::Status(code, _) => {
                format!("Download failed: GitHub answered HTTP {code}.")
            }
            ureq::Error::Transport(t) => format!("Download failed: {t}"),
        })?;
    let expected = release.asset.size;
    let mut reader = resp.into_reader();
    let mut file = std::fs::File::create(&dest)
        .map_err(|e| format!("Could not write {}: {e}", dest.display()))?;
    let mut buf = [0u8; 64 * 1024];
    let mut written: u64 = 0;
    loop {
        match reader.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                file.write_all(&buf[..n])
                    .map_err(|e| format!("Could not write {}: {e}", dest.display()))?;
                written += n as u64;
                if expected > 0 {
                    on_progress(written as f64 / expected as f64);
                }
            }
            Err(e) => {
                let _ = std::fs::remove_file(&dest);
                return Err(format!("Download failed: {e}"));
            }
        }
    }
    drop(file);
    if expected > 0 && written != expected {
        let _ = std::fs::remove_file(&dest);
        return Err(format!(
            "Downloaded file is {written} bytes, expected {expected} — the download was incomplete."
        ));
    }
    if written == 0 {
        let _ = std::fs::remove_file(&dest);
        return Err("Downloaded file is empty.".into());
    }
    on_progress(1.0);
    Ok(dest)
}

/// Launch the downloaded installer (Windows only — the NSIS setup
/// installs over the app folder).
///
/// REQUIRED CALL ORDER (see the v0.8.0 stuck-install bug): the caller
/// MUST stop the sidecar server and quit the app *before* calling this,
/// and exit immediately after:
///   1. stop the sidecar (`stop_sidecar_if_running` in `main.rs`),
///   2. `wait_for_process_gone("clipforge-server.exe", ...)` — do not
///      launch while the server still exists. (Only the server: the
///      app itself is still running at this point and exits right
///      after launching the installer; the app exe is the NSIS hook's
///      job, not the updater's.)
///   3. call this, then `app.exit(0)` at once.
///
/// Launching first and quitting afterwards is a bug: the installer
/// starts while `clipforge-server.exe` still holds its own exe file
/// open, and the install fails with NSIS "Error opening file for
/// writing". The NSIS installer hook
/// (`apps/desktop/src-tauri/installer-hooks.nsh`, `NSIS_HOOK_PREINSTALL`)
/// kills leftover ClipForge processes as defence in depth, but the
/// caller must not rely on it — a clean shutdown here is what makes the
/// update smooth.
pub fn launch_installer(path: &Path) -> std::io::Result<()> {
    #[cfg(windows)]
    {
        std::process::Command::new(path).spawn()?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "installers are launched only on Windows; install the downloaded file manually",
        ))
    }
}

/// Image name of the sidecar server — the one process the updater must
/// confirm is gone before launching the installer (see
/// `launch_installer` docs). The app exe itself is NOT in this check:
/// the app is still running while the updater runs.
pub const SIDECAR_IMAGE_NAME: &str = "clipforge-server.exe";

/// The image names the updater must confirm are gone before launching
/// the Windows installer: the sidecar server first (it locks
/// `clipforge-server.exe`), then the app itself. `ClipForge.exe` is the
/// product-name spelling, included in case the main binary is ever
/// renamed to it; the binary as built is `clipforge-desktop.exe`.
/// (Consumed by the NSIS hook `installer-hooks.nsh` and by the Python
/// regression tests — not by Rust code — hence the allow.)
#[allow(dead_code)]
pub const INSTALLER_GUARD_PROCESSES: &[&str] =
    &[SIDECAR_IMAGE_NAME, "clipforge-desktop.exe", "ClipForge.exe"];

/// Wait until no process with the given image name (e.g.
/// `"clipforge-server.exe"`) is running, polling `tasklist` on Windows.
/// Returns `true` once the process is gone, `false` when `timeout`
/// elapses first (or `tasklist` cannot run — fail closed, do not launch
/// the installer blind). On non-Windows there are no Windows processes
/// to wait for, so this always returns `true`.
pub fn wait_for_process_gone(image_name: &str, timeout: Duration) -> bool {
    #[cfg(windows)]
    {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            let gone = std::process::Command::new("tasklist")
                .args(["/FI", &format!("IMAGENAME eq {image_name}"), "/NH"])
                .output()
                .map(|out| {
                    let text = String::from_utf8_lossy(&out.stdout);
                    !text.to_lowercase().contains(&image_name.to_lowercase())
                })
                .unwrap_or(false);
            if gone {
                return true;
            }
            if std::time::Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
    }
    #[cfg(not(windows))]
    {
        let _ = (image_name, timeout);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_version_accepts_plain_and_v_prefixed() {
        assert_eq!(
            parse_version("0.4.0"),
            Some(Version {
                major: 0,
                minor: 4,
                patch: 0
            })
        );
        assert_eq!(
            parse_version("v2.0.1"),
            Some(Version {
                major: 2,
                minor: 0,
                patch: 1
            })
        );
        assert_eq!(
            parse_version("10.20.30"),
            Some(Version {
                major: 10,
                minor: 20,
                patch: 30
            })
        );
    }

    #[test]
    fn parse_version_rejects_malformed() {
        for bad in [
            "",
            "v",
            "v0.4",
            "0.4",
            "0.4.0.1",
            "0.4.0-beta",
            "abc",
            "0.4.x",
            "0..0",
            " 0.4.0",
            "0.4.0 ",
            "v0.4.0-rc.1",
        ] {
            assert_eq!(parse_version(bad), None, "{bad:?} should be malformed");
        }
    }

    #[test]
    fn compare_versions_is_numeric() {
        // 0.4.10 > 0.4.9 — string comparison would get this wrong.
        assert_eq!(
            compare_versions(
                &parse_version("0.4.10").unwrap(),
                &parse_version("0.4.9").unwrap()
            ),
            Ordering::Greater
        );
        assert_eq!(
            compare_versions(
                &parse_version("0.4.0").unwrap(),
                &parse_version("0.4.0").unwrap()
            ),
            Ordering::Equal
        );
        assert_eq!(
            compare_versions(
                &parse_version("0.3.9").unwrap(),
                &parse_version("0.4.0").unwrap()
            ),
            Ordering::Less
        );
        assert_eq!(
            compare_versions(
                &parse_version("1.0.0").unwrap(),
                &parse_version("0.9.9").unwrap()
            ),
            Ordering::Greater
        );
    }

    #[test]
    fn is_newer_release_rules() {
        assert!(is_newer_release("v0.4.1", "0.4.0"));
        assert!(is_newer_release("0.4.10", "0.4.9"));
        assert!(!is_newer_release("v0.4.0", "0.4.0"));
        assert!(!is_newer_release("v0.3.0", "0.4.0"));
        assert!(!is_newer_release("garbage", "0.4.0"));
        assert!(!is_newer_release("v0.4.1", "garbage"));
    }

    #[test]
    fn setup_asset_name_from_tag() {
        assert_eq!(setup_asset_name("v0.4.0"), "ClipForge_0.4.0_x64-setup.exe");
        assert_eq!(setup_asset_name("0.4.1"), "ClipForge_0.4.1_x64-setup.exe");
    }

    fn sample_release_json() -> &'static str {
        r#"{
          "tag_name": "v0.4.0",
          "assets": [
            {"name": "ClipForge_0.4.0_x64-setup.exe", "browser_download_url": "https://example.com/setup.exe", "size": 28501890},
            {"name": "ClipForge_0.4.0_x64_en-US.msi", "browser_download_url": "https://example.com/setup.msi", "size": 25288704},
            {"name": "clipforge_0.4.0_amd64.deb", "browser_download_url": "https://example.com/pkg.deb", "size": 12345678},
            {"name": "ClipForge-0.4.0-x86_64.AppImage", "browser_download_url": "https://example.com/app.AppImage", "size": 23456789},
            {"name": "ClipForge-v0.4.0-Linux.zip", "browser_download_url": "https://example.com/linux.zip", "size": 1111}
          ]
        }"#
    }

    #[test]
    fn parse_latest_release_picks_windows_setup_exe() {
        let info = parse_latest_release(sample_release_json(), "windows").unwrap();
        assert_eq!(info.tag, "v0.4.0");
        assert_eq!(info.asset.name, "ClipForge_0.4.0_x64-setup.exe");
        assert_eq!(info.asset.download_url, "https://example.com/setup.exe");
        assert_eq!(info.asset.size, 28_501_890);
    }

    #[test]
    fn parse_latest_release_picks_linux_deb_over_appimage() {
        let info = parse_latest_release(sample_release_json(), "linux").unwrap();
        assert_eq!(info.asset.name, "clipforge_0.4.0_amd64.deb");
    }

    #[test]
    fn parse_latest_release_none_without_platform_asset() {
        let json = r#"{"tag_name": "v0.4.0", "assets": [
            {"name": "ClipForge-v0.4.0-Linux.zip", "browser_download_url": "https://example.com/z.zip", "size": 1}
        ]}"#;
        assert_eq!(parse_latest_release(json, "windows"), None);
        assert_eq!(parse_latest_release(json, "linux"), None);
        // Malformed JSON and missing fields are also None.
        assert_eq!(parse_latest_release("not json", "windows"), None);
        assert_eq!(
            parse_latest_release(r#"{"tag_name": "v0.4.0"}"#, "windows"),
            None
        );
    }

    #[test]
    fn pick_update_asset_falls_back_to_msi() {
        let assets = vec![ReleaseAsset {
            name: "ClipForge_0.4.0_x64_en-US.msi".into(),
            download_url: "https://example.com/m.msi".into(),
            size: 1,
        }];
        assert_eq!(
            pick_update_asset(&assets, "windows", "v0.4.0")
                .unwrap()
                .name,
            "ClipForge_0.4.0_x64_en-US.msi"
        );
        assert_eq!(pick_update_asset(&assets, "macos", "v0.4.0"), None);
    }

    #[test]
    fn parse_latest_release_skips_malformed_assets() {
        // v0.10.2: one asset missing `size` (or its URL) used to kill
        // the whole check (`?` in the asset loop) — the user got "no
        // installer for your platform" even though the setup exe was
        // right there. Malformed assets are skipped now.
        let json = r#"{"tag_name": "v0.10.1", "assets": [
            {"name": "weird.bin", "browser_download_url": "https://example.com/w"},
            {"name": "ClipForge_0.10.1_x64-setup.exe", "browser_download_url": "https://example.com/setup.exe"},
            {"browser_download_url": "https://example.com/noname.exe", "size": 5},
            {"name": "ClipForge_0.10.1_x64_en-US.msi", "browser_download_url": "https://example.com/setup.msi", "size": 47448163}
        ]}"#;
        let info = parse_latest_release(json, "windows").unwrap();
        assert_eq!(info.asset.name, "ClipForge_0.10.1_x64-setup.exe");
        // A missing size is "unknown" (0), not a parse failure.
        assert_eq!(info.asset.size, 0);
    }

    #[test]
    fn parse_latest_release_real_v0101_asset_list() {
        // The exact asset list GitHub served for v0.10.1 (2026-11-24):
        // the updater must pick the NSIS setup exe for Windows and
        // the .deb for Linux, and must NOT offer the same version.
        let json = r#"{"tag_name": "v0.10.1", "assets": [
            {"name": "ClipForge-v0.10.1-Linux.zip", "browser_download_url": "https://example.com/l.zip", "size": 91993246},
            {"name": "ClipForge-v0.10.1-Windows.zip", "browser_download_url": "https://example.com/w.zip", "size": 58225827},
            {"name": "ClipForge_0.10.1_amd64.AppImage", "browser_download_url": "https://example.com/a", "size": 158865912},
            {"name": "ClipForge_0.10.1_amd64.deb", "browser_download_url": "https://example.com/d", "size": 75392598},
            {"name": "ClipForge_0.10.1_x64-setup.exe", "browser_download_url": "https://example.com/s.exe", "size": 45985124},
            {"name": "ClipForge_0.10.1_x64_en-US.msi", "browser_download_url": "https://example.com/s.msi", "size": 47448163}
        ]}"#;
        let win = parse_latest_release(json, "windows").unwrap();
        assert_eq!(win.asset.name, "ClipForge_0.10.1_x64-setup.exe");
        let lin = parse_latest_release(json, "linux").unwrap();
        assert_eq!(lin.asset.name, "ClipForge_0.10.1_amd64.deb");
        assert!(!is_newer_release("v0.10.1", "0.10.1"));
        assert!(is_newer_release("v0.10.1", "0.9.3"));
        assert!(is_newer_release("v0.10.1", "0.10.0"));
        // Numeric, not lexicographic: 0.10.10 > 0.10.9.
        assert!(is_newer_release("v0.10.10", "0.10.9"));
    }

    #[test]
    fn busy_check_returns_last_known_release() {
        // v0.10.2: a check that arrives while the launch check is
        // still running must hand the UI the release that is already
        // known — not an error that hides the update for the session.
        let release = ReleaseInfo {
            tag: "v0.10.1".into(),
            asset: ReleaseAsset {
                name: "ClipForge_0.10.1_x64-setup.exe".into(),
                download_url: "https://example.com/setup.exe".into(),
                size: 45985124,
            },
        };
        let r = busy_check_result("0.9.3".into(), Some(&release));
        assert!(r.available);
        assert_eq!(r.version.as_deref(), Some("0.10.1"));
        assert_eq!(r.error, None);
        // Same version is known but not newer: no phantom update.
        let r = busy_check_result("0.10.1".into(), Some(&release));
        assert!(!r.available);
        assert_eq!(r.error, None);
        // Nothing known yet: still an error, and the UI retries it.
        let r = busy_check_result("0.9.3".into(), None);
        assert!(!r.available);
        assert!(r.error.as_deref().unwrap().contains("already in progress"));
    }

    #[test]
    fn pick_update_asset_prefers_exact_setup_exe() {
        let assets = vec![
            ReleaseAsset {
                name: "ClipForge_0.4.0_x64-setup.exe".into(),
                download_url: "https://example.com/old.exe".into(),
                size: 1,
            },
            ReleaseAsset {
                name: "ClipForge_0.4.1_x64-setup.exe".into(),
                download_url: "https://example.com/new.exe".into(),
                size: 2,
            },
        ];
        assert_eq!(
            pick_update_asset(&assets, "windows", "v0.4.1")
                .unwrap()
                .download_url,
            "https://example.com/new.exe"
        );
    }

    #[test]
    fn state_helpers_record_failure_and_progress() {
        let state = Mutex::new(UpdaterState::default());
        fail(&state, "offline (test)");
        let snap = snapshot(&state);
        assert_eq!(snap.state, UpdatePhase::Failed);
        assert_eq!(snap.error.as_deref(), Some("offline (test)"));
        set_progress(&state, 0.5);
        let snap = snapshot(&state);
        assert_eq!(snap.progress, 0.5);
        assert_eq!(snap.progress_pct, 50.0);
    }

    #[test]
    fn installer_guard_lists_server_and_app_exes() {
        // The sidecar first (it is the file that locked the v0.8.0
        // install), then the app exe under both spellings.
        assert!(INSTALLER_GUARD_PROCESSES.contains(&"clipforge-server.exe"));
        assert!(INSTALLER_GUARD_PROCESSES.contains(&"clipforge-desktop.exe"));
        assert!(INSTALLER_GUARD_PROCESSES.contains(&"ClipForge.exe"));
    }

    #[test]
    fn wait_for_process_gone_returns_true_for_absent_process() {
        // A name that can never be running: true on Windows (tasklist
        // finds nothing) and on other platforms (no Windows processes
        // to wait for, so the stub returns true).
        assert!(wait_for_process_gone(
            "clipforge-definitely-not-running-xyz.exe",
            Duration::from_secs(5)
        ));
    }

    #[test]
    fn check_result_carries_web_ui_aliases() {
        let release = ReleaseInfo {
            tag: "v0.4.1".into(),
            asset: ReleaseAsset {
                name: "ClipForge_0.4.1_x64-setup.exe".into(),
                download_url: "https://example.com/setup.exe".into(),
                size: 1234,
            },
        };
        let r = check_result("0.4.0".into(), Some(&release), true, None);
        assert_eq!(r.version.as_deref(), Some("0.4.1"));
        assert_eq!(r.latest_tag.as_deref(), Some("v0.4.1"));
        assert_eq!(r.size_bytes, Some(1234));
        assert_eq!(r.asset_size, Some(1234));
        assert!(r.available);
        // Serialised state names match the documented state machine.
        let status = UpdateStatus::default();
        assert_eq!(serde_json::to_string(&status.state).unwrap(), "\"idle\"");
        assert_eq!(
            serde_json::to_string(&UpdatePhase::Failed).unwrap(),
            "\"failed\""
        );
    }
}
