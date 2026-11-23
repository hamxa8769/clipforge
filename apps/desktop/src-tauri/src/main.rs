//! ClipForge desktop shell — Tauri v2.
//!
//! One double-click app, no terminal window:
//! * spawns the `clipforge-server` sidecar (Python/FastAPI) hidden,
//! * waits for `GET /health`, then navigates the main window to the
//!   server, which serves the static web UI itself,
//! * on startup, stops any stale server left by a previous run
//!   (pid file + `/health` probe + TCP connect — see `sidecar.rs`),
//! * closing the main window STOPS the sidecar and quits the app
//!   completely. Nothing keeps running in the background: there is
//!   no hide-to-tray-on-close. The tray icon only exists while the
//!   app is running; Quit from the tray also stops the sidecar and
//!   exits.
//!
//! Startup health wait (v0.8.2): the frozen exe can take well over
//! 30 s to answer `/health` on a cold start, so the shell waits up
//! to 120 s and tells the splash what it is waiting for (with
//! elapsed time). A server process that *exits* fails fast with its
//! exit code and the bind-error / traceback from its log; a process
//! that is still alive after 120 s is NOT killed — it keeps running
//! and Retry re-waits on it instead of restarting it. A healthy
//! server already on the port is reused, never restarted.
//!
//! On Windows the binary is built with `windows_subsystem = "windows"`
//! (no console window) and the sidecar is spawned with
//! `CREATE_NO_WINDOW` (see `sidecar.rs`). The sidecar is also put in
//! a Job Object with kill-on-job-close, so even a shell crash cannot
//! leave `clipforge-server.exe` behind to block the next install.
//!
//! Supervisor (v0.9.3): once the server is up, a supervisor thread
//! watches it for the whole life of the app. If the sidecar exits —
//! or stops answering `/health` while still alive (hung) — it is
//! restarted automatically on the SAME port with the SAME data dir,
//! with backoff (1 s, 2 s, 5 s, 10 s, capped) and a plain-language
//! "Server restarting… attempt N" status, until it stays up or fails
//! five times in a minute (then the splash shows the error with the
//! log path and Retry). The supervisor never restarts after the user
//! quits: every intentional stop invalidates the supervisor thread
//! first (see `stop_sidecar_if_running` / `stop_supervisor`).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod ffmpeg;
mod settings;
mod sidecar;
mod updater;

use settings::DesktopSettings;
use sidecar::{
    pick_port, resolve_sidecar, start_sidecar, stop_sidecar, HealthWaitOutcome,
    SidecarLaunchConfig, SidecarSource,
};
use std::io::Write;
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant};
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, State, WebviewWindow, WindowEvent,
};

/// What the splash page shows.
#[derive(Debug, Clone, serde::Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
enum ServerPhase {
    /// Still starting. `detail` says what the shell is waiting for
    /// and `elapsed_secs` how long it has waited — the splash shows
    /// both, so a slow cold start never looks like a hang (v0.8.2).
    Starting {
        elapsed_secs: u64,
        detail: String,
    },
    Ready {
        url: String,
        port: u16,
    },
    /// The server was up and then died (or hung) while the app was
    /// running; the supervisor is restarting it on the same port.
    /// `detail` is the plain-language status the splash shows
    /// ("Server restarting… attempt N (retrying in 2 s)").
    Restarting {
        attempt: u32,
        detail: String,
    },
    Failed {
        error: String,
    },
}

#[derive(Debug, Clone, serde::Serialize)]
struct ServerStatus {
    #[serde(flatten)]
    phase: ServerPhase,
    /// How the server was launched (`bundled exe`, `dev exe`, `dev python`).
    source: String,
    data_dir: String,
    log_path: String,
    /// Successful supervisor restarts since the app started (0 when
    /// the server never died). The in-progress attempt number lives
    /// on the `Restarting` phase, not here.
    restart_count: u32,
    ffmpeg: ffmpeg::FfmpegStatus,
}

struct AppState {
    status: Mutex<ServerStatus>,
    data_dir: PathBuf,
    log_path: PathBuf,
    /// Where the Tauri app-data dir is (settings file lives here).
    app_data_dir: PathBuf,
    sidecar: sidecar::SidecarState,
    /// A healthy server this shell did not spawn but adopted because
    /// it was already answering `/health` on the port (see
    /// `launch_sidecar_thread`). The shell still owns stopping it on
    /// quit / before an update: a server adopted at startup must not
    /// outlive the app.
    adopted: Mutex<Option<AdoptedSidecar>>,
    /// True while a launch thread is running — prevents double-spawn
    /// (two sidecars, one orphaned) if Retry is hit while starting.
    launching: Mutex<bool>,
    /// In-app updater state (see `updater.rs`).
    update: Mutex<updater::UpdaterState>,
    /// Supervisor control (v0.9.3): `supervisor_stop` is set when the
    /// user quits (or the updater stops the server) and is NEVER
    /// cleared except by Retry re-arming it — a supervisor thread
    /// that sees it set exits without restarting anything.
    /// `supervisor_gen` is bumped whenever supervision must change
    /// hands (Retry, a new launch, quit): a thread whose generation
    /// no longer matches exits, so two supervisors can never fight
    /// over one sidecar.
    supervisor_stop: Arc<AtomicBool>,
    supervisor_gen: Mutex<u64>,
    /// How to respawn the sidecar exactly as launched (same source,
    /// same port, same data dir). Set by the launch thread before
    /// the first spawn; read by the supervisor on every restart.
    sidecar_config: Mutex<Option<SidecarLaunchConfig>>,
    /// The tray icon must be kept alive: dropping it removes it.
    tray: Mutex<Option<tauri::tray::TrayIcon>>,
}

/// A ClipForge server adopted from a previous run (or started by
/// hand) instead of spawned by this shell. `pid` is `None` on
/// Windows when no pid file attributes the server to a pid — the
/// image name is then the only way to stop it.
#[allow(dead_code)] // port is diagnostic info, kept for the status view.
#[derive(Debug, Clone, Copy)]
struct AdoptedSidecar {
    pid: Option<u32>,
    port: u16,
}

impl AppState {
    fn set_phase(&self, phase: ServerPhase) {
        if let Ok(mut s) = self.status.lock() {
            s.phase = phase;
        }
    }
    fn set_starting(&self, elapsed_secs: u64, detail: &str) {
        self.set_phase(ServerPhase::Starting {
            elapsed_secs,
            detail: detail.to_string(),
        });
    }
    fn snapshot(&self) -> ServerStatus {
        self.status.lock().unwrap().clone()
    }
}

/// The thread that owns the sidecar: resolve, spawn, wait for /health,
/// then publish the result. Retried from the splash page via `retry_server`.
/// Startup order (v0.8.2 — the v0.8.1 failure was killing a slow server
/// at 30 s and then repeating the same doomed cycle on Retry):
/// 0. a sidecar we already spawned and are still waiting on (a previous
///    launch timed out while it was still starting) is re-waited on —
///    never spawned twice, never killed for being slow;
/// 1. a healthy ClipForge server already on the port is adopted, not
///    killed and restarted;
/// 2. only then: fresh log + stale cleanup + spawn + 120 s health wait.
fn launch_sidecar_thread(app: AppHandle) {
    {
        let state = app.state::<AppState>();
        if let Ok(mut launching) = state.launching.lock() {
            if *launching {
                return;
            }
            *launching = true;
        };
    }
    std::thread::spawn(move || {
        let state = app.state::<AppState>();

        // Step 0: re-wait on a sidecar we already spawned. Its log is
        // NOT rotated — rotating would wipe the lines that explain
        // what it is doing.
        let in_flight = state.sidecar.lock().ok().and_then(|mut guard| guard.take());
        if let Some(mut running) = in_flight {
            let outcome = await_server_health(&state, &mut running);
            finish_launch(&app, &state, running, outcome);
            if let Ok(mut launching) = state.launching.lock() {
                *launching = false;
            };
            return;
        }

        let cfg = DesktopSettings::load(&state.app_data_dir);
        let data_dir = cfg.effective_data_dir(&state.app_data_dir);
        let port = match cfg.effective_manual_port() {
            Some(p) => p,
            None => pick_port(sidecar::PREFERRED_PORT),
        };

        // Step 1: reuse. A healthy ClipForge server already on the
        // port (stale from a previous run, or started by hand) is
        // adopted instead of killed and restarted. In auto mode the
        // preferred port is probed too: a stale-but-healthy server on
        // 8000 beats starting a second server on a random port.
        let mut probe_ports = vec![port];
        if port != sidecar::PREFERRED_PORT {
            probe_ports.push(sidecar::PREFERRED_PORT);
        }
        for &p in &probe_ports {
            let url = format!("http://127.0.0.1:{p}");
            if sidecar::health_is_clipforge(&url) {
                let pid = sidecar::clipforge_pid_for_port(p, &data_dir);
                if let Ok(mut guard) = state.adopted.lock() {
                    *guard = Some(AdoptedSidecar { pid, port: p });
                }
                sidecar::log_line(
                    &state.log_path,
                    &format!(
                        "[shell] reusing healthy clipforge-server already on port {p} (pid {}) instead of starting a new one",
                        pid.map(|p| p.to_string())
                            .unwrap_or_else(|| "?".to_string())
                    ),
                );
                state.set_phase(ServerPhase::Ready { url, port: p });
                // An adopted server can die too: the supervisor
                // watches its /health and starts a fresh server when
                // it stops answering (see `supervisor_loop`).
                start_supervisor(app.clone());
                if let Ok(mut launching) = state.launching.lock() {
                    *launching = false;
                };
                return;
            }
        }

        state.set_starting(0, "Starting the ClipForge server…");

        // Step 2: fresh log for this launch (previous launch kept as
        // `.log.1`), so the splash can never show a stale line as if
        // the current server were alive — then the stale cleanup,
        // whose lines land in the fresh log.
        sidecar::begin_launch_log(&state.log_path, port);

        let resource_dir = app
            .path()
            .resource_dir()
            .unwrap_or_else(|_| PathBuf::from("."));
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."));
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let source = resolve_sidecar(&resource_dir, &exe_dir, &cwd);

        // A previous run may have died without stopping its server
        // (crash, forced kill, old version without the shutdown fix):
        // stop that stale server first, or it would keep the port and
        // the exe file locked. Probes the preferred port and the
        // configured manual port, if different.
        sidecar::cleanup_stale_sidecar(&data_dir, &probe_ports, &state.log_path);

        // Static web export for the server to serve at /: the bundled one
        // under the resources dir, or (dev Python fallback) the export in
        // the repo. Without it the server runs API-only (status page).
        let web_out = match &source {
            SidecarSource::DevPython { repo_root } => {
                let dev_out = repo_root.join("apps").join("web").join("out");
                if dev_out.join("index.html").is_file() {
                    Some(dev_out)
                } else {
                    sidecar::bundled_web_out(&resource_dir)
                }
            }
            _ => sidecar::bundled_web_out(&resource_dir),
        };

        let source_label = match &source {
            SidecarSource::Exe(p) => {
                if p.starts_with(cwd.join("dist")) || p.to_string_lossy().contains("/dist/") {
                    format!("dev exe: {}", p.display())
                } else {
                    format!("bundled exe: {}", p.display())
                }
            }
            SidecarSource::DevPython { repo_root } => {
                format!(
                    "dev python: python -m clipforge_api ({})",
                    repo_root.display()
                )
            }
        };
        if let Ok(mut s) = state.status.lock() {
            s.source = source_label.clone();
            s.data_dir = data_dir.to_string_lossy().into_owned();
        }
        // Remember exactly how this server was launched, so the
        // supervisor can respawn it on the same port with the same
        // data dir (v0.9.3) — never re-resolved, never a new port.
        if let Ok(mut guard) = state.sidecar_config.lock() {
            *guard = Some(SidecarLaunchConfig {
                source: source.clone(),
                port,
                data_dir: data_dir.clone(),
                web_out: web_out.clone(),
            });
        }

        match start_sidecar(
            &source,
            port,
            &data_dir,
            &state.log_path,
            web_out.as_deref(),
        ) {
            Ok(mut running) => {
                let outcome = await_server_health(&state, &mut running);
                finish_launch(&app, &state, running, outcome);
            }
            Err(e) => {
                state.set_phase(ServerPhase::Failed {
                    error: format!(
                        "Could not start the ClipForge server ({source_label}): {e}. \
                         In a packaged install this means clipforge-server is missing next \
                         to the app; in dev, build it with scripts/build_exe.py or run \
                         the Python API from source."
                    ),
                });
            }
        }
        if let Ok(mut launching) = state.launching.lock() {
            *launching = false;
        };
    });
}

/// Wait for a spawned server to answer `/health`, updating the splash
/// with honest progress while waiting (see
/// [`sidecar::wait_for_health_running`]). Only sets the phase — the
/// caller owns what happens to the child afterwards (see
/// `finish_launch`).
fn await_server_health(
    state: &AppState,
    running: &mut sidecar::RunningSidecar,
) -> HealthWaitOutcome {
    let port = running.port;
    let pid = running.child.id();
    let outcome = sidecar::wait_for_health_running(
        running,
        sidecar::HEALTH_WAIT_TIMEOUT,
        Duration::from_millis(250),
        &mut |elapsed, listening| {
            let detail = if listening {
                format!("Server is listening on port {port} — waiting for it to finish starting…")
            } else {
                format!("Server process is running (pid {pid}) — waiting for it to answer /health…")
            };
            state.set_starting(elapsed.as_secs(), &detail);
        },
    );
    match &outcome {
        HealthWaitOutcome::Ready => {
            let url = format!("http://127.0.0.1:{port}");
            state.set_phase(ServerPhase::Ready { url, port });
        }
        HealthWaitOutcome::Exited { code, hint } => {
            let code = match code {
                Some(c) => format!("exit code {c}"),
                None => "killed by a signal".to_string(),
            };
            let mut error = format!(
                "clipforge-server exited during startup ({code}) on port {port} — it will never answer /health. "
            );
            if let Some(hint) = hint {
                error.push_str(hint);
                error.push(' ');
            }
            error.push_str(&format!(
                "See the log below (or {}) for the full server output.",
                state.log_path.display()
            ));
            state.set_phase(ServerPhase::Failed { error });
        }
        HealthWaitOutcome::TimedOut { elapsed_secs } => {
            state.set_phase(ServerPhase::Failed {
                error: format!(
                    "clipforge-server is still starting after {elapsed_secs} s on port {port} \
                     (process {pid} is alive, but /health has not answered yet). It may still \
                     come up — press Retry to check again; Retry will not restart it while it \
                     is still running. See the log below (or {}) for what the server is doing.",
                    state.log_path.display()
                ),
            });
        }
    }
    outcome
}

/// Settle a spawned child after the health wait:
/// * `Ready` — keep it in `state.sidecar` so quit / update can stop it.
/// * `TimedOut` — keep it RUNNING in `state.sidecar`. Killing a
///   slow-but-alive server is the v0.8.1 failure: Retry re-waits on
///   this child instead of spawning a second server.
/// * `Exited` — already dead: reap it and remove the pid file.
///
/// `Ready` and `TimedOut` both start the supervisor (v0.9.3): a
/// server that is still starting can still die, and one that just
/// came up can die a second later — either way the supervisor must
/// already be watching.
fn finish_launch(
    app: &AppHandle,
    state: &AppState,
    mut running: sidecar::RunningSidecar,
    outcome: HealthWaitOutcome,
) {
    match outcome {
        HealthWaitOutcome::Ready | HealthWaitOutcome::TimedOut { .. } => {
            if let Ok(mut guard) = state.sidecar.lock() {
                *guard = Some(running);
            }
            start_supervisor(app.clone());
        }
        HealthWaitOutcome::Exited { .. } => {
            stop_sidecar(&mut running);
        }
    }
}

// ---------------------------------------------------------------- supervisor
// v0.9.3: the server must keep running while the app runs. One
// supervisor thread per launch watches the sidecar (spawned child,
// or an adopted server's /health) and restarts it on the same port
// with the same data dir when it dies or hangs. The thread is
// identified by a generation: bumping `supervisor_gen` (Retry, a new
// launch, quit) makes the old thread exit at its next check, so two
// supervisors can never spawn two servers.

/// Start a supervisor thread for the sidecar that is now running
/// (spawned or adopted). Bumps the generation, so any previous
/// supervisor thread exits instead of fighting this one.
fn start_supervisor(app: AppHandle) {
    let generation = {
        let state = app.state::<AppState>();
        match state.supervisor_gen.lock() {
            Ok(mut guard) => {
                *guard += 1;
                *guard
            }
            Err(_) => return,
        }
    };
    std::thread::spawn(move || supervisor_loop(app, generation));
}

/// Invalidate the running supervisor thread. `permanent` is true
/// when the sidecar is being stopped on purpose and must never come
/// back (quit / update) — the stop flag stays set until Retry
/// re-arms it. `permanent` is false when supervision is only
/// changing hands (a fresh launch starts its own supervisor).
fn stop_supervisor(state: &AppState, permanent: bool) {
    if permanent {
        state.supervisor_stop.store(true, Ordering::SeqCst);
    }
    if let Ok(mut guard) = state.supervisor_gen.lock() {
        *guard += 1;
    }
}

fn supervisor_generation(state: &AppState) -> u64 {
    state.supervisor_gen.lock().map(|g| *g).unwrap_or(0)
}

/// True when this supervisor thread must exit without restarting
/// anything: the user quit (stop flag set), or a newer supervisor /
/// launch took over (generation moved on).
fn supervisor_should_stop(state: &AppState, generation: u64) -> bool {
    state.supervisor_stop.load(Ordering::SeqCst) || supervisor_generation(state) != generation
}

/// What a supervisor thread must do with a child it spawned, after
/// a long wait (e.g. the health wait) during which the world may
/// have moved on. Quit and superseded look the same to
/// `supervisor_should_stop`, but they need opposite handling: on
/// quit the child must be stopped; on supersede it must be LEFT
/// ALONE — a newer launch may just have adopted it (Retry during a
/// restart health-wait), or its cleanup may already be stopping it,
/// and killing it here would undo the adoption and leave the app
/// pointing at a dead server.
enum SupervisorFate {
    Continue,
    /// The user quit (or the updater stopped the server): stop the child.
    Quit,
    /// A newer supervisor / launch owns the server now: leave the child alone.
    Superseded,
}

fn supervisor_fate(state: &AppState, generation: u64) -> SupervisorFate {
    if state.supervisor_stop.load(Ordering::SeqCst) {
        SupervisorFate::Quit
    } else if supervisor_generation(state) != generation {
        SupervisorFate::Superseded
    } else {
        SupervisorFate::Continue
    }
}

/// Apply `supervisor_fate` to a child this thread spawned: stop it
/// on quit, leave it alone on supersede, keep it otherwise.
/// Returns `true` when the caller must exit the supervisor loop.
fn supervisor_settle_spawned(
    state: &AppState,
    generation: u64,
    child: &mut sidecar::RunningSidecar,
) -> bool {
    match supervisor_fate(state, generation) {
        SupervisorFate::Continue => false,
        SupervisorFate::Quit => {
            stop_sidecar(child);
            true
        }
        SupervisorFate::Superseded => true,
    }
}

/// Sleep `dur` in 100 ms slices, returning `false` when the
/// supervisor must exit part-way through (quit / superseded) — so a
/// backoff sleep never delays quitting the app.
fn supervisor_sleep(state: &AppState, generation: u64, dur: Duration) -> bool {
    let deadline = Instant::now() + dur;
    loop {
        if supervisor_should_stop(state, generation) {
            return false;
        }
        if Instant::now() >= deadline {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// Record one more server failure; when the give-up limit is hit,
/// show the plain-language error (with the log path and Retry) and
/// return `true` — the caller must stop restarting and exit.
fn supervisor_note_failure(state: &AppState, tracker: &mut sidecar::RestartTracker) -> bool {
    if !tracker.record_failure(Instant::now()) {
        return false;
    }
    let n = sidecar::SUPERVISOR_MAX_RAPID_FAILURES;
    let window = sidecar::SUPERVISOR_RAPID_WINDOW.as_secs();
    let log = state.log_path.display();
    state.set_phase(ServerPhase::Failed {
        error: format!(
            "The ClipForge server stopped {n} times within {window} seconds and keeps failing \
             to start or stay up, so ClipForge stopped restarting it automatically. \
             See the server log at {log} for what went wrong, then press Retry to try again."
        ),
    });
    sidecar::log_line(
        &state.log_path,
        &format!(
            "[shell] supervisor gave up after {n} failures within {window} s — waiting for Retry"
        ),
    );
    true
}

/// Adopt a healthy ClipForge server already answering on the port
/// instead of spawning a second one. Shared by the supervisor's fast
/// probe and its slow D5 re-probe (see `supervisor_loop`).
fn supervisor_adopt(state: &AppState, cfg: &SidecarLaunchConfig) {
    let pid = sidecar::clipforge_pid_for_port(cfg.port, &cfg.data_dir);
    if let Ok(mut guard) = state.adopted.lock() {
        *guard = Some(AdoptedSidecar {
            pid,
            port: cfg.port,
        });
    }
    sidecar::log_line(
        &state.log_path,
        &format!(
            "[shell] a healthy clipforge-server is already answering on port {} — reusing it instead of starting a new one",
            cfg.port
        ),
    );
    state.set_phase(ServerPhase::Ready {
        url: format!("http://127.0.0.1:{}", cfg.port),
        port: cfg.port,
    });
}

/// Forensic record of a server death (v0.9.3, diagnosis P3-10):
/// the child exit code/signal plus the last 40 log lines, written
/// into `clipforge-server.log` right where the server died — so the
/// next "it died again" is attributable from the log instead of
/// ending in silence. The splash's log view shows the same tail.
fn supervisor_log_death(state: &AppState, running: &mut sidecar::RunningSidecar) {
    let exit_note = match running.child.try_wait() {
        Ok(Some(status)) => match status.code() {
            Some(c) => format!("exit code {c}"),
            None => "killed by a signal".to_string(),
        },
        Ok(None) => "still running (stopped as hung)".to_string(),
        Err(_) => "exit status unknown".to_string(),
    };
    sidecar::log_line(
        &state.log_path,
        &format!("[shell] server died ({exit_note}) — restarting it"),
    );
    let tail = sidecar::tail_log(&state.log_path, 40);
    if !tail.is_empty() {
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&state.log_path)
        {
            let _ = writeln!(f, "[shell] last 40 log lines before the restart:\n{tail}");
        }
    }
}

fn supervisor_loop(app: AppHandle, generation: u64) {
    let state = app.state::<AppState>();
    let mut tracker = sidecar::RestartTracker::new();
    let mut attempt: u32 = 0;
    // Health watchdog (see below): last time /health answered while
    // the server was Ready. Polled at most once every 2 s, so the
    // supervisor never adds load to a busy server.
    let mut last_healthy = Instant::now();
    let mut last_health_poll = Instant::now() - Duration::from_secs(10);
    let mut adopted_outage_since: Option<Instant> = None;
    // The port this supervisor watches (from the launch config, or
    // from the adopted server). `None` when the server was adopted
    // before any launch stored a config — health is then polled
    // through the adopted slot instead.
    let port: Option<u16> = state
        .sidecar_config
        .lock()
        .ok()
        .and_then(|g| g.as_ref().map(|c| c.port));

    loop {
        if supervisor_should_stop(&state, generation) {
            return;
        }

        // --- adopted server: no child handle, only /health + pid ---
        let has_child = state.sidecar.lock().map(|g| g.is_some()).unwrap_or(false);
        if !has_child {
            let adopted = state.adopted.lock().ok().and_then(|g| *g);
            match adopted {
                Some(adopted_server) => {
                    if last_health_poll.elapsed() >= Duration::from_secs(2) {
                        last_health_poll = Instant::now();
                        let url = format!("http://127.0.0.1:{}", adopted_server.port);
                        if sidecar::health_ok(&url) {
                            adopted_outage_since = None;
                        } else {
                            let since = *adopted_outage_since.get_or_insert(Instant::now());
                            let pid_gone = adopted_server
                                .pid
                                .map(|pid| !sidecar::pid_alive(pid))
                                .unwrap_or(false);
                            if pid_gone
                                || since.elapsed()
                                    >= Duration::from_secs(sidecar::SUPERVISOR_ADOPTED_OUTAGE_SECS)
                            {
                                sidecar::log_line(
                                    &state.log_path,
                                    &format!(
                                        "[shell] adopted server on port {} stopped answering /health — starting a fresh server",
                                        adopted_server.port
                                    ),
                                );
                                if let Ok(mut guard) = state.adopted.lock() {
                                    *guard = None;
                                }
                                state.set_starting(0, "Starting the ClipForge server…");
                                launch_sidecar_thread(app.clone());
                                return;
                            }
                        }
                    }
                    std::thread::sleep(Duration::from_millis(250));
                    continue;
                }
                // No child and no adopted server: Retry / quit / the
                // updater owns the sidecar now — stop watching.
                None => return,
            }
        }

        // --- spawned child: alive, exited, or hung ---
        // Inspect under a short lock, then drop the lock BEFORE the
        // /health poll: the poll can take up to a second, and quit /
        // Retry must never block on it while trying to stop the child.
        let (child_alive, child_pid) = {
            let mut guard = match state.sidecar.lock() {
                Ok(g) => g,
                Err(_) => return,
            };
            match guard.as_mut() {
                Some(running) => match running.child.try_wait() {
                    Ok(None) => (true, running.child.id()),
                    _ => (false, running.child.id()),
                },
                None => {
                    // Raced with Retry / quit taking the child: the
                    // loop top re-checks (stop flag / adopted /
                    // nothing left).
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                }
            }
        };
        let mut restart_needed = !child_alive;
        if child_alive && last_health_poll.elapsed() >= Duration::from_secs(2) {
            last_health_poll = Instant::now();
            if let Some(port) = port {
                let url = format!("http://127.0.0.1:{port}");
                if sidecar::health_ok(&url) {
                    last_healthy = Instant::now();
                    // A server that was still starting when the
                    // launch timed out has now come up: promote it to
                    // Ready so the splash navigates to it (v0.8.2
                    // slow start).
                    if matches!(state.snapshot().phase, ServerPhase::Restarting { .. }) {
                        state.set_phase(ServerPhase::Ready { url, port });
                    }
                } else if matches!(state.snapshot().phase, ServerPhase::Ready { .. })
                    && last_healthy.elapsed()
                        >= Duration::from_secs(sidecar::SUPERVISOR_HEALTH_OUTAGE_SECS)
                {
                    // Alive but /health silent for 60 s: hung, not
                    // running. Restart it the same way as a crash.
                    sidecar::log_line(
                        &state.log_path,
                        &format!(
                            "[shell] server (pid {child_pid}) is alive but has not answered /health for {} s — treating it as hung and restarting it",
                            sidecar::SUPERVISOR_HEALTH_OUTAGE_SECS
                        ),
                    );
                    restart_needed = true;
                }
            }
        }
        if !restart_needed {
            std::thread::sleep(Duration::from_millis(250));
            continue;
        }

        // Take the dead / hung child out of the slot and stop it:
        // for a hung child this kills the whole tree; for an exited
        // one it just reaps it and removes the pid file. If Retry /
        // quit took it first, there is nothing left to restart.
        let taken = match state.sidecar.lock() {
            Ok(mut guard) => guard.take(),
            Err(_) => return,
        };
        let Some(mut running) = taken else {
            continue;
        };
        // Forensic record first (exit code + last 40 log lines),
        // then stop/reap the child.
        supervisor_log_death(&state, &mut running);
        stop_sidecar(&mut running);

        // --- restart loop: backoff, respawn, wait for /health ---
        'restart: loop {
            if supervisor_should_stop(&state, generation) {
                return;
            }
            attempt += 1;
            if supervisor_note_failure(&state, &mut tracker) {
                return;
            }
            let delay = sidecar::restart_delay_for_attempt(attempt);
            state.set_phase(ServerPhase::Restarting {
                attempt,
                detail: format!(
                    "Server restarting… attempt {attempt} (retrying in {} s)",
                    delay.as_secs()
                ),
            });
            sidecar::log_line(
                &state.log_path,
                &format!(
                    "[shell] server died while the app was running — restarting it (attempt {attempt}, in {} s)",
                    delay.as_secs()
                ),
            );
            if !supervisor_sleep(&state, generation, delay) {
                return;
            }

            let cfg = match state.sidecar_config.lock() {
                Ok(g) => g.clone(),
                Err(_) => return,
            };
            let Some(cfg) = cfg else {
                state.set_phase(ServerPhase::Failed {
                    error: format!(
                        "The ClipForge server stopped and ClipForge has no record of how it was \
                         started, so it cannot restart it. See the log at {} and press Retry.",
                        state.log_path.display()
                    ),
                });
                return;
            };

            // Port handling (v0.9.3): if a healthy ClipForge server
            // is answering on the port now — e.g. it revived itself,
            // or another copy started — adopt it instead of spawning
            // a second server. Otherwise clear any stale holder
            // (kills only a verifiably-ours pid; an unattributable
            // listener is left alone and logged, and the spawn below
            // then fails to bind with a plain error — the next
            // attempt retries, and give-up shows the log path).
            let url = format!("http://127.0.0.1:{}", cfg.port);
            if sidecar::health_is_clipforge(&url) {
                supervisor_adopt(&state, &cfg);
                attempt = 0;
                last_healthy = Instant::now();
                adopted_outage_since = None;
                break 'restart;
            }
            // D5 guard: the port is open but /health stayed silent
            // for the 1 s probe — a live server under load (or a
            // second shell's server) can miss that probe. One slow
            // 5 s re-probe before touching anything: killing a live
            // server here is the second-launch hazard.
            if sidecar::port_is_open(cfg.port) && sidecar::health_is_clipforge_slow(&url) {
                supervisor_adopt(&state, &cfg);
                attempt = 0;
                last_healthy = Instant::now();
                adopted_outage_since = None;
                break 'restart;
            }
            sidecar::cleanup_stale_sidecar(&cfg.data_dir, &[cfg.port], &state.log_path);
            if supervisor_should_stop(&state, generation) {
                return;
            }

            // Respawn on the SAME port with the SAME data dir, so the
            // UI reconnects to the URL it already has. The log is NOT
            // rotated here — rotating would wipe the lines that
            // explain why the server died.
            match sidecar::start_sidecar(
                &cfg.source,
                cfg.port,
                &cfg.data_dir,
                &state.log_path,
                cfg.web_out.as_deref(),
            ) {
                Ok(mut new_running) => {
                    if supervisor_settle_spawned(&state, generation, &mut new_running) {
                        return;
                    }
                    let new_pid = new_running.child.id();
                    let outcome = sidecar::wait_for_health_running(
                        &mut new_running,
                        sidecar::HEALTH_WAIT_TIMEOUT,
                        Duration::from_millis(250),
                        &mut |_elapsed, listening| {
                            let detail = if listening {
                                format!(
                                    "Server is listening on port {} — waiting for it to finish starting… (restart attempt {attempt})",
                                    cfg.port
                                )
                            } else {
                                format!(
                                    "Server process is running (pid {new_pid}) — waiting for it to answer /health… (restart attempt {attempt})"
                                )
                            };
                            state.set_phase(ServerPhase::Restarting { attempt, detail });
                        },
                    );
                    match outcome {
                        HealthWaitOutcome::Ready => {
                            if supervisor_settle_spawned(&state, generation, &mut new_running) {
                                return;
                            }
                            if let Ok(mut guard) = state.sidecar.lock() {
                                *guard = Some(new_running);
                            }
                            state.set_phase(ServerPhase::Ready {
                                url,
                                port: cfg.port,
                            });
                            if let Ok(mut s) = state.status.lock() {
                                s.restart_count += 1;
                            }
                            sidecar::log_line(
                                &state.log_path,
                                &format!(
                                    "[shell] server restarted successfully (attempt {attempt}) on port {}",
                                    cfg.port
                                ),
                            );
                            attempt = 0;
                            last_healthy = Instant::now();
                            break 'restart;
                        }
                        HealthWaitOutcome::TimedOut { elapsed_secs } => {
                            // Still starting after 120 s: keep it
                            // RUNNING (never kill a slow server) and
                            // let the watch loop promote it to Ready
                            // when /health answers.
                            if supervisor_settle_spawned(&state, generation, &mut new_running) {
                                return;
                            }
                            if let Ok(mut guard) = state.sidecar.lock() {
                                *guard = Some(new_running);
                            }
                            state.set_phase(ServerPhase::Restarting {
                                attempt,
                                detail: format!(
                                    "Server is still starting after {elapsed_secs} s (restart attempt {attempt}) — waiting for it to answer…"
                                ),
                            });
                            last_healthy = Instant::now();
                            break 'restart;
                        }
                        HealthWaitOutcome::Exited { code, hint } => {
                            let code = match code {
                                Some(c) => format!("exit code {c}"),
                                None => "killed by a signal".to_string(),
                            };
                            sidecar::log_line(
                                &state.log_path,
                                &format!(
                                    "[shell] restart attempt {attempt} failed: server exited ({code}){}",
                                    hint.as_ref().map(|h| format!(" — {h}")).unwrap_or_default()
                                ),
                            );
                            stop_sidecar(&mut new_running);
                            continue 'restart;
                        }
                    }
                }
                Err(e) => {
                    sidecar::log_line(
                        &state.log_path,
                        &format!(
                            "[shell] restart attempt {attempt} could not start the server: {e}"
                        ),
                    );
                    continue 'restart;
                }
            }
        }
    }
}

/// Stop the sidecar if one is running — spawned or adopted (see
/// `AdoptedSidecar`). Returns what was stopped (pid + exe path) so
/// callers that need proof the process is really gone — the updater,
/// before launching the installer — can wait on it. `None` when no
/// sidecar was running. An adopted server with no known pid is
/// stopped by image name on Windows.
///
/// The supervisor is stopped FIRST (permanently: stop flag set +
/// generation bumped) — a sidecar stopped on purpose must never be
/// restarted by the supervisor thread still watching it. That is
/// what makes close=quit stay quit (v0.8.1): quit calls this, the
/// supervisor exits, the server stays dead. Retry re-arms
/// supervision explicitly (see `retry_server`).
fn stop_sidecar_if_running(state: &AppState) -> Option<sidecar::StoppedSidecar> {
    stop_supervisor(state, true);
    let mut adopted_info = None;
    if let Ok(mut guard) = state.adopted.lock() {
        if let Some(adopted) = guard.take() {
            match adopted.pid {
                Some(pid) => {
                    sidecar::kill_process_tree(pid, sidecar::SIDECAR_STOP_GRACE);
                    adopted_info = Some(sidecar::StoppedSidecar {
                        pid,
                        exe_path: None,
                    });
                }
                #[cfg(windows)]
                None => {
                    sidecar::win::taskkill_clipforge_server_image();
                }
                #[cfg(not(windows))]
                None => {}
            }
            sidecar::remove_pid_file(&state.data_dir);
        }
    }
    if let Ok(mut guard) = state.sidecar.lock() {
        if let Some(mut running) = guard.take() {
            let info = running.stopped_info();
            stop_sidecar(&mut running);
            return Some(info);
        }
    }
    adopted_info
}

// ---------------------------------------------------------------- commands

#[tauri::command]
fn get_server_status(state: State<'_, AppState>) -> ServerStatus {
    state.snapshot()
}

#[tauri::command]
fn get_server_logs(state: State<'_, AppState>, lines: Option<usize>) -> String {
    // Current launch only: stale lines from a previous run must never
    // look like a live server (v0.8.2).
    sidecar::tail_log_since_launch(&state.log_path, lines.unwrap_or(120))
}

#[tauri::command]
fn retry_server(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    if let Ok(launching) = state.launching.lock() {
        if *launching {
            return Err("Server start already in progress.".into());
        }
    }
    // If our sidecar is still alive (a previous launch timed out
    // while it was still starting), do NOT kill it: the launch thread
    // re-waits on it, and only a dead child is reaped. Killing a
    // slow-but-live server is the v0.8.1 failure this fix removes —
    // Retry must re-check a live server, not restart the same doomed
    // cycle. A healthy server already on the port is likewise reused
    // by the launch thread instead of restarted.
    let spawned_alive = match state.sidecar.lock() {
        Ok(mut guard) => match guard.as_mut() {
            Some(running) => running
                .child
                .try_wait()
                .map(|s| s.is_none())
                .unwrap_or(false),
            None => false,
        },
        Err(_) => false,
    };
    if !spawned_alive {
        stop_sidecar_if_running(&state);
    }
    // Re-arm supervision: stopping the sidecar above set the
    // supervisor stop flag (so the old supervisor never restarts a
    // server stopped on purpose). Retry means "start it again and
    // keep it up", so clear the flag — the launch thread starts a
    // fresh supervisor once the new server is Ready.
    state.supervisor_stop.store(false, Ordering::SeqCst);
    state.set_starting(0, "Starting the ClipForge server…");
    launch_sidecar_thread(app);
    Ok(())
}

#[tauri::command]
fn check_ffmpeg() -> ffmpeg::FfmpegStatus {
    ffmpeg::check_ffmpeg()
}

#[tauri::command]
fn get_settings(state: State<'_, AppState>) -> DesktopSettings {
    DesktopSettings::load(&state.app_data_dir)
}

#[tauri::command]
fn save_settings(state: State<'_, AppState>, settings: DesktopSettings) -> Result<(), String> {
    settings
        .save(&state.app_data_dir)
        .map_err(|e| format!("Could not save settings: {e}"))
}

#[tauri::command]
fn ffmpeg_download_url() -> &'static str {
    ffmpeg::FFMPEG_DOWNLOAD_URL
}

/// Open an external URL in the user's system browser.
///
/// The web UI runs inside the Tauri WebView, where `window.open(url,
/// "_blank")` opens nothing (the WebView has no new-window handler) and
/// plain `<a target="_blank">` links only reach the browser through the
/// opener plugin's `open_url` command — which the ACL denies unless a
/// capability grants it (see `capabilities/default.json`). Every
/// external link in the UI therefore goes through this command (via
/// `openExternal()` in `apps/web/lib/tauri.ts`), so there is exactly one
/// place that decides what may leave the app. Returns `true` only when
/// the OS accepted the open — the UI must not claim "opened" otherwise.
///
/// Only `http://` and `https://` URLs are allowed, with no whitespace,
/// control characters or backslashes — no `file:`, `javascript:`,
/// `data:` or other schemes that could escape the browser.
#[tauri::command]
fn open_external_url(app: AppHandle, url: String) -> Result<bool, String> {
    if !is_allowed_external_url(&url) {
        return Err("Refusing to open a non-http(s) URL.".into());
    }
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| format!("Could not open the link in your browser: {e}"))?;
    Ok(true)
}

/// Allow-list check for `open_external_url` — pure, so it is
/// unit-testable without a running Tauri app. Must stay in sync with
/// `isExternalUrl` in `apps/web/lib/external-links.ts`: a bad URL is
/// refused on both sides.
fn is_allowed_external_url(url: &str) -> bool {
    let lower = url.to_ascii_lowercase();
    let rest = lower
        .strip_prefix("http://")
        .or_else(|| lower.strip_prefix("https://"));
    match rest {
        Some(rest) if !rest.is_empty() => !url
            .chars()
            .any(|c| c.is_whitespace() || c.is_control() || c == '\\'),
        _ => false,
    }
}

#[tauri::command]
fn open_data_folder_app(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_path(state.data_dir.to_string_lossy().into_owned(), None::<&str>)
        .map_err(|e| format!("Could not open the data folder: {e}"))
}

#[tauri::command]
fn show_main_window(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    let Some(w) = app.get_webview_window("main") else {
        return Err("main window not found".into());
    };
    if let ServerPhase::Ready { url, .. } = &state.snapshot().phase {
        w.navigate(url.parse().map_err(|_| "bad server url".to_string())?)
            .map_err(|e| e.to_string())?;
    }
    w.show().map_err(|e| e.to_string())?;
    w.set_focus().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    let state = app.state::<AppState>();
    stop_sidecar_if_running(&state);
    app.exit(0);
}

// ---------------------------------------------------------------- updater
// Commands for the in-app updater (see `updater.rs`). The web UI calls
// these from the Settings page; the state machine is polled with
// `get_update_status`.

#[tauri::command]
fn check_for_updates(state: State<'_, AppState>) -> updater::UpdateCheckResult {
    updater::run_check(&state.update)
}

#[tauri::command]
fn get_update_status(state: State<'_, AppState>) -> updater::UpdateStatus {
    updater::snapshot(&state.update)
}

#[tauri::command]
fn download_and_install_update(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<updater::UpdateInstallResult, String> {
    // Only valid from Available; anything else is a no-op error.
    let release = {
        let guard = state.update.lock().map_err(|e| e.to_string())?;
        if guard.status.state != updater::UpdatePhase::Available {
            return Err(format!(
                "No update is available to install (updater state: {:?}).",
                guard.status.state
            ));
        }
        guard.release.clone().ok_or("No update release recorded.")?
    };
    updater::set_phase(&state.update, updater::UpdatePhase::Downloading, None);
    let dest_dir = std::env::temp_dir();
    let downloaded = updater::download_asset(&release, &dest_dir, &|progress| {
        updater::set_progress(&state.update, progress);
    });
    let path = match downloaded {
        Ok(p) => p,
        Err(e) => {
            updater::fail(&state.update, &e);
            return Err(e);
        }
    };
    updater::set_phase(&state.update, updater::UpdatePhase::Ready, None);
    updater::set_downloaded_path(&state.update, Some(path.to_string_lossy().into_owned()));

    if cfg!(windows) {
        // Windows: the NSIS setup installs over the app folder, so the
        // server MUST be stopped before the installer is launched —
        // launching first was the v0.8.0 failure (NSIS "Error opening
        // file for writing: ...\\clipforge-server.exe" because the
        // sidecar was still running). Stop the sidecar, wait until
        // the process is actually gone AND the exe file is no longer
        // locked, and only then launch the installer and exit. If the
        // sidecar cannot be stopped, fail the update instead of
        // launching an installer that will hit a file lock.
        let stopped = stop_sidecar_if_running(&state);
        if let Some(info) = stopped {
            if !sidecar::wait_for_process_gone(info.pid, Duration::from_secs(10)) {
                let msg = format!(
                    "Could not stop the ClipForge server (pid {}) before updating — \
                     the installer would fail to replace clipforge-server.exe. \
                     Close ClipForge and try the update again.",
                    info.pid
                );
                updater::fail(&state.update, &msg);
                return Err(msg);
            }
            if let Some(exe) = &info.exe_path {
                if !sidecar::wait_until_file_unlocked(exe, Duration::from_secs(10)) {
                    let msg = format!(
                        "The ClipForge server file ({}) is still locked after stopping the server — \
                         the installer would fail to replace it. \
                         Close ClipForge and try the update again.",
                        exe.display()
                    );
                    updater::fail(&state.update, &msg);
                    return Err(msg);
                }
            }
        }
        // Belt and braces: confirm by image name (tasklist) that no
        // ClipForge server survives — e.g. a stale server from an
        // older run that this shell never spawned. Only the server is
        // checked here: the app itself is still running at this point
        // (it exits right after launching the installer), so waiting
        // for the app exe would never succeed. This is the same guard
        // the NSIS hook applies; if it fails here, do not launch.
        if !updater::wait_for_process_gone(updater::SIDECAR_IMAGE_NAME, Duration::from_secs(10)) {
            let msg = format!(
                "A ClipForge server ({}) is still running and would block the installer — \
                 close ClipForge (tray icon → Quit) or end {} in Task Manager, \
                 then try the update again.",
                updater::SIDECAR_IMAGE_NAME,
                updater::SIDECAR_IMAGE_NAME
            );
            updater::fail(&state.update, &msg);
            return Err(msg);
        }
        updater::launch_installer(&path).map_err(|e| {
            let msg = format!("Could not launch the installer: {e}");
            updater::fail(&state.update, &msg);
            msg
        })?;
        app.exit(0);
        // Unreachable, but keep the type checker happy.
        Ok(updater::UpdateInstallResult {
            downloaded_path: path.to_string_lossy().into_owned(),
            manual_install: false,
            message: "Installer launched — ClipForge is closing so the installer can replace the app files.".into(),
        })
    } else {
        // Linux (and macOS): no silent install — the user installs the
        // downloaded .deb / .AppImage themselves.
        Ok(updater::UpdateInstallResult {
            downloaded_path: path.to_string_lossy().into_owned(),
            manual_install: true,
            message: format!(
                "Downloaded to {}. Install it manually (double-click the .deb, or run the .AppImage).",
                path.display()
            ),
        })
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<tauri::tray::TrayIcon> {
    let show = MenuItemBuilder::with_id("show", "Show ClipForge").build(app)?;
    let data = MenuItemBuilder::with_id("data", "Open data folder").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
    let menu = MenuBuilder::new(app)
        .items(&[&show, &data, &quit])
        .build()?;
    let icon = {
        let png = image::load_from_memory(include_bytes!("../icons/icon.png"))
            .expect("tray icon png decodes");
        let rgba = png.to_rgba8();
        let (w, h) = rgba.dimensions();
        tauri::image::Image::new_owned(rgba.into_raw(), w, h)
    };
    TrayIconBuilder::with_id("main")
        .icon(icon)
        .tooltip("ClipForge")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "data" => {
                let state = app.state::<AppState>();
                let _ = open_data_folder_app(app.clone(), state);
            }
            "quit" => quit_app(app.clone()),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
        })
        .build(app)
}

fn main() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            get_server_status,
            get_server_logs,
            retry_server,
            check_ffmpeg,
            get_settings,
            save_settings,
            ffmpeg_download_url,
            open_external_url,
            open_data_folder_app,
            show_main_window,
            quit_app,
            check_for_updates,
            get_update_status,
            download_and_install_update,
        ])
        .setup(|app| {
            let app_data_dir = app.path().app_data_dir().expect("app data dir");
            std::fs::create_dir_all(&app_data_dir).expect("create app data dir");
            let cfg = DesktopSettings::load(&app_data_dir);
            let data_dir = cfg.effective_data_dir(&app_data_dir);
            std::fs::create_dir_all(&data_dir).expect("create data dir");
            let log_dir = data_dir.join("logs");
            std::fs::create_dir_all(&log_dir).expect("create log dir");
            let log_path = log_dir.join("clipforge-server.log");

            let state = AppState {
                status: Mutex::new(ServerStatus {
                    phase: ServerPhase::Starting {
                        elapsed_secs: 0,
                        detail: "Starting the ClipForge server…".into(),
                    },
                    source: "resolving…".into(),
                    data_dir: data_dir.to_string_lossy().into_owned(),
                    log_path: log_path.to_string_lossy().into_owned(),
                    restart_count: 0,
                    ffmpeg: ffmpeg::check_ffmpeg(),
                }),
                data_dir,
                log_path,
                app_data_dir,
                sidecar: Mutex::new(None),
                adopted: Mutex::new(None),
                launching: Mutex::new(false),
                update: Mutex::new(updater::UpdaterState::default()),
                supervisor_stop: Arc::new(AtomicBool::new(false)),
                supervisor_gen: Mutex::new(0),
                sidecar_config: Mutex::new(None),
                tray: Mutex::new(None),
            };
            app.manage(state);

            // SIGINT / SIGTERM (e.g. `kill`, logout, OS shutdown): the
            // RunEvent::ExitRequested hook only fires on a *clean* Tauri
            // exit, so without this a forced kill would orphan the sidecar.
            {
                let handle = app.handle().clone();
                ctrlc::set_handler(move || {
                    eprintln!("[shell] signal received, stopping sidecar…");
                    let state = handle.state::<AppState>();
                    stop_sidecar_if_running(&state);
                    eprintln!("[shell] sidecar stopped, exiting.");
                    std::process::exit(0);
                })
                .expect("install SIGINT/SIGTERM handler");
            }

            let tray = build_tray(app.handle())?;
            if let Some(st) = app.try_state::<AppState>() {
                if let Ok(mut guard) = st.tray.lock() {
                    *guard = Some(tray);
                }
            }

            let main: WebviewWindow = app
                .get_webview_window("main")
                .expect("main window from tauri.conf.json");
            let app_handle = app.handle().clone();
            main.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { .. } = event {
                    // Close means quit: stop the sidecar and exit the
                    // app completely. Nothing keeps running in the
                    // background — no hide-to-tray-on-close. (The tray
                    // icon only exists while the app is running; Quit
                    // from the tray does the same thing.)
                    let state = app_handle.state::<AppState>();
                    stop_sidecar_if_running(&state);
                    app_handle.exit(0);
                }
            });

            launch_sidecar_thread(app.handle().clone());

            // One quiet update check on launch: it only sets the updater
            // state (Available / Idle / Failed) — it never downloads.
            {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    let state = handle.state::<AppState>();
                    updater::run_check(&state.update);
                });
            }
            Ok(())
        });

    let app = builder
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::ExitRequested { .. } = event {
            let state = app_handle.state::<AppState>();
            stop_sidecar_if_running(&state);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn external_url_allow_list_accepts_the_links_the_ui_opens() {
        // Every external URL the UI can produce (OAuth authorize URLs,
        // developer consoles, FFmpeg downloads, GitHub releases).
        for url in [
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
            "https://console.cloud.google.com/apis/credentials",
            "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
            "https://developers.facebook.com/apps",
            "https://developers.tiktok.com/",
            "https://www.facebook.com/v19.0/dialog/oauth",
            "https://www.tiktok.com/v2/auth/authorize/",
            "https://ffmpeg.org/download.html",
            "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
            "https://github.com/hamxa8769/clipforge/releases",
            "http://127.0.0.1:17847/auth/callback/youtube",
            "HTTPS://EXAMPLE.COM/UPPER",
        ] {
            assert!(is_allowed_external_url(url), "{url} should be allowed");
        }
    }

    #[test]
    fn external_url_allow_list_refuses_anything_but_http() {
        for url in [
            "",
            "https://",
            "http://",
            "ftp://example.com/x",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
            "mailto:someone@example.com",
            "/channels/connect",
            "https://example.com/has space",
            "https://example.com/tab\there",
            "https://example.com/back\\slash",
            "https://example.com\nnewline",
        ] {
            assert!(!is_allowed_external_url(url), "{url:?} should be refused");
        }
    }
}
