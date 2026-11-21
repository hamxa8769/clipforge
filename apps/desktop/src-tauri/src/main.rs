//! ClipForge desktop shell — Tauri v2.
//!
//! One double-click app, no terminal window:
//! * spawns the `clipforge-server` sidecar (Python/FastAPI) hidden,
//! * waits for `GET /health`, then navigates the main window to the
//!   server, which serves the static web UI itself,
//! * hides to the system tray on window close; Quit from the tray stops
//!   the sidecar and exits — the sidecar is never orphaned.
//!
//! On Windows the binary is built with `windows_subsystem = "windows"`
//! (no console window) and the sidecar is spawned with
//! `CREATE_NO_WINDOW` (see `sidecar.rs`).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod ffmpeg;
mod settings;
mod sidecar;
mod updater;

use settings::DesktopSettings;
use sidecar::{pick_port, resolve_sidecar, start_sidecar, stop_sidecar, SidecarSource};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, State, WebviewWindow, WindowEvent,
};

/// What the splash page shows.
#[derive(Debug, Clone, serde::Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
enum ServerPhase {
    Starting,
    Ready { url: String, port: u16 },
    Failed { error: String },
}

#[derive(Debug, Clone, serde::Serialize)]
struct ServerStatus {
    #[serde(flatten)]
    phase: ServerPhase,
    /// How the server was launched (`bundled exe`, `dev exe`, `dev python`).
    source: String,
    data_dir: String,
    log_path: String,
    ffmpeg: ffmpeg::FfmpegStatus,
}

struct AppState {
    status: Mutex<ServerStatus>,
    data_dir: PathBuf,
    log_path: PathBuf,
    /// Where the Tauri app-data dir is (settings file lives here).
    app_data_dir: PathBuf,
    sidecar: sidecar::SidecarState,
    /// True while a launch thread is running — prevents double-spawn
    /// (two sidecars, one orphaned) if Retry is hit while starting.
    launching: Mutex<bool>,
    /// In-app updater state (see `updater.rs`).
    update: Mutex<updater::UpdaterState>,
    /// The tray icon must be kept alive: dropping it removes it.
    tray: Mutex<Option<tauri::tray::TrayIcon>>,
}

impl AppState {
    fn set_phase(&self, phase: ServerPhase) {
        if let Ok(mut s) = self.status.lock() {
            s.phase = phase;
        }
    }
    fn snapshot(&self) -> ServerStatus {
        self.status.lock().unwrap().clone()
    }
}

/// The thread that owns the sidecar: resolve, spawn, wait for /health,
/// then publish the result. Retried from the splash page via `retry_server`.
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
        let cfg = DesktopSettings::load(&state.app_data_dir);
        let data_dir = cfg.effective_data_dir(&state.app_data_dir);
        let port = match cfg.effective_manual_port() {
            Some(p) => p,
            None => pick_port(sidecar::PREFERRED_PORT),
        };

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
                format!("dev python: python -m clipforge_api ({})", repo_root.display())
            }
        };
        if let Ok(mut s) = state.status.lock() {
            s.source = source_label.clone();
            s.data_dir = data_dir.to_string_lossy().into_owned();
        }

        match start_sidecar(&source, port, &data_dir, &state.log_path, web_out.as_deref()) {
            Ok(running) => {
                let url = format!("http://127.0.0.1:{port}");
                let healthy = sidecar::wait_for_health(
                    &url,
                    Duration::from_secs(30),
                    Duration::from_millis(250),
                );
                if healthy {
                    if let Ok(mut guard) = state.sidecar.lock() {
                        *guard = Some(running);
                    }
                    state.set_phase(ServerPhase::Ready { url, port });
                } else {
                    let mut running = running;
                    stop_sidecar(&mut running);
                    state.set_phase(ServerPhase::Failed {
                        error: format!(
                            "clipforge-server did not answer /health within 30 s on port {port}. \
                             See the log below (or {log}) for the server's own error.",
                            log = state.log_path.display()
                        ),
                    });
                }
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

fn stop_sidecar_if_running(state: &AppState) {
    if let Ok(mut guard) = state.sidecar.lock() {
        if let Some(mut running) = guard.take() {
            stop_sidecar(&mut running);
        }
    }
}

// ---------------------------------------------------------------- commands

#[tauri::command]
fn get_server_status(state: State<'_, AppState>) -> ServerStatus {
    state.snapshot()
}

#[tauri::command]
fn get_server_logs(state: State<'_, AppState>, lines: Option<usize>) -> String {
    sidecar::tail_log(&state.log_path, lines.unwrap_or(120))
}

#[tauri::command]
fn retry_server(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    if let Ok(launching) = state.launching.lock() {
        if *launching {
            return Err("Server start already in progress.".into());
        }
    }
    stop_sidecar_if_running(&state);
    state.set_phase(ServerPhase::Starting);
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

#[tauri::command]
fn open_data_folder_app(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_path(
            state.data_dir.to_string_lossy().into_owned(),
            None::<&str>,
        )
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
        // Windows: the NSIS setup installs over the app folder. Launch it
        // and quit so the installer can replace the running files. The
        // data dir (AppData) is never touched by the installer.
        updater::launch_installer(&path).map_err(|e| {
            let msg = format!("Could not launch the installer: {e}");
            updater::fail(&state.update, &msg);
            msg
        })?;
        stop_sidecar_if_running(&state);
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
            open_data_folder_app,
            show_main_window,
            quit_app,
            check_for_updates,
            get_update_status,
            download_and_install_update,
        ])
        .setup(|app| {
            let app_data_dir = app
                .path()
                .app_data_dir()
                .expect("app data dir");
            std::fs::create_dir_all(&app_data_dir).expect("create app data dir");
            let cfg = DesktopSettings::load(&app_data_dir);
            let data_dir = cfg.effective_data_dir(&app_data_dir);
            std::fs::create_dir_all(&data_dir).expect("create data dir");
            let log_dir = data_dir.join("logs");
            std::fs::create_dir_all(&log_dir).expect("create log dir");
            let log_path = log_dir.join("clipforge-server.log");

            let state = AppState {
                status: Mutex::new(ServerStatus {
                    phase: ServerPhase::Starting,
                    source: "resolving…".into(),
                    data_dir: data_dir.to_string_lossy().into_owned(),
                    log_path: log_path.to_string_lossy().into_owned(),
                    ffmpeg: ffmpeg::check_ffmpeg(),
                }),
                data_dir,
                log_path,
                app_data_dir,
                sidecar: Mutex::new(None),
                launching: Mutex::new(false),
                update: Mutex::new(updater::UpdaterState::default()),
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
                if let WindowEvent::CloseRequested { api, .. } = event {
                    // Hide to tray instead of closing; Quit lives in the
                    // tray. There is exactly one window: server status
                    // and settings live inside the main window now.
                    api.prevent_close();
                    if let Some(w) = app_handle.get_webview_window("main") {
                        let _ = w.hide();
                    }
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
