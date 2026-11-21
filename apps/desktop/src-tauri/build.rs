//! Build script: Tauri build + app ACL manifest.
//!
//! The `AppManifest` lists every `#[tauri::command]` in `src/main.rs`.
//! Tauri generates `allow-<command>` permissions from it, which the
//! capability file (`capabilities/default.json`) then grants to the main
//! window. Without this manifest there is no app ACL at all, so a
//! capability cannot grant the app commands — and because the web UI is
//! served from http://127.0.0.1 (a remote origin for Tauri), every
//! invoke from the UI is ACL-checked and denied. That was the "no link
//! opens" bug: the opener plugin's `open_url` command was denied, and
//! `window.open()` is never routed to the browser by the WebView.
//!
//! KEEP IN SYNC: `tests/open_external_links.rs` fails if a command is
//! registered in `src/main.rs` but missing here (or vice versa).
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "check_ffmpeg",
            "check_for_updates",
            "download_and_install_update",
            "ffmpeg_download_url",
            "get_server_logs",
            "get_server_status",
            "get_settings",
            "get_update_status",
            "open_data_folder_app",
            "open_external_url",
            "quit_app",
            "retry_server",
            "save_settings",
            "show_main_window",
        ]),
    ))
    .expect("tauri build failed");
}
