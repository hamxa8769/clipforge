//! Sidecar lifecycle for `clipforge-server`.
//!
//! Resolution order:
//! 1. `CLIPFORGE_SIDECAR_PATH` env override (explicit, any mode).
//! 2. Tauri `externalBin` sidecar `clipforge-server`
//!    (`<resources>/binaries/clipforge-server-<target-triple>[.exe]`).
//! 3. A plain `clipforge-server[.exe]` next to the app resources / exe
//!    (where the release packager stages it).
//! 4. **Dev only:** `<repo>/dist/clipforge-server[.exe]` (the PyInstaller
//!    one-file build produced by `scripts/build_exe.py`).
//! 5. **Dev only:** `python -m clipforge_api` from a source checkout.
//!
//! The server is always spawned hidden (no console window on Windows:
//! `CREATE_NO_WINDOW`), bound to 127.0.0.1 only, with
//! `CLIPFORGE_DATA_DIR` / `DATABASE_URL` pointed at the desktop app-data
//! dir. Its stdout/stderr go to a rotating log file; shutdown is
//! SIGTERM (Unix) / terminate (Windows), wait up to 5 s, then force-kill.

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// How the sidecar will be launched.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SidecarSource {
    /// A real `clipforge-server` executable.
    Exe(PathBuf),
    /// Dev-only fallback: `python -m clipforge_api` run from `repo_root`.
    DevPython { repo_root: PathBuf },
}

/// Ports: prefer 8000, otherwise take whatever the OS hands us.
pub const PREFERRED_PORT: u16 = 8000;

/// Pick a free loopback port. Tries `preferred` first; if it is taken,
/// binds port 0 and returns the port the OS assigned.
///
/// There is an unavoidable TOCTOU window between this probe and the
/// server binding the port; the server failing to bind is surfaced by
/// the health-wait timeout and the splash page's Retry button.
pub fn pick_port(preferred: u16) -> u16 {
    if let Ok(l) = TcpListener::bind(("127.0.0.1", preferred)) {
        let port = l.local_addr().map(|a| a.port()).unwrap_or(preferred);
        drop(l);
        return port;
    }
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(preferred)
}

/// Pure candidate search used by [`resolve_sidecar`]; unit-tested.
pub fn find_sidecar_in_dirs(dirs: &[PathBuf]) -> Option<PathBuf> {
    for dir in dirs {
        for name in exe_names() {
            let cand = dir.join(name);
            if cand.is_file() {
                return Some(cand);
            }
        }
    }
    None
}

fn exe_names() -> Vec<String> {
    #[cfg(windows)]
    {
        vec![
            "clipforge-server.exe".into(),
            "clipforge-server-x86_64-pc-windows-msvc.exe".into(),
        ]
    }
    #[cfg(not(windows))]
    {
        vec![
            "clipforge-server".into(),
            "clipforge-server-x86_64-unknown-linux-gnu".into(),
            "clipforge-server-aarch64-apple-darwin".into(),
        ]
    }
}

/// Walk up from `start` looking for a directory that looks like the repo
/// root (contains `apps/desktop/src-tauri/tauri.conf.json`).
pub fn find_repo_root(start: &Path) -> Option<PathBuf> {
    let mut cur = Some(start.to_path_buf());
    while let Some(dir) = cur {
        if dir
            .join("apps/desktop/src-tauri/tauri.conf.json")
            .is_file()
        {
            return Some(dir);
        }
        cur = dir.parent().map(|p| p.to_path_buf());
    }
    None
}

/// Resolve how to launch the server.
///
/// `resource_dir` / `exe_dir` come from Tauri (`AppHandle::path()`).
/// `cwd` is used to find the repo root for the dev fallbacks.
pub fn resolve_sidecar(resource_dir: &Path, exe_dir: &Path, cwd: &Path) -> SidecarSource {
    if let Ok(p) = std::env::var("CLIPFORGE_SIDECAR_PATH") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return SidecarSource::Exe(p);
        }
    }
    // Tauri externalBin layout: <resources>/binaries/<name>-<triple>[.exe]
    // plus a plain binary next to resources / the app exe (release staging).
    let mut dirs: Vec<PathBuf> = vec![
        resource_dir.join("binaries"),
        resource_dir.to_path_buf(),
        exe_dir.to_path_buf(),
    ];
    if let Some(repo) = find_repo_root(cwd) {
        dirs.push(repo.join("dist"));
        if let Some(exe) = find_sidecar_in_dirs(&dirs) {
            return SidecarSource::Exe(exe);
        }
        return SidecarSource::DevPython { repo_root: repo };
    }
    if let Some(exe) = find_sidecar_in_dirs(&dirs) {
        return SidecarSource::Exe(exe);
    }
    // No repo found and no binary: last resort is the Python fallback
    // from the current directory (will fail loudly with a clear log line).
    SidecarSource::DevPython {
        repo_root: cwd.to_path_buf(),
    }
}

/// Build the command that launches the server for `source`.
/// Returns `(program, args, workdir)`.
pub fn sidecar_command_parts(
    source: &SidecarSource,
    port: u16,
    _data_dir: &Path,
) -> (String, Vec<String>, PathBuf) {
    match source {
        SidecarSource::Exe(path) => (
            path.to_string_lossy().into_owned(),
            vec![
                "--host".into(),
                "127.0.0.1".into(),
                "--port".into(),
                port.to_string(),
            ],
            path.parent()
                .map(|p| p.to_path_buf())
                .unwrap_or_else(|| PathBuf::from(".")),
        ),
        SidecarSource::DevPython { repo_root } => {
            let py = if cfg!(windows) { "python" } else { "python3" };
            (
                py.into(),
                vec![
                    "-m".into(),
                    "clipforge_api".into(),
                    "--host".into(),
                    "127.0.0.1".into(),
                    "--port".into(),
                    port.to_string(),
                ],
                repo_root.clone(),
            )
        }
    }
}

/// Env for the sidecar process. Data dir + SQLite URL always point at
/// the desktop app-data dir (env already set wins in the server itself,
/// but we set them explicitly here so the packaged app is deterministic).
/// `web_out` is the static web export, when the shell found one: the
/// server honours `CLIPFORGE_WEB_OUT` (see `clipforge_api/__main__.py`).
/// It is needed because the Linux bundles install the sidecar to
/// `/usr/bin` while the export lives under the app resources dir
/// (`<resources>/binaries/web/out`), so "next to the exe" does not hold.
pub fn sidecar_env(data_dir: &Path, web_out: Option<&Path>) -> Vec<(String, String)> {
    let db = data_dir.join("clipforge.db");
    let mut env = vec![
        (
            "CLIPFORGE_DATA_DIR".into(),
            data_dir.to_string_lossy().into_owned(),
        ),
        (
            "DATABASE_URL".into(),
            format!("sqlite:///{}", db.as_posix()),
        ),
    ];
    if let Some(web_out) = web_out {
        env.push((
            "CLIPFORGE_WEB_OUT".into(),
            web_out.to_string_lossy().into_owned(),
        ));
    }
    env
}

/// The bundled static web export, if the packager staged one:
/// `<resource_dir>/binaries/web/out/index.html` (see `tauri.conf.json`
/// `bundle.resources`). `None` when it is absent (dev runs without a
/// static export, or a zip install without `web/out`).
pub fn bundled_web_out(resource_dir: &Path) -> Option<PathBuf> {
    let out = resource_dir.join("binaries").join("web").join("out");
    if out.join("index.html").is_file() {
        Some(out)
    } else {
        None
    }
}

/// A running sidecar + where its log lives.
#[allow(dead_code)] // log_path/source/port are diagnostic info, kept for the status view.
pub struct RunningSidecar {
    pub child: Child,
    pub log_path: PathBuf,
    pub source: SidecarSource,
    pub port: u16,
}

fn open_log_file(log_path: &Path) -> std::io::Result<std::fs::File> {
    // Rotate: keep one backup once the log passes 5 MiB.
    if let Ok(meta) = fs::metadata(log_path) {
        if meta.len() > 5 * 1024 * 1024 {
            let bak = log_path.with_extension("log.1");
            let _ = fs::rename(log_path, bak);
        }
    }
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)?;
    }
    OpenOptions::new().create(true).append(true).open(log_path)
}

fn pipe_to_log<R: std::io::Read + Send + 'static>(
    reader: R,
    log_path: PathBuf,
    tag: &'static str,
) {
    std::thread::spawn(move || {
        let mut file = match open_log_file(&log_path) {
            Ok(f) => f,
            Err(_) => return,
        };
        let buf = BufReader::new(reader);
        for line in buf.lines() {
            if let Ok(line) = line {
                let _ = writeln!(file, "[{tag}] {line}");
                let _ = file.flush();
            }
        }
    });
}

/// Spawn the sidecar hidden, wiring stdout/stderr into the log file.
pub fn start_sidecar(
    source: &SidecarSource,
    port: u16,
    data_dir: &Path,
    log_path: &Path,
    web_out: Option<&Path>,
) -> std::io::Result<RunningSidecar> {
    let (program, args, workdir) = sidecar_command_parts(source, port, data_dir);
    let mut cmd = Command::new(&program);
    cmd.args(&args).current_dir(&workdir);
    for (k, v) in sidecar_env(data_dir, web_out) {
        cmd.env(k, v);
    }
    if let SidecarSource::DevPython { repo_root } = source {
        // Make `clipforge_api` + `clipforge` importable from source.
        let mut paths: Vec<PathBuf> = vec![
            repo_root.join("apps").join("api"),
            repo_root.join("src"),
        ];
        if let Ok(existing) = std::env::var("PYTHONPATH") {
            paths.extend(std::env::split_paths(&existing));
        }
        let joined = std::env::join_paths(paths).unwrap_or_default();
        cmd.env("PYTHONPATH", joined);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: never show a console window for the server.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd.spawn()?;
    if let Some(out) = child.stdout.take() {
        pipe_to_log(out, log_path.to_path_buf(), "server");
    }
    if let Some(err) = child.stderr.take() {
        pipe_to_log(err, log_path.to_path_buf(), "server-err");
    }
    // Record how we launched it, for the log view.
    if let Ok(mut f) = open_log_file(log_path) {
        let _ = writeln!(
            f,
            "[shell] starting sidecar: {program} {} (port {port})",
            args.join(" ")
        );
    }
    Ok(RunningSidecar {
        child,
        log_path: log_path.to_path_buf(),
        source: source.clone(),
        port,
    })
}

/// True when `GET <base>/health` answers 200 with a JSON `status: ok`.
pub fn health_ok(base_url: &str) -> bool {
    let url = format!("{base_url}/health");
    match ureq::get(&url)
        .timeout(Duration::from_millis(1000))
        .call()
    {
        Ok(resp) => {
            if resp.status() != 200 {
                return false;
            }
            match resp.into_string() {
                Ok(body) => body.contains("\"status\"") && body.contains("ok"),
                Err(_) => false,
            }
        }
        Err(_) => false,
    }
}

/// Generic wait loop; `check` is called up to `max_attempts` times.
/// Kept injectable so the timeout logic is unit-testable without HTTP.
pub fn wait_until<F: FnMut() -> bool>(
    mut check: F,
    max_attempts: u32,
    interval: Duration,
) -> bool {
    for _ in 0..max_attempts {
        if check() {
            return true;
        }
        std::thread::sleep(interval);
    }
    // One final check after the last sleep, so a server that became
    // healthy during the final interval is not reported as failed.
    check()
}

/// Poll `/health` until it answers or `timeout` elapses.
pub fn wait_for_health(base_url: &str, timeout: Duration, interval: Duration) -> bool {
    let attempts = (timeout.as_millis() / interval.as_millis().max(1)) as u32;
    let start = Instant::now();
    wait_until(
        || health_ok(base_url) || start.elapsed() >= timeout,
        attempts.max(1),
        interval,
    ) && health_ok(base_url)
}

/// Stop a running sidecar: SIGTERM (Unix) / terminate (Windows), wait
/// up to 5 s, then force-kill. Never leaves an orphan behind.
pub fn stop_sidecar(running: &mut RunningSidecar) {
    #[cfg(unix)]
    {
        let pid = running.child.id();
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
    }
    #[cfg(windows)]
    {
        let _ = running.child.kill();
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match running.child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => {
                if Instant::now() >= deadline {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            Err(_) => return,
        }
    }
    let _ = running.child.kill();
    let _ = running.child.wait();
}

/// Shared mutable handle to the sidecar, held in Tauri managed state.
pub type SidecarState = Mutex<Option<RunningSidecar>>;

/// Read the last `lines` lines of the sidecar log (empty if none yet).
pub fn tail_log(log_path: &Path, lines: usize) -> String {
    let Ok(text) = fs::read_to_string(log_path) else {
        return String::new();
    };
    let all: Vec<&str> = text.lines().collect();
    let start = all.len().saturating_sub(lines);
    all[start..].join("\n")
}

trait AsPosix {
    fn as_posix(&self) -> String;
}
impl AsPosix for Path {
    fn as_posix(&self) -> String {
        self.to_string_lossy().replace('\\', "/")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn pick_port_prefers_free_preferred() {
        let probe = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let free = probe.local_addr().unwrap().port();
        drop(probe);
        assert_eq!(pick_port(free), free);
    }

    #[test]
    fn pick_port_falls_back_when_preferred_busy() {
        let busy = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = busy.local_addr().unwrap().port();
        let picked = pick_port(port);
        assert_ne!(picked, port);
        // The fallback port must itself be free right now.
        assert!(TcpListener::bind(("127.0.0.1", picked)).is_ok());
    }

    #[test]
    fn wait_until_succeeds_on_third_try() {
        let n = AtomicUsize::new(0);
        let ok = wait_until(
            || n.fetch_add(1, Ordering::SeqCst) >= 2,
            10,
            Duration::from_millis(1),
        );
        assert!(ok);
        assert_eq!(n.load(Ordering::SeqCst), 3);
    }

    #[test]
    fn wait_until_times_out() {
        let ok = wait_until(|| false, 3, Duration::from_millis(1));
        assert!(!ok);
    }

    #[test]
    fn find_sidecar_in_dirs_finds_exe() {
        let dir = std::env::temp_dir().join("clipforge-test-sidecar-dir");
        let _ = fs::create_dir_all(&dir);
        let exe = dir.join("clipforge-server");
        fs::write(&exe, b"fake").unwrap();
        assert_eq!(find_sidecar_in_dirs(&[dir.clone()]), Some(exe));
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(find_sidecar_in_dirs(&[dir]), None);
    }

    #[test]
    fn sidecar_command_parts_exe() {
        let (prog, args, _) = sidecar_command_parts(
            &SidecarSource::Exe(PathBuf::from("/opt/cf/clipforge-server")),
            8123,
            Path::new("/data"),
        );
        assert_eq!(prog, "/opt/cf/clipforge-server");
        assert_eq!(args, ["--host", "127.0.0.1", "--port", "8123"]);
    }

    #[test]
    fn sidecar_command_parts_dev_python() {
        let (prog, args, workdir) = sidecar_command_parts(
            &SidecarSource::DevPython {
                repo_root: PathBuf::from("/repo"),
            },
            8000,
            Path::new("/data"),
        );
        assert!(prog == "python3" || prog == "python");
        assert_eq!(args[..2], ["-m", "clipforge_api"]);
        assert_eq!(workdir, PathBuf::from("/repo"));
    }

    #[test]
    fn sidecar_env_points_at_data_dir() {
        let env = sidecar_env(Path::new("/tmp/cf-data"), None);
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();
        assert_eq!(map["CLIPFORGE_DATA_DIR"], "/tmp/cf-data");
        assert!(map["DATABASE_URL"].contains("clipforge.db"));
        assert!(!map.contains_key("CLIPFORGE_WEB_OUT"));
    }

    #[test]
    fn sidecar_env_sets_web_out_when_present() {
        let env = sidecar_env(
            Path::new("/tmp/cf-data"),
            Some(Path::new("/tmp/cf-res/binaries/web/out")),
        );
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();
        assert_eq!(map["CLIPFORGE_WEB_OUT"], "/tmp/cf-res/binaries/web/out");
    }

    #[test]
    fn bundled_web_out_found_only_with_index_html() {
        let dir = std::env::temp_dir()
            .join(format!("clipforge-test-web-out-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let out = dir.join("binaries").join("web").join("out");
        fs::create_dir_all(&out).unwrap();
        assert_eq!(bundled_web_out(&dir), None);
        fs::write(out.join("index.html"), "<html></html>").unwrap();
        assert_eq!(bundled_web_out(&dir), Some(out.clone()));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn tail_log_returns_last_lines() {
        let path = std::env::temp_dir().join("clipforge-test-tail.log");
        fs::write(&path, "a\nb\nc\nd\ne\n").unwrap();
        assert_eq!(tail_log(&path, 2), "d\ne");
        assert_eq!(tail_log(&path, 99), "a\nb\nc\nd\ne");
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn health_ok_false_when_nothing_listening() {
        // Port 1 on loopback should refuse (or be closed) in practice.
        assert!(!health_ok("http://127.0.0.1:1"));
    }
}
