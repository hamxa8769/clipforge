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
//! dir. Its stdout/stderr go to a rotating log file.
//!
//! Shutdown kills the WHOLE process tree, never just the direct child:
//! `clipforge-server` is a PyInstaller one-file binary that spawns an
//! inner child process, so killing the parent alone would orphan the
//! real server. Unix spawns the sidecar in its own process group
//! (`setsid`) and shutdown signals the group; Windows puts the sidecar
//! in a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so the OS
//! kills the whole tree when the shell exits (and `stop_sidecar` also
//! terminates the job explicitly). The 5 s graceful wait is kept, then
//! force-kill, then reap — never a half-dead child.
//!
//! The sidecar's pid (+ port) is written to `clipforge-server.pid` in
//! the data dir on spawn and removed on stop. On launch,
//! `cleanup_stale_sidecar` stops a stale server from a previous run
//! (verified by pid file + process identity, and by probing the
//! preferred port's `/health`) before a new sidecar is spawned — so a
//! crash or a forced kill can never leave a server behind that blocks
//! the next start (or the next install).

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
        if dir.join("apps/desktop/src-tauri/tauri.conf.json").is_file() {
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
///
/// `pid_file` is the `clipforge-server.pid` written on spawn and
/// removed on stop (see `cleanup_stale_sidecar`). On Windows `job` is
/// the Job Object handle the sidecar was assigned to; with
/// `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` the OS kills the whole process
/// tree if the shell dies without stopping the sidecar cleanly.
#[allow(dead_code)] // log_path/source/port are diagnostic info, kept for the status view.
pub struct RunningSidecar {
    pub child: Child,
    pub log_path: PathBuf,
    pub source: SidecarSource,
    pub port: u16,
    pub pid_file: PathBuf,
    #[cfg(windows)]
    pub job: Option<usize>,
}

/// What `stop_sidecar_if_running` in `main.rs` needs after the sidecar
/// is stopped: the pid to confirm gone, and the exe path to confirm no
/// longer locked (the updater waits for both before launching the
/// installer on Windows).
#[derive(Debug, Clone)]
pub struct StoppedSidecar {
    pub pid: u32,
    /// `Some` when the sidecar was a real executable (not dev Python).
    pub exe_path: Option<PathBuf>,
}

impl RunningSidecar {
    pub fn stopped_info(&self) -> StoppedSidecar {
        StoppedSidecar {
            pid: self.child.id(),
            exe_path: match &self.source {
                SidecarSource::Exe(p) => Some(p.clone()),
                SidecarSource::DevPython { .. } => None,
            },
        }
    }
}

/// Best-effort safety net: if a `RunningSidecar` is ever dropped
/// without `stop_sidecar` (a code path that forgets to stop it), still
/// signal the tree so the server cannot outlive the shell. No waiting
/// here — a destructor must not block; `stop_sidecar` is the path that
/// waits and reaps.
impl Drop for RunningSidecar {
    fn drop(&mut self) {
        let _ = remove_pid_file_for(&self.pid_file);
        #[cfg(unix)]
        {
            let pid = self.child.id();
            let pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
            if pgid > 0 {
                unsafe { libc::killpg(pgid, libc::SIGTERM) };
            }
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        }
        #[cfg(windows)]
        {
            if let Some(job) = self.job.take() {
                win::terminate_job(job);
                win::close_job(job);
            }
        }
    }
}

/// How long `stop_sidecar` waits for a graceful exit before force-kill.
pub const SIDECAR_STOP_GRACE: Duration = Duration::from_secs(5);

/// Name of the pid file in the data dir.
pub const PID_FILE_NAME: &str = "clipforge-server.pid";

pub fn pid_file_path(data_dir: &Path) -> PathBuf {
    data_dir.join(PID_FILE_NAME)
}

/// Write `"<pid>\n<port>\n"` to the pid file. Best effort for the
/// caller: a failure is logged by the caller, never fatal to a spawn.
pub fn write_pid_file(data_dir: &Path, pid: u32, port: u16) -> std::io::Result<()> {
    if let Some(parent) = pid_file_path(data_dir).parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(pid_file_path(data_dir), format!("{pid}\n{port}\n"))
}

/// Read the pid file. `None` when it is missing or unparseable (a
/// half-written file from a crash is treated as "no pid file").
pub fn read_pid_file(data_dir: &Path) -> Option<(u32, u16)> {
    let text = fs::read_to_string(pid_file_path(data_dir)).ok()?;
    let mut lines = text.lines();
    let pid: u32 = lines.next()?.trim().parse().ok()?;
    let port: u16 = lines.next()?.trim().parse().ok()?;
    Some((pid, port))
}

pub fn remove_pid_file(data_dir: &Path) {
    let _ = fs::remove_file(pid_file_path(data_dir));
}

fn remove_pid_file_for(pid_file: &Path) -> std::io::Result<()> {
    fs::remove_file(pid_file)
}

/// Append one `[shell] …` line to the server log (best effort).
pub fn log_line(log_path: &Path, msg: &str) {
    if let Ok(mut f) = open_log_file(log_path) {
        let _ = writeln!(f, "{msg}");
    }
}

/// True when the process `pid` exists. (An `EPERM` from `kill(pid, 0)`
/// still means "alive" — it exists, we just may not signal it.)
#[cfg(unix)]
pub fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    let r = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if r != 0 {
        return std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM);
    }
    // A zombie still answers kill(pid, 0) but is already dead (our
    // own unreaped child, or a stale server awaiting reap). Treat it
    // as gone: /proc/<pid>/stat state Z (zombie) or X (dead).
    if let Ok(stat) = fs::read_to_string(format!("/proc/{pid}/stat")) {
        // comm is parenthesised and may contain spaces/parens, so
        // the state is the first token after the closing paren.
        if let Some(close) = stat.rfind(')') {
            if let Some(state) = stat[close + 1..].split_whitespace().next() {
                return state != "Z" && state != "X";
            }
        }
    }
    true
}

/// True when the process `pid` exists (Windows: `OpenProcess` with
/// `SYNCHRONIZE` succeeds).
#[cfg(windows)]
pub fn pid_alive(pid: u32) -> bool {
    win::process_alive(pid)
}

/// The process group of `pid` on Unix (its own pid when it is a group
/// leader, e.g. a sidecar spawned with `setsid`). `None` when the
/// process is gone.
#[cfg(unix)]
fn process_group(pid: u32) -> Option<libc::pid_t> {
    let pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
    if pgid > 0 {
        Some(pgid)
    } else {
        None
    }
}

/// Kill a whole process tree: SIGTERM the group, wait up to `grace`,
/// then SIGKILL the group. Returns `true` when the pid is gone
/// afterwards. The pid itself is signalled too, in case it is not its
/// group leader (e.g. a stale server from before process groups).
#[cfg(unix)]
pub fn kill_process_tree(pid: u32, grace: Duration) -> bool {
    if pid == 0 || !pid_alive(pid) {
        return true;
    }
    let pgid = process_group(pid);
    unsafe {
        if let Some(pgid) = pgid {
            libc::killpg(pgid, libc::SIGTERM);
        }
        libc::kill(pid as libc::pid_t, libc::SIGTERM);
    }
    if wait_for_process_gone(pid, grace) {
        return true;
    }
    unsafe {
        if let Some(pgid) = pgid {
            libc::killpg(pgid, libc::SIGKILL);
        }
        libc::kill(pid as libc::pid_t, libc::SIGKILL);
    }
    // One short poll so a slow-to-die tree is not reported gone early.
    wait_for_process_gone(pid, Duration::from_secs(2))
}

/// Kill a whole process tree on Windows: `taskkill /F /T` (tree kill),
/// then wait until the pid is gone. Returns `true` when the pid is
/// gone afterwards.
#[cfg(windows)]
pub fn kill_process_tree(pid: u32, grace: Duration) -> bool {
    if pid == 0 || !pid_alive(pid) {
        return true;
    }
    win::taskkill_tree(pid);
    wait_for_process_gone(pid, grace)
}

/// Wait until `pid` no longer exists (or `timeout` elapses).
/// Unix: poll `kill(pid, 0)`. Windows: `WaitForSingleObject` on an
/// `OpenProcess(SYNCHRONIZE)` handle.
pub fn wait_for_process_gone(pid: u32, timeout: Duration) -> bool {
    #[cfg(unix)]
    {
        let deadline = Instant::now() + timeout;
        loop {
            if !pid_alive(pid) {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    #[cfg(windows)]
    {
        win::wait_for_process_gone(pid, timeout)
    }
}

/// Wait until the file at `path` can be opened for writing (or
/// `timeout` elapses). On Windows a running exe is locked, so this is
/// how the updater proves the installer will not hit a file lock; on
/// Unix the open succeeds immediately.
pub fn wait_until_file_unlocked(path: &Path, timeout: Duration) -> bool {
    wait_until(
        || {
            OpenOptions::new()
                .write(true)
                .open(path)
                .map(|_| true)
                .unwrap_or(false)
        },
        (timeout.as_millis() / 100).max(1) as u32,
        Duration::from_millis(100),
    )
}

/// Windows-only helpers: Job Objects and process waits.
///
/// The signatures here are checked against the `windows-sys 0.61`
/// crate source (see `Cargo.toml`); this module is only compiled on
/// Windows, so it cannot be type-checked on a Linux dev machine —
/// keep it small and literal.
#[cfg(windows)]
pub mod win {
    use std::time::Duration;

    /// `SYNCHRONIZE` access right for `OpenProcess` (waitable handle).
    const SYNCHRONIZE: u32 = 0x0010_0000;

    /// Create a Job Object that kills every process in it when the
    /// last handle to the job is closed. Returns the raw `HANDLE`
    /// (as `usize`) or `None` on failure.
    pub fn create_kill_on_close_job() -> Option<usize> {
        use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
        use windows_sys::Win32::System::JobObjects::*;
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() || job == INVALID_HANDLE_VALUE {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                CloseHandle(job);
                return None;
            }
            Some(job as usize)
        }
    }

    /// Assign a spawned child to a fresh kill-on-close job. Returns
    /// the job handle (as `usize`) or `None` when the job could not
    /// be created / the child could not be assigned.
    pub fn assign_child_to_new_job(child: &std::process::Child) -> Option<usize> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;
        let job = create_kill_on_close_job()?;
        let ok = unsafe {
            AssignProcessToJobObject(job as *mut core::ffi::c_void, child.as_raw_handle())
        };
        if ok == 0 {
            unsafe { CloseHandle(job as *mut core::ffi::c_void) };
            return None;
        }
        Some(job)
    }

    /// Terminate every process in the job (the whole sidecar tree).
    pub fn terminate_job(job: usize) {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;
        unsafe {
            TerminateJobObject(job as *mut core::ffi::c_void, 1);
        }
    }

    /// Close a job handle. When this is the last handle and the job
    /// was created kill-on-close, the OS kills the tree — the safety
    /// net if the shell dies without stopping the sidecar cleanly.
    pub fn close_job(job: usize) {
        use windows_sys::Win32::Foundation::CloseHandle;
        unsafe {
            CloseHandle(job as *mut core::ffi::c_void);
        }
    }

    /// `taskkill /F /T` — force-kill a process tree by pid. Used when
    /// there is no job (job creation failed) and for stale servers
    /// found by pid. Hidden console (`CREATE_NO_WINDOW`).
    pub fn taskkill_tree(pid: u32) {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let _ = std::process::Command::new("taskkill")
            .args(["/F", "/T", "/PID", &pid.to_string()])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }

    /// `taskkill /F /IM clipforge-server.exe` — kill every process
    /// whose image name is exactly our server. The image name is the
    /// identity check, so no unrelated process can match.
    pub fn taskkill_clipforge_server_image() {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let _ = std::process::Command::new("taskkill")
            .args(["/F", "/IM", "clipforge-server.exe"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }

    /// True when the process exists (`OpenProcess(SYNCHRONIZE)`).
    pub fn process_alive(pid: u32) -> bool {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::OpenProcess;
        unsafe {
            let h = OpenProcess(SYNCHRONIZE, 0, pid);
            if h.is_null() {
                return false;
            }
            CloseHandle(h);
            true
        }
    }

    /// Wait until the process is gone (or `timeout` elapses) via
    /// `WaitForSingleObject` on the process handle.
    pub fn wait_for_process_gone(pid: u32, timeout: Duration) -> bool {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject};
        unsafe {
            let h = OpenProcess(SYNCHRONIZE, 0, pid);
            if h.is_null() {
                // No such process (or already gone).
                return true;
            }
            let waited = WaitForSingleObject(h, timeout.as_millis() as u32);
            CloseHandle(h);
            waited == 0 // WAIT_OBJECT_0
        }
    }
}

/// Command line of a process, as separate arguments. Unix: `/proc`.
/// Falls back to `ps` where `/proc` does not exist (macOS).
#[cfg(unix)]
fn process_cmdline(pid: u32) -> Option<Vec<String>> {
    if let Ok(raw) = fs::read(format!("/proc/{pid}/cmdline")) {
        let args = raw
            .split(|&b| b == 0)
            .filter(|s| !s.is_empty())
            .map(|s| String::from_utf8_lossy(s).into_owned())
            .collect::<Vec<_>>();
        if !args.is_empty() {
            return Some(args);
        }
    }
    // No /proc (macOS): `ps -p <pid> -o args=`.
    let out = std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "args="])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let line = text.lines().next()?.trim();
    if line.is_empty() {
        return None;
    }
    Some(vec![line.to_string()])
}

/// True when `pid` is a ClipForge server process: its command line
/// names `clipforge-server` (the packaged exe, any platform) or
/// `clipforge_api` (the dev-Python fallback). Never true for an
/// unrelated process — this is the guard that stops the stale cleanup
/// from killing something that is not ours.
pub fn pid_is_clipforge_server(pid: u32) -> bool {
    #[cfg(unix)]
    {
        let Some(args) = process_cmdline(pid) else {
            return false;
        };
        args.iter()
            .any(|a| a.contains("clipforge-server") || a.contains("clipforge_api"))
    }
    #[cfg(windows)]
    {
        // `tasklist` by pid; the image name is the identity check.
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let Ok(out) = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
            .creation_flags(CREATE_NO_WINDOW)
            .output()
        else {
            return false;
        };
        String::from_utf8_lossy(&out.stdout)
            .to_ascii_lowercase()
            .contains("clipforge-server")
    }
}

/// Find a ClipForge server process that was started with `--port
/// <port>` (Unix: scan `/proc` command lines). `None` when no such
/// process exists — e.g. on macOS, where there is no `/proc` and the
/// pid-file path is the only way to find a stale server.
#[cfg(unix)]
pub fn find_clipforge_pid_for_port(port: u16) -> Option<u32> {
    let wanted = port.to_string();
    let entries = fs::read_dir("/proc").ok()?;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Ok(pid) = name.to_string_lossy().parse::<u32>() else {
            continue;
        };
        let Some(args) = process_cmdline(pid) else {
            continue;
        };
        let is_server = args
            .iter()
            .any(|a| a.contains("clipforge-server") || a.contains("clipforge_api"));
        if !is_server {
            continue;
        }
        if args.windows(2).any(|w| w[0] == "--port" && w[1] == wanted) {
            return Some(pid);
        }
    }
    None
}

/// True when `GET <base>/health` answers 200 with the ClipForge
/// status payload (`status: ok` AND a `version` field — our
/// `/health` returns `{"status": "ok", "dry_run": …, "version": …}`).
/// A random other server on the port does not answer with our
/// payload, so this is the "verifiably ours" check for the stale
/// port probe.
pub fn health_is_clipforge(base_url: &str) -> bool {
    match health_body(base_url) {
        Some(body) => {
            body.contains("\"status\"") && body.contains("ok") && body.contains("\"version\"")
        }
        None => false,
    }
}

fn health_body(base_url: &str) -> Option<String> {
    let url = format!("{base_url}/health");
    match ureq::get(&url).timeout(Duration::from_millis(1000)).call() {
        Ok(resp) => {
            if resp.status() != 200 {
                return None;
            }
            resp.into_string().ok()
        }
        Err(_) => None,
    }
}

/// Stop a stale server left by a previous run, before a new sidecar
/// is spawned. Two independent checks, both logged to the server log:
///
/// 1. Pid file: if the recorded pid is alive AND is a ClipForge
///    server process, terminate its whole tree. Otherwise the pid
///    file is just removed (stale pid, or pid reuse by another
///    program — never killed on a pid alone).
/// 2. Port probe: for each candidate port, if something answers
///    `/health` with our status payload, it is a ClipForge server
///    that is not ours — find it (Unix: `/proc` scan for the
///    `--port` argument; Windows: the `clipforge-server.exe` image
///    name) and terminate it, then wait until the port stops
///    answering.
///
/// Nothing is killed unless it is verifiably a ClipForge server.
pub fn cleanup_stale_sidecar(data_dir: &Path, ports: &[u16], log_path: &Path) {
    if let Some((pid, port)) = read_pid_file(data_dir) {
        if pid_alive(pid) && pid_is_clipforge_server(pid) {
            log_line(
                log_path,
                &format!(
                    "[shell] stale clipforge-server from a previous run (pid {pid}, port {port}) — stopping its process tree"
                ),
            );
            let gone = kill_process_tree(pid, SIDECAR_STOP_GRACE);
            if gone {
                log_line(
                    log_path,
                    &format!("[shell] stale clipforge-server (pid {pid}) stopped"),
                );
            } else {
                log_line(
                    log_path,
                    &format!(
                        "[shell] WARNING: could not stop stale clipforge-server (pid {pid}); it may still be running"
                    ),
                );
            }
        } else {
            log_line(
                log_path,
                &format!(
                    "[shell] removing stale pid file (pid {pid} is no longer a clipforge-server)"
                ),
            );
        }
        remove_pid_file(data_dir);
    }

    for &port in ports {
        let base = format!("http://127.0.0.1:{port}");
        if !health_is_clipforge(&base) {
            continue;
        }
        log_line(
            log_path,
            &format!(
                "[shell] a ClipForge server is already answering /health on port {port} — stopping it before starting a new one"
            ),
        );
        #[cfg(unix)]
        {
            match find_clipforge_pid_for_port(port) {
                Some(pid) => {
                    let gone = kill_process_tree(pid, SIDECAR_STOP_GRACE);
                    log_line(
                        log_path,
                        &format!(
                            "[shell] stale server on port {port} (pid {pid}) stopped: {gone}"
                        ),
                    );
                }
                None => log_line(
                    log_path,
                    &format!(
                        "[shell] WARNING: could not find the pid of the stale server on port {port}; leaving it alone"
                    ),
                ),
            }
        }
        #[cfg(windows)]
        {
            // Image name is the identity check: only our exe matches.
            win::taskkill_clipforge_server_image();
            log_line(
                log_path,
                &format!("[shell] sent taskkill to clipforge-server.exe for port {port}"),
            );
        }
        // Wait until the port stops answering as our server.
        let stopped = wait_until(
            || !health_is_clipforge(&base),
            50,
            Duration::from_millis(100),
        );
        if !stopped {
            log_line(
                log_path,
                &format!(
                    "[shell] WARNING: a ClipForge server is still answering /health on port {port} after cleanup"
                ),
            );
        }
    }
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

fn pipe_to_log<R: std::io::Read + Send + 'static>(reader: R, log_path: PathBuf, tag: &'static str) {
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
        let mut paths: Vec<PathBuf> =
            vec![repo_root.join("apps").join("api"), repo_root.join("src")];
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
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Own process group, so shutdown can signal the whole tree
        // (the PyInstaller one-file launcher + its inner child) with
        // one killpg — the child becomes its group leader, pgid == pid.
        // SAFETY: setsid() cannot fail in pre_exec in a way that
        // matters here (the child has no controlling terminal to lose
        // and is not already a group leader); ignore its result.
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
    }
    let mut child = cmd.spawn()?;
    #[cfg(windows)]
    let mut job: Option<usize> = None;
    #[cfg(windows)]
    {
        match win::assign_child_to_new_job(&child) {
            Some(h) => job = Some(h),
            // No job: tree-kill still works via taskkill /T in
            // stop_sidecar, but a shell crash could orphan the tree.
            None => log_line(
                log_path,
                "[shell] WARNING: could not put the sidecar in a Job Object; tree-kill falls back to taskkill /T",
            ),
        }
    }
    let pid = child.id();
    // Record the pid (+ port) so a later launch can stop this server
    // if the shell dies without stopping it (see
    // cleanup_stale_sidecar). A write failure is logged, never fatal.
    if let Err(e) = write_pid_file(data_dir, pid, port) {
        log_line(
            log_path,
            &format!("[shell] WARNING: could not write pid file: {e}"),
        );
    }
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
            "[shell] starting sidecar: {program} {} (port {port}, pid {pid})",
            args.join(" ")
        );
    }
    Ok(RunningSidecar {
        child,
        log_path: log_path.to_path_buf(),
        source: source.clone(),
        port,
        pid_file: pid_file_path(data_dir),
        #[cfg(windows)]
        job,
    })
}

/// True when `GET <base>/health` answers 200 with a JSON `status: ok`.
pub fn health_ok(base_url: &str) -> bool {
    match health_body(base_url) {
        Some(body) => body.contains("\"status\"") && body.contains("ok"),
        None => false,
    }
}

/// Generic wait loop; `check` is called up to `max_attempts` times.
/// Kept injectable so the timeout logic is unit-testable without HTTP.
pub fn wait_until<F: FnMut() -> bool>(mut check: F, max_attempts: u32, interval: Duration) -> bool {
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

/// Stop a running sidecar — the WHOLE process tree, never just the
/// direct child.
///
/// * Unix: SIGTERM the process group (the sidecar was spawned with
///   `setsid`, so the group is the launcher + its inner child),
///   wait up to 5 s, then SIGKILL the group.
/// * Windows: `TerminateJobObject` on the kill-on-close job (the
///   whole tree dies at once), falling back to `taskkill /F /T`
///   when there is no job; then `child.kill()` as a last resort.
///
/// Always waits/reaps the direct child afterwards, closes the job
/// handle, and removes the pid file. Never leaves a half-dead child.
pub fn stop_sidecar(running: &mut RunningSidecar) {
    #[cfg(unix)]
    {
        let pid = running.child.id();
        if let Some(pgid) = process_group(pid) {
            unsafe { libc::killpg(pgid, libc::SIGTERM) };
        }
        // Also signal the child itself, in case it is not its group
        // leader (e.g. spawned before process groups existed).
        unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
    }
    #[cfg(windows)]
    let mut job: Option<usize> = None;
    #[cfg(windows)]
    {
        if let Some(j) = running.job.take() {
            win::terminate_job(j);
            job = Some(j);
        } else {
            win::taskkill_tree(running.child.id());
        }
    }
    let deadline = Instant::now() + SIDECAR_STOP_GRACE;
    loop {
        match running.child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {
                if Instant::now() >= deadline {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            Err(_) => break,
        }
    }
    // Grace expired and the child is still alive: force-kill the
    // whole tree, then the child itself, then reap.
    if running
        .child
        .try_wait()
        .map(|s| s.is_none())
        .unwrap_or(false)
    {
        #[cfg(unix)]
        {
            let pid = running.child.id();
            if let Some(pgid) = process_group(pid) {
                unsafe { libc::killpg(pgid, libc::SIGKILL) };
            }
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGKILL) };
        }
        #[cfg(windows)]
        {
            if job.is_none() {
                win::taskkill_tree(running.child.id());
            }
        }
        let _ = running.child.kill();
    }
    let _ = running.child.wait();
    #[cfg(windows)]
    {
        if let Some(j) = job {
            win::close_job(j);
        }
    }
    let _ = remove_pid_file_for(&running.pid_file);
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
        let dir =
            std::env::temp_dir().join(format!("clipforge-test-web-out-{}", std::process::id()));
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

    #[test]
    fn health_is_clipforge_false_when_nothing_listening() {
        assert!(!health_is_clipforge("http://127.0.0.1:1"));
    }

    #[test]
    fn pid_file_roundtrip_and_remove() {
        let dir =
            std::env::temp_dir().join(format!("clipforge-test-pidfile-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        assert_eq!(read_pid_file(&dir), None);
        write_pid_file(&dir, 4242, 8123).unwrap();
        assert_eq!(read_pid_file(&dir), Some((4242, 8123)));
        // The pid file lives next to the data dir contents.
        assert!(pid_file_path(&dir).is_file());
        remove_pid_file(&dir);
        assert_eq!(read_pid_file(&dir), None);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn pid_file_malformed_is_none() {
        let dir =
            std::env::temp_dir().join(format!("clipforge-test-pidfile-bad-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(pid_file_path(&dir), "not-a-pid\n8000\n").unwrap();
        assert_eq!(read_pid_file(&dir), None);
        fs::write(pid_file_path(&dir), "1234\nnot-a-port\n").unwrap();
        assert_eq!(read_pid_file(&dir), None);
        fs::write(pid_file_path(&dir), "1234\n").unwrap();
        assert_eq!(read_pid_file(&dir), None);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn pid_is_clipforge_server_false_for_this_test_process() {
        // The test runner is not a clipforge-server, whatever its path.
        assert!(!pid_is_clipforge_server(std::process::id()));
        assert!(!pid_is_clipforge_server(1));
    }

    #[test]
    fn wait_until_file_unlocked_true_for_plain_file() {
        let path =
            std::env::temp_dir().join(format!("clipforge-test-unlocked-{}", std::process::id()));
        fs::write(&path, b"x").unwrap();
        assert!(wait_until_file_unlocked(&path, Duration::from_secs(1)));
        let _ = fs::remove_file(&path);
        // A missing file cannot be opened for writing either — but the
        // updater only ever probes the real exe path, which exists.
        assert!(!wait_until_file_unlocked(&path, Duration::from_millis(300)));
    }

    /// A process whose argv[0] is `clipforge-server` and whose args
    /// carry `--port <port>` is found by the stale-port scan — and
    /// `kill_process_tree` takes it down.
    #[test]
    #[cfg(unix)]
    fn find_clipforge_pid_for_port_finds_server_by_argv() {
        use std::os::unix::process::CommandExt;
        use std::process::Command;

        // Unique port per test-process run, so a leftover fake server
        // from an earlier (failed) run can never match this scan.
        let port: u16 = 18700 + (std::process::id() % 200) as u16;
        let script = format!(
            "exec -a clipforge-server python3 -c \"import time; time.sleep(60)\" --port {port}"
        );
        // Own process group (setsid in pre_exec), so the tree kill
        // below cannot touch the test runner's group.
        let mut cmd = Command::new("bash");
        cmd.args(["-c", &script])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let mut child = cmd.spawn().expect("spawn fake clipforge-server");
        let pid = child.id();
        // The child starts as bash and execs into python3: poll
        // briefly until the exec has happened and the scan sees it.
        let found = wait_until(
            || find_clipforge_pid_for_port(port).is_some(),
            50,
            Duration::from_millis(100),
        );
        assert!(found, "port scan should find the fake server");
        assert_eq!(find_clipforge_pid_for_port(port), Some(pid));
        assert!(pid_is_clipforge_server(pid));
        assert!(pid_alive(pid));
        assert!(kill_process_tree(pid, Duration::from_secs(5)));
        assert!(!pid_alive(pid));
        assert_eq!(find_clipforge_pid_for_port(port), None);
        let _ = child.wait();
    }
}
