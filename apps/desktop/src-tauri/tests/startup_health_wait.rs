//! Regression tests: ClipForge v0.8.2 — reliable startup health wait.
//!
//! The v0.8.1 failure: the splash said "clipforge-server did not
//! answer /health within 30 s on port 8000" while the visible log
//! showed `/yt-dlp/status` 200s — lines that were stale from a
//! previous run, in a shared append-only log with no timestamps.
//! The failure branch then KILLED the just-spawned (slow but
//! healthy) server, so Retry repeated the same doomed cycle forever.
//!
//! These tests pin the fix at the source level, like the other
//! tests in this directory: the desktop crate cannot link on a bare
//! Linux VM (it needs WebKitGTK), so source-level tests are what
//! runs here; the full `cargo test` suite — including the unit
//! tests for the same behaviour in `src/sidecar.rs` — runs in CI
//! on Windows.

use std::fs;
use std::path::PathBuf;

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn read(rel: &str) -> String {
    fs::read_to_string(manifest_dir().join(rel)).unwrap_or_else(|_| panic!("cannot read {rel}"))
}

#[test]
fn health_wait_is_120s_not_30s() {
    let sidecar = read("src/sidecar.rs");
    assert!(
        sidecar.contains("HEALTH_WAIT_TIMEOUT"),
        "sidecar.rs must define HEALTH_WAIT_TIMEOUT"
    );
    assert!(
        sidecar.contains("Duration::from_secs(120)"),
        "the health wait must be 120 s for a frozen-exe cold start"
    );
    let main_rs = read("src/main.rs");
    assert!(
        main_rs.contains("HEALTH_WAIT_TIMEOUT"),
        "main.rs must wait HEALTH_WAIT_TIMEOUT, not a hardcoded 30 s"
    );
    assert!(
        !main_rs.contains("within 30 s"),
        "the generic 30 s failure message must be gone from main.rs"
    );
    assert!(
        !sidecar.contains("pub fn wait_for_health("),
        "the old bool wait_for_health (one outcome for crash and slowness) must be gone"
    );
}

#[test]
fn health_wait_distinguishes_exited_from_slow() {
    let sidecar = read("src/sidecar.rs");
    for needle in [
        "HealthWaitOutcome",
        "wait_for_health_running",
        "Exited {",
        "TimedOut {",
        "extract_failure_hint",
        "address already in use",
    ] {
        assert!(sidecar.contains(needle), "sidecar.rs is missing {needle:?}");
    }
    // The wait loop must watch the child process, not just the clock.
    let wait_fn = sidecar
        .find("pub fn wait_for_health_running")
        .expect("wait_for_health_running must exist");
    let wait_body = &sidecar[wait_fn..];
    let wait_body = &wait_body[..wait_body
        .find("\n/// Stop a running sidecar")
        .expect("wait fn must end before stop_sidecar")];
    assert!(
        wait_body.contains("try_wait"),
        "wait_for_health_running must poll the child with try_wait so an exited server fails fast"
    );
    assert!(
        wait_body.contains("on_progress"),
        "wait_for_health_running must report progress (elapsed, listening)"
    );

    let main_rs = read("src/main.rs");
    assert!(
        main_rs.contains("exited during startup"),
        "main.rs must fail fast with the exit code when the server dies"
    );
    assert!(
        main_rs.contains("still starting after"),
        "main.rs must say the server is still starting (not dead) on timeout"
    );
    assert!(
        main_rs.contains("Retry will not restart it"),
        "the timeout message must promise Retry re-waits instead of restarting"
    );
}

#[test]
fn slow_server_is_never_killed_for_being_slow() {
    let main_rs = read("src/main.rs");
    // The TimedOut outcome keeps the child in state.sidecar.
    assert!(
        main_rs.contains("HealthWaitOutcome::Ready | HealthWaitOutcome::TimedOut"),
        "finish_launch must keep a timed-out (still alive) server running"
    );
    // Retry must not kill a still-alive spawned sidecar.
    assert!(
        main_rs.contains("spawned_alive"),
        "retry_server must check whether the spawned sidecar is still alive before stopping anything"
    );
}

#[test]
fn splash_log_is_per_launch_and_timestamped() {
    let sidecar = read("src/sidecar.rs");
    for needle in [
        "begin_launch_log",
        "LAUNCH_MARKER",
        "==== ClipForge start ",
        "tail_log_since_launch",
        "timestamp_now",
        "format_utc_timestamp",
    ] {
        assert!(sidecar.contains(needle), "sidecar.rs is missing {needle:?}");
    }
    let main_rs = read("src/main.rs");
    assert!(
        main_rs.contains("begin_launch_log(&state.log_path, port)"),
        "main.rs must rotate the log and write the launch separator before the stale cleanup"
    );
    assert!(
        main_rs.contains("tail_log_since_launch"),
        "get_server_logs must return only the current launch's lines"
    );
    // The separator is written before the cleanup, so cleanup lines
    // land in the fresh log (not wiped by a later rotation).
    assert!(
        main_rs.find("begin_launch_log").unwrap() < main_rs.find("cleanup_stale_sidecar").unwrap(),
        "begin_launch_log must run before cleanup_stale_sidecar"
    );
}

#[test]
fn stale_cleanup_does_not_depend_on_health_alone() {
    let sidecar = read("src/sidecar.rs");
    assert!(
        sidecar.contains("port_is_open"),
        "sidecar.rs must probe the port with a plain TCP connect as a /health fallback"
    );
    let cleanup = sidecar
        .find("pub fn cleanup_stale_sidecar")
        .expect("cleanup_stale_sidecar must exist");
    let cleanup_body = &sidecar[cleanup..];
    let cleanup_body = &cleanup_body[..cleanup_body
        .find("\nfn open_log_file")
        .expect("cleanup must end before open_log_file")];
    assert!(
        cleanup_body.contains("port_is_open(port)"),
        "the port probe must act when TCP connects but /health hangs"
    );
    assert!(
        cleanup_body.contains("could not attribute the listener"),
        "an unattributable listener must be left alone with a warning, not killed blind"
    );
    // Windows attribution for the hung case: netstat maps the
    // listening socket to its pid.
    assert!(
        sidecar.contains("pid_listening_on_port"),
        "sidecar.rs must map a listening socket to its pid on Windows"
    );
    // The pid-file branch kills a live clipforge-server regardless
    // of whether /health answers (it never consulted /health).
    assert!(
        cleanup_body.contains("pid_is_clipforge_server(pid)"),
        "the pid-file branch must verify identity and kill regardless of /health"
    );
}

#[test]
fn retry_reuses_a_healthy_server_instead_of_restarting_it() {
    let main_rs = read("src/main.rs");
    assert!(
        main_rs.contains("health_is_clipforge(&url)"),
        "the launch thread must probe for a healthy server before spawning"
    );
    assert!(
        main_rs.contains("reusing healthy clipforge-server already on port"),
        "the launch thread must adopt a healthy server"
    );
    assert!(
        main_rs.contains("clipforge_pid_for_port"),
        "an adopted server must be tracked by pid so quit can stop it"
    );
    // Reuse happens before the stale cleanup (which would kill it).
    assert!(
        main_rs.find("reusing healthy").unwrap() < main_rs.find("cleanup_stale_sidecar").unwrap(),
        "the reuse check must run before cleanup_stale_sidecar"
    );
    // An adopted server is stopped on quit / before an update.
    assert!(
        main_rs.contains("state.adopted.lock()"),
        "stop_sidecar_if_running must also stop an adopted server"
    );
}

#[test]
fn splash_shows_progress_and_the_status_button_works() {
    let splash = read("../src/index.html");
    assert!(
        !splash.contains("open_shell_window"),
        "the splash must not invoke the nonexistent open_shell_window command"
    );
    for needle in [
        "shell-panel",
        "elapsed_secs",
        "status-text",
        "save-panel-settings",
        "open-data-folder",
        "get_server_status",
        "get_settings",
        "save_settings",
    ] {
        assert!(splash.contains(needle), "splash page is missing {needle:?}");
    }
    assert!(
        splash.contains("Could not restart the server: "),
        "a failed Retry must say so instead of silently spinning"
    );
    // The status button lives outside the error box, so it works
    // while the server is still starting, not only after a failure.
    let error_box = splash
        .find("<div id=\"error\"")
        .expect("error box must exist");
    let button = splash
        .find("id=\"open-shell\"")
        .expect("status button must exist");
    let panel = splash
        .find("id=\"shell-panel\"")
        .expect("status panel must exist");
    assert!(
        button > error_box && panel > button,
        "the status button and panel must sit outside the error box"
    );
}

#[test]
fn release_version_is_0_11_0() {
    // The fix branches deliberately did NOT bump the version (the release
    // process owns that); the release bump to 0.11.0 updates this pin.
    let cargo = read("Cargo.toml");
    assert!(
        cargo.contains("version = \"0.11.0\""),
        "Cargo.toml must say 0.11.0 for the v0.11.0 release"
    );
}
