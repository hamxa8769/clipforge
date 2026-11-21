//! Regression test: ClipForge v0.4.0 is ONE window only.
//!
//! The old second window ("shell", `shell.html`, tray item
//! "Server status & settings") was removed; server status and settings
//! now live inside the main window. This test reads the Tauri shell
//! sources and config and fails if any second-window creation comes
//! back.

use std::fs;
use std::path::PathBuf;

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

#[test]
fn no_shell_window_in_tauri_sources() {
    let rust_files = [
        "src/main.rs",
        "src/sidecar.rs",
        "src/ffmpeg.rs",
        "src/settings.rs",
        "src/updater.rs",
    ];
    // Strings that only a second-window implementation would contain.
    let banned = [
        "open_shell_window",
        "get_webview_window(\"shell\")",
        "WebviewWindowBuilder::new",
        "shell.html",
        "Server status & settings",
        "MenuItemBuilder::with_id(\"shell\"",
    ];
    for file in rust_files {
        let text = fs::read_to_string(manifest_dir().join(file))
            .unwrap_or_else(|_| panic!("cannot read {file}"));
        for needle in banned {
            assert!(
                !text.contains(needle),
                "{file} still references the second window ({needle:?})"
            );
        }
    }
}

#[test]
fn tauri_config_declares_exactly_one_main_window() {
    let text = fs::read_to_string(manifest_dir().join("tauri.conf.json"))
        .expect("cannot read tauri.conf.json");
    let conf: serde_json::Value =
        serde_json::from_str(&text).expect("tauri.conf.json is not valid JSON");
    let windows = conf["app"]["windows"]
        .as_array()
        .expect("app.windows must be an array");
    assert_eq!(
        windows.len(),
        1,
        "expected exactly one window, got {}",
        windows.len()
    );
    assert_eq!(windows[0]["label"], "main");
}

#[test]
fn update_commands_are_registered() {
    // The web UI builds against these exact command names.
    let main_rs =
        fs::read_to_string(manifest_dir().join("src/main.rs")).expect("cannot read src/main.rs");
    for cmd in [
        "check_for_updates",
        "get_update_status",
        "download_and_install_update",
    ] {
        assert!(
            main_rs.contains(&format!("fn {cmd}")),
            "src/main.rs is missing the {cmd} command"
        );
        assert!(
            main_rs.contains(&format!("{cmd},")),
            "src/main.rs does not register {cmd} in the invoke handler"
        );
    }
    // The removed shell command must be gone from the handler too.
    assert!(!main_rs.contains("open_shell_window"));
}
