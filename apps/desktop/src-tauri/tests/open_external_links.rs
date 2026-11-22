//! Regression tests: external links must open in the system browser
//! (v0.4.2 — the "no link opens from inside the app" bug).
//!
//! v0.4.1 had NO `capabilities/` file at all, so Tauri's ACL denied
//! every invoke from the web UI — the UI is served from
//! http://127.0.0.1 (a remote origin for Tauri), where ALL invokes are
//! ACL-checked — including the opener plugin's `open_url`, which is the
//! only thing that can take a link out of the WebView. On top of that,
//! the Connect buttons used `window.open(url, "_blank")`, which the
//! WebView never routes to the browser. The result: nothing opened,
//! while the UI claimed "Signed-in page opened in a new tab".
//!
//! These tests pin the three layers of the fix, without needing a
//! running app or a display:
//!   1. the capability grants the opener + every app command, for the
//!      main window, on the loopback origin the UI is served from;
//!   2. the build.rs app manifest lists exactly the commands registered
//!      in `src/main.rs` (a command missing from either side is silently
//!      denied from the UI — or fails the build);
//!   3. the web UI routes every external link through `openExternal()`
//!      — no raw `window.open()` call and no raw `<a target="_blank">`
//!      anchor outside the one shared `ExternalLink` component.
//!
//! Style note: like `no_shell_window.rs`, these tests read the sources
//! as text — the desktop crate cannot link on a bare Linux VM (it needs
//! WebKitGTK), so source-level tests are what runs here; the full
//! `cargo test` suite runs in CI on Windows.

use std::collections::BTreeSet;
use std::fs;
use std::path::PathBuf;

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn read(rel: &str) -> String {
    fs::read_to_string(manifest_dir().join(rel)).unwrap_or_else(|_| panic!("cannot read {rel}"))
}

/// Lines that are code, not comments: a line whose trimmed form starts
/// with `//`, `*` or `/*` is a comment (doc comments included). The
/// doc comments in this repo mention `window.open()` and
/// `target="_blank"` by name, so a naive substring search would flag
/// them.
fn code_lines(text: &str) -> Vec<&str> {
    text.lines()
        .filter(|l| {
            let t = l.trim_start();
            !(t.starts_with("//") || t.starts_with('*') || t.starts_with("/*"))
        })
        .collect()
}

/// Commands in the `generate_handler![ ... ]` list of `src/main.rs`.
fn invoke_handler_commands() -> BTreeSet<String> {
    let main_rs = read("src/main.rs");
    let start = main_rs
        .find("generate_handler![")
        .expect("src/main.rs has no generate_handler!");
    let body = &main_rs[start..];
    let end = body.find(']').expect("generate_handler! never closes");
    body[..end]
        .split(|c: char| c == ',' || c == '[' || c.is_whitespace())
        .filter(|s| !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'))
        .map(str::to_string)
        .collect()
}

/// Commands in the `AppManifest::new().commands(&[ ... ])` list of
/// `build.rs`.
fn app_manifest_commands() -> BTreeSet<String> {
    let build_rs = read("build.rs");
    let start = build_rs
        .find(".commands(&[")
        .expect("build.rs has no AppManifest commands list");
    let body = &build_rs[start..];
    let end = body
        .find("])")
        .expect("AppManifest commands list never closes");
    body[..end]
        .split('"')
        .skip(1)
        .step_by(2)
        .map(str::to_string)
        .collect()
}

#[test]
fn capability_grants_opener_and_app_commands_on_the_loopback_origin() {
    let text = read("capabilities/default.json");
    let cap: serde_json::Value =
        serde_json::from_str(&text).expect("capabilities/default.json is not valid JSON");
    assert_eq!(cap["identifier"], "default");

    let windows = cap["windows"]
        .as_array()
        .expect("capability must list windows");
    assert!(
        windows.iter().any(|w| w == "main"),
        "capability must apply to the main window, got {windows:?}"
    );

    // The web UI is served from http://127.0.0.1:<port> — a remote
    // origin for Tauri. Without this, no permission in the file applies
    // to the UI at all, and every invoke is denied.
    let urls = cap["remote"]["urls"]
        .as_array()
        .expect("capability must list remote urls for the served UI");
    assert!(
        urls.iter()
            .any(|u| u.as_str().unwrap_or("").contains("127.0.0.1")),
        "capability remote urls must cover http://127.0.0.1, got {urls:?}"
    );

    let permissions: BTreeSet<String> = cap["permissions"]
        .as_array()
        .expect("capability must list permissions")
        .iter()
        .filter_map(|p| p.as_str().map(str::to_string))
        .collect();
    // The opener plugin's open_url command + its http(s) URL scope: this
    // is what carries `<a target="_blank">` clicks to the browser.
    assert!(
        permissions.contains("opener:default"),
        "capability must grant opener:default, got {permissions:?}"
    );
    // The shared open-external-link command (lib/tauri.ts openExternal).
    assert!(
        permissions.contains("allow-open-external-url"),
        "capability must grant allow-open-external-url, got {permissions:?}"
    );
    // Every app command the UI can invoke must be granted, or its
    // invoke is denied with no visible error.
    for cmd in invoke_handler_commands() {
        let perm = format!("allow-{}", cmd.replace('_', "-"));
        assert!(
            permissions.contains(&perm),
            "capability is missing {perm} for command {cmd}"
        );
    }
}

#[test]
fn app_manifest_lists_exactly_the_registered_commands() {
    let handler = invoke_handler_commands();
    let manifest = app_manifest_commands();
    assert!(
        handler.contains("open_external_url"),
        "src/main.rs must register the open_external_url command"
    );
    assert_eq!(
        handler, manifest,
        "build.rs AppManifest commands must match src/main.rs generate_handler! \
         (a command missing from either side is silently denied from the web UI)"
    );
}

#[test]
fn rust_side_opens_only_http_urls() {
    let main_rs = read("src/main.rs");
    assert!(
        main_rs.contains("fn open_external_url"),
        "src/main.rs must define the open_external_url command"
    );
    assert!(
        main_rs.contains("is_allowed_external_url"),
        "open_external_url must refuse non-http(s) URLs via is_allowed_external_url"
    );
    assert!(
        main_rs.contains("open_url(url"),
        "open_external_url must open via the opener plugin"
    );
}

#[test]
fn web_ui_opens_external_links_only_through_open_external() {
    let web = manifest_dir().join("../../web");
    let mut files = Vec::new();
    for dir in ["app", "components", "lib"] {
        let root = web.join(dir);
        let mut stack = vec![root];
        while let Some(d) = stack.pop() {
            for entry in fs::read_dir(&d).unwrap_or_else(|_| panic!("cannot read {}", d.display()))
            {
                let path = entry.expect("dir entry").path();
                if path.is_dir() {
                    stack.push(path);
                } else if path.extension().is_some_and(|e| e == "ts" || e == "tsx") {
                    files.push(path);
                }
            }
        }
    }
    assert!(!files.is_empty(), "expected web sources under apps/web");

    for path in files {
        // Normalise the separator: on Windows strip_prefix yields
        // `components\ui.tsx`, which never equals the forward-slash
        // exemption below — the test then flagged ExternalLink's own
        // anchor (and lib/tauri.ts's fallback) as violations.
        let rel = path
            .strip_prefix(&web)
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        let text = fs::read_to_string(&path).unwrap();
        let code = code_lines(&text).join("\n");
        // The only live window.open() call is the plain-browser fallback
        // inside openExternal() itself.
        if rel != "lib/tauri.ts" {
            assert!(
                !code.contains("window.open("),
                "{rel} still opens a link with raw window.open() — use openExternal() from lib/tauri.ts"
            );
        }
        // The only live target=_blank anchor is the shared ExternalLink
        // component, which intercepts the click and routes it through
        // openExternal().
        if rel != "components/ui.tsx" {
            assert!(
                !code.contains("target=\"_blank\""),
                "{rel} still has a raw <a target=\"_blank\"> — use ExternalLink from components/ui.tsx"
            );
        }
    }
}
