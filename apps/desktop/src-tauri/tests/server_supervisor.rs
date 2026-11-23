//! Regression tests: ClipForge v0.9.3 — the server must stay up
//! while the app is running.
//!
//! The user's report: the server keeps shutting down again and
//! again while the app is running and then doesn't recover. Before
//! this fix the shell spawned the sidecar once, waited for /health,
//! and then never watched it again — a crash after startup left a
//! dead server and a dead page, with no recovery except quitting
//! and relaunching the app.
//!
//! These tests pin the fix at the source level, like the other
//! tests in this directory: the desktop crate cannot link on a bare
//! Linux VM (it needs WebKitGTK), so source-level tests are what
//! runs here; the full `cargo test` suite — including the unit
//! tests for the backoff policy in `src/sidecar.rs`
//! (`restart_delay_for_attempt`, `RestartTracker`) — runs in CI on
//! Windows.

use std::fs;
use std::path::PathBuf;

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn read(rel: &str) -> String {
    fs::read_to_string(manifest_dir().join(rel)).unwrap_or_else(|_| panic!("cannot read {rel}"))
}

#[test]
fn supervisor_watches_the_sidecar_after_startup() {
    let main_rs = read("src/main.rs");
    // A supervisor thread is started once the server is up (Ready)
    // AND when it is still starting (TimedOut) — a slow server can
    // still die, and a just-started one can die a second later.
    assert!(
        main_rs.contains("start_supervisor(app.clone())"),
        "finish_launch / the adopted-server path must start the supervisor"
    );
    assert!(
        main_rs.contains("fn supervisor_loop"),
        "main.rs must have the supervisor watch loop"
    );
    // The loop watches the child process, not just /health: an
    // exited child is what the old code never noticed.
    let loop_body = &main_rs[main_rs.find("fn supervisor_loop").unwrap()..];
    assert!(
        loop_body.contains("try_wait"),
        "supervisor_loop must poll the child with try_wait"
    );
    assert!(
        loop_body.contains("health_ok"),
        "supervisor_loop must also poll /health (hung-server watchdog)"
    );
}

#[test]
fn supervisor_restarts_on_the_same_port_with_backoff() {
    let sidecar = read("src/sidecar.rs");
    for needle in [
        "SUPERVISOR_RESTART_BACKOFF_SECS",
        "restart_delay_for_attempt",
        "RestartTracker",
        "SidecarLaunchConfig",
    ] {
        assert!(sidecar.contains(needle), "sidecar.rs is missing {needle:?}");
    }
    let main_rs = read("src/main.rs");
    // Same port + same data dir, from the stored launch config —
    // never re-resolved, never a new port, or the UI could not
    // reconnect to the URL it already has.
    assert!(
        main_rs.contains("sidecar_config"),
        "main.rs must store the launch config for the supervisor"
    );
    assert!(
        main_rs.contains("start_sidecar("),
        "supervisor must respawn via start_sidecar"
    );
    // The restart is surfaced in plain language, with the attempt
    // number, through the existing status channel.
    assert!(
        main_rs.contains("Restarting {"),
        "ServerPhase must have a Restarting variant"
    );
    assert!(
        main_rs.contains("Server restarting… attempt"),
        "the restarting status must say so in plain language"
    );
}

#[test]
fn supervisor_gives_up_after_rapid_failures_instead_of_looping_forever() {
    let sidecar = read("src/sidecar.rs");
    assert!(
        sidecar.contains("SUPERVISOR_MAX_RAPID_FAILURES"),
        "sidecar.rs must cap rapid failures"
    );
    assert!(
        sidecar.contains("SUPERVISOR_RAPID_WINDOW"),
        "sidecar.rs must bound the rapid-failure window"
    );
    let main_rs = read("src/main.rs");
    assert!(
        main_rs.contains("supervisor_note_failure"),
        "main.rs must give up after rapid failures"
    );
    // Give-up shows a plain-language error with the log path and
    // Retry — not a silent loop, not a bare traceback.
    let give_up = &main_rs[main_rs.find("fn supervisor_note_failure").unwrap()..];
    assert!(
        give_up.contains("stopped restarting it automatically"),
        "give-up message must say restarts stopped"
    );
    assert!(
        give_up.contains("press Retry"),
        "give-up message must point at Retry"
    );
}

#[test]
fn supervisor_never_restarts_after_quit() {
    let main_rs = read("src/main.rs");
    // Every intentional stop invalidates the supervisor first, and
    // the quit paths all go through stop_sidecar_if_running — so
    // close=quit (v0.8.1) stays quit and the server stays dead.
    assert!(
        main_rs.contains("stop_supervisor(state, true)"),
        "stop_sidecar_if_running must permanently stop the supervisor"
    );
    assert!(
        main_rs.contains("supervisor_stop: Arc<AtomicBool>"),
        "AppState must carry the supervisor stop flag"
    );
    assert!(
        main_rs.contains("supervisor_should_stop"),
        "the supervisor loop must check the stop flag / generation"
    );
    // Retry re-arms supervision explicitly — otherwise the new
    // server would start unsupervised.
    assert!(
        main_rs.contains("supervisor_stop.store(false, Ordering::SeqCst)"),
        "retry_server must re-arm supervision after stopping the sidecar"
    );
}

#[test]
fn supervisor_reuses_a_healthy_server_on_the_port() {
    // Port handling (v0.9.3): on restart, a healthy ClipForge server
    // already answering on the port is adopted, not fought — and a
    // stale holder is cleaned up before respawning, so a port
    // conflict never leaves the app with a dead server.
    let main_rs = read("src/main.rs");
    let loop_body = &main_rs[main_rs.find("fn supervisor_loop").unwrap()..];
    assert!(
        loop_body.contains("health_is_clipforge"),
        "supervisor must adopt a healthy server already on the port"
    );
    assert!(
        loop_body.contains("cleanup_stale_sidecar"),
        "supervisor must clear a stale port holder before respawning"
    );
    // D5 guard (diagnosis §1.2): the port is open but /health stayed
    // silent for the 1 s probe — a live server under load (or a
    // second shell's server) must get one slow 5 s re-probe before
    // anything is killed, never a kill on the fast probe alone.
    assert!(
        loop_body.contains("health_is_clipforge_slow"),
        "supervisor must slow re-probe an open-but-silent port (D5 guard)"
    );
    assert!(
        main_rs.contains("fn supervisor_adopt"),
        "adoption must be one shared helper (fast + slow probe)"
    );
    let sidecar = read("src/sidecar.rs");
    assert!(
        sidecar.contains("health_is_clipforge_slow"),
        "sidecar.rs must provide the slow health probe"
    );
}

#[test]
fn supervisor_logs_the_death_forensically() {
    // Diagnosis P3-10: on restart, the exit code/signal + the last
    // 40 log lines go into clipforge-server.log, so the next "it
    // died again" is attributable instead of ending in silence.
    let main_rs = read("src/main.rs");
    assert!(
        main_rs.contains("fn supervisor_log_death"),
        "main.rs must log each server death forensically"
    );
    let log_fn = &main_rs[main_rs.find("fn supervisor_log_death").unwrap()..];
    assert!(
        log_fn.contains("exit code"),
        "death log must record the exit code"
    );
    assert!(
        log_fn.contains("last 40 log lines"),
        "death log must record the last 40 log lines"
    );
}

#[test]
fn splash_shows_the_restarting_phase() {
    // The splash polls get_server_status; a restarting server must
    // show "Server restarting… attempt N", not a stale "Starting…"
    // line and not the error panel.
    let splash = read("../src/index.html");
    assert!(
        splash.contains("restarting"),
        "splash must handle the restarting phase"
    );
    assert!(
        splash.contains("Server restarting…"),
        "splash must show the plain-language restarting text"
    );
}
