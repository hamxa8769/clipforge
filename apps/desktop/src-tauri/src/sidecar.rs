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
//! dir. Its stdout/stderr go to a log file that is **rotated on every
//! launch** (previous launch kept as `clipforge-server.log.1`) and
//! headed by a `[shell] ==== ClipForge start <timestamp>, port <port>
//! ====` separator, with every line timestamped — so the splash page
//! can never again show a stale `/yt-dlp/status` line from a previous
//! run as if the current server were alive (v0.8.2).
//!
//! Startup health wait (v0.8.2): the frozen PyInstaller one-file exe
//! extracts its whole archive on every launch, so a cold start can
//! take well over 30 s. The shell therefore waits up to
//! [`HEALTH_WAIT_TIMEOUT`] (120 s) and distinguishes the cases: a
//! server process that is alive but not answering yet keeps being
//! waited on (with progress), while a process that *exited* fails
//! fast with its exit code and the bind-error / traceback from the
//! log — never the generic "did not answer within 30 s" message, and
//! never by killing a slow-but-healthy server.
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
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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

/// How long the shell waits for a freshly spawned server to answer
/// `/health` before giving up. The frozen PyInstaller one-file exe
/// extracts its whole archive to `%TEMP%` on every launch and imports
/// under Defender scanning, so a cold start on a slow machine can
/// take well over the old 30 s — and killing a healthy-but-slow
/// server at 30 s just repeated the same doomed cycle on Retry.
/// 120 s is the wait; a server that *exits* still fails fast (see
/// [`wait_for_health_running`]) — the timeout only bounds waiting on
/// a process that is still alive.
pub const HEALTH_WAIT_TIMEOUT: Duration = Duration::from_secs(120);

/// Marker fragment of the separator line [`begin_launch_log`] writes
/// at the top of a fresh log on every launch. [`tail_log_since_launch`]
/// uses it to show only this launch's lines.
pub const LAUNCH_MARKER: &str = "==== ClipForge start ";

/// Format seconds since the Unix epoch as an ISO-8601 UTC timestamp
/// (`2026-11-23T02:55:10Z`). Std-only (no chrono dependency):
/// civil-from-days conversion. Pure, so it is unit-testable.
pub fn format_utc_timestamp(secs_since_epoch: u64) -> String {
    let days = (secs_since_epoch / 86_400) as i64;
    let secs_of_day = secs_since_epoch % 86_400;
    // Howard Hinnant's civil-from-days algorithm.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        secs_of_day / 3600,
        (secs_of_day % 3600) / 60,
        secs_of_day % 60
    )
}

/// Current UTC timestamp for log lines.
pub fn timestamp_now() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format_utc_timestamp(secs)
}

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

/// Append one `[shell] <timestamp> …` line to the server log (best
/// effort). Every shell line carries a UTC timestamp so a stale line
/// can never be mistaken for a live one. Callers pass messages
/// starting with `"[shell] "`; the timestamp goes after that prefix.
pub fn log_line(log_path: &Path, msg: &str) {
    if let Ok(mut f) = open_log_file(log_path) {
        let line = match msg.strip_prefix("[shell] ") {
            Some(rest) => format!("[shell] {} {rest}", timestamp_now()),
            None => format!("[shell] {} {msg}", timestamp_now()),
        };
        let _ = writeln!(f, "{line}");
    }
}

/// Rotate the log for a new launch and head it with a separator:
///
/// ```text
/// [shell] ==== ClipForge start 2026-11-23T02:55:10Z, port 8000 ====
/// ```
///
/// The previous launch's log is kept as one backup
/// (`clipforge-server.log.1`, replacing any older backup) — the old
/// lines are never mixed into the new launch's view. Call this once
/// per launch, *before* the stale cleanup, so the cleanup lines land
/// in the fresh log. Best effort: a failure is not fatal to the
/// launch (the old append-only behaviour is the fallback).
pub fn begin_launch_log(log_path: &Path, port: u16) {
    if log_path.is_file() {
        let bak = log_path.with_extension("log.1");
        let _ = fs::remove_file(&bak);
        let _ = fs::rename(log_path, &bak);
    }
    if let Some(parent) = log_path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut f) = fs::File::create(log_path) {
        let _ = writeln!(
            f,
            "[shell] {LAUNCH_MARKER}{}, port {port} ====",
            timestamp_now()
        );
        let _ = f.flush();
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

    /// Pid of the process listening on `127.0.0.1:port` (TCP), via
    /// `netstat -ano`. `None` when nothing listens there or the
    /// output cannot be parsed. Used by the stale cleanup to
    /// attribute a listener to a pid before killing it — the
    /// Windows equivalent of the Unix `/proc` scan.
    pub fn pid_listening_on_port(port: u16) -> Option<u32> {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let out = std::process::Command::new("netstat")
            .args(["-ano", "-p", "TCP"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .output()
            .ok()?;
        let text = String::from_utf8_lossy(&out.stdout);
        let wanted = format!("127.0.0.1:{port}");
        for line in text.lines() {
            // "  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    1234"
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 5
                && parts[0] == "TCP"
                && parts[1] == wanted
                && parts[3] == "LISTENING"
            {
                return parts[4].parse().ok();
            }
        }
        None
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

/// True when something accepts a plain TCP connection on
/// `127.0.0.1:port` — no HTTP involved, so it also fires for a
/// server whose `/health` hangs. This is the fallback signal for
/// the stale cleanup (v0.8.2): a stale server whose `/health` never
/// answers must not be left holding the port just because the
/// `/health` probe timed out. It is also the "listening but not
/// ready yet" signal for the startup progress display.
pub fn port_is_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok()
}

/// Pid of the ClipForge server listening on `port`, if we can find
/// it. Unix: scan `/proc` for the `--port` argument (see
/// [`find_clipforge_pid_for_port`]). Windows has no `/proc`, so the
/// pid file is the only port→pid map — and only when it names a live
/// `clipforge-server.exe` for exactly this port.
pub fn clipforge_pid_for_port(port: u16, data_dir: &Path) -> Option<u32> {
    #[cfg(unix)]
    {
        let _ = data_dir;
        find_clipforge_pid_for_port(port)
    }
    #[cfg(windows)]
    {
        match read_pid_file(data_dir) {
            Some((pid, p)) if p == port && pid_alive(pid) && pid_is_clipforge_server(pid) => {
                Some(pid)
            }
            _ => None,
        }
    }
}

/// Stop a stale server left by a previous run, before a new sidecar
/// is spawned. Two independent checks, both logged to the server log:
///
/// 1. Pid file: if the recorded pid is alive AND is a ClipForge
///    server process, terminate its whole tree — regardless of
///    whether `/health` answers (v0.8.2: a stale server whose
///    `/health` hangs is still a stale server). Otherwise the pid
///    file is just removed (stale pid, or pid reuse by another
///    program — never killed on a pid alone).
/// 2. Port probe: for each candidate port, if something answers
///    `/health` with our status payload, it is a ClipForge server
///    that is not ours — find it (Unix: `/proc` scan for the
///    `--port` argument; Windows: the `clipforge-server.exe` image
///    name) and terminate it, then wait until the port stops
///    answering. A port that merely accepts TCP connections but
///    never answers `/health` as ClipForge is treated the same way
///    when its pid can be verified as a ClipForge server (v0.8.2:
///    the old code only acted when `/health` answered, so a stale
///    server with a hung `/health` kept the port forever). When no
///    pid can be verified, the listener is left alone and the
///    failure is logged — killing an unverified process would be
///    worse than a bind error the new server reports honestly.
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
        let healthy = health_is_clipforge(&base);
        // TCP fallback (v0.8.2): a stale server whose /health hangs
        // never answers the probe above, but it still holds the port
        // — a plain connect is the only signal left.
        let listening = !healthy && port_is_open(port);
        if !healthy && !listening {
            continue;
        }
        if healthy {
            log_line(
                log_path,
                &format!(
                    "[shell] a ClipForge server is already answering /health on port {port} — stopping it before starting a new one"
                ),
            );
        } else {
            log_line(
                log_path,
                &format!(
                    "[shell] something is listening on port {port} but is not answering /health as ClipForge — checking whether it is a stale clipforge-server"
                ),
            );
        }
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
                        "[shell] WARNING: could not find the pid of the server on port {port}; leaving it alone — the new server may fail to bind"
                    ),
                ),
            }
        }
        #[cfg(windows)]
        {
            // Attribute the listener before killing: `netstat -ano`
            // maps the listening socket to its pid, and the pid is
            // only killed when it is verifiably our exe. When
            // attribution fails but /health answered with our payload,
            // the listener IS a ClipForge server, so the image-name
            // kill is the fallback; when it merely listens and cannot
            // be attributed, it is left alone — killing an unrelated
            // process would be worse than a bind error the new server
            // then reports honestly.
            match win::pid_listening_on_port(port) {
                Some(pid) if pid_is_clipforge_server(pid) => {
                    win::taskkill_tree(pid);
                    log_line(
                        log_path,
                        &format!(
                            "[shell] stale server on port {port} (pid {pid}) stopped via taskkill"
                        ),
                    );
                }
                _ if healthy => {
                    // Image name is the identity check: only our exe matches.
                    win::taskkill_clipforge_server_image();
                    log_line(
                        log_path,
                        &format!("[shell] sent taskkill to clipforge-server.exe for port {port}"),
                    );
                }
                _ => log_line(
                    log_path,
                    &format!(
                        "[shell] WARNING: could not attribute the listener on port {port} to a clipforge-server process; leaving it alone — the new server may fail to bind"
                    ),
                ),
            }
        }
        // Wait until the port stops answering as our server AND stops
        // accepting TCP connections.
        let stopped = wait_until(
            || !health_is_clipforge(&base) && !port_is_open(port),
            50,
            Duration::from_millis(100),
        );
        if !stopped {
            log_line(
                log_path,
                &format!(
                    "[shell] WARNING: something is still listening on port {port} after cleanup — the new server may fail to bind"
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
                // Timestamped, so a line from a previous run can never
                // be mistaken for this run's output.
                let _ = writeln!(file, "[{tag}] {} {line}", timestamp_now());
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
            "[shell] {} starting sidecar: {program} {} (port {port}, pid {pid})",
            timestamp_now(),
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

/// What waiting for a freshly spawned server to answer `/health`
/// ended in. The old code collapsed all three cases into one bool
/// and one generic "did not answer within 30 s" message — which is
/// exactly the message that hid a crashed server behind a
/// slow-start story (v0.8.2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HealthWaitOutcome {
    /// `/health` answered 200 with `status: ok`.
    Ready,
    /// The server process exited before ever answering `/health`:
    /// fail fast, with its exit code and the most useful line(s)
    /// from its log — never a timeout message for a dead process.
    Exited {
        code: Option<i32>,
        hint: Option<String>,
    },
    /// The whole [`HEALTH_WAIT_TIMEOUT`] passed with the process
    /// still alive but `/health` still unanswered: a genuinely slow
    /// (or stuck) start, not a crash. The caller must NOT kill the
    /// server on this outcome — it may still come up, and Retry
    /// re-waits on it instead of restarting it.
    TimedOut { elapsed_secs: u64 },
}

/// Pull the most useful failure line(s) out of a server log tail, so
/// a failed start can say *why* instead of "did not answer".
/// Pure, so it is unit-testable. `None` when the tail says nothing
/// specific — the caller then falls back to the generic hint.
pub fn extract_failure_hint(log_tail: &str, port: u16) -> Option<String> {
    let lower = log_tail.to_lowercase();
    if lower.contains("address already in use") {
        return Some(format!(
            "The server could not bind port {port}: address already in use — \
             another process is holding the port. Close it (or pick a \
             different port in the splash page's status panel) and press Retry."
        ));
    }
    if lower.contains("traceback") {
        // The last traceback block: the exception line at its end is
        // the actual error; keep at most the last 4 lines so the
        // splash stays readable.
        let lines: Vec<&str> = log_tail.lines().collect();
        let start = lines
            .iter()
            .rposition(|l| l.to_lowercase().contains("traceback"))
            .unwrap_or(0);
        let block = &lines[start..];
        let keep = block.len().saturating_sub(4);
        return Some(format!(
            "The server crashed during startup:\n{}",
            block[keep..].join("\n")
        ));
    }
    None
}

/// Poll `/health` until it answers, the server process exits, or
/// `timeout` elapses — the three outcomes are kept apart (see
/// [`HealthWaitOutcome`]).
///
/// `on_progress` is called about once per `interval` with the
/// elapsed wait and whether the port already accepts TCP connections
/// (`listening`): the splash uses it to show honest progress —
/// "server is listening, waiting for it to finish starting" vs
/// "server process is running, waiting for it to listen".
pub fn wait_for_health_running(
    running: &mut RunningSidecar,
    timeout: Duration,
    interval: Duration,
    on_progress: &mut dyn FnMut(Duration, bool),
) -> HealthWaitOutcome {
    let base_url = format!("http://127.0.0.1:{}", running.port);
    let start = Instant::now();
    loop {
        if health_ok(&base_url) {
            return HealthWaitOutcome::Ready;
        }
        match running.child.try_wait() {
            Ok(Some(status)) => {
                // Fail FAST: the process is dead; no point waiting
                // out the timeout for a server that will never answer.
                let hint = extract_failure_hint(&tail_log(&running.log_path, 40), running.port);
                return HealthWaitOutcome::Exited {
                    code: status.code(),
                    hint,
                };
            }
            Ok(None) => {}
            Err(_) => {
                let hint = extract_failure_hint(&tail_log(&running.log_path, 40), running.port);
                return HealthWaitOutcome::Exited { code: None, hint };
            }
        }
        let elapsed = start.elapsed();
        if elapsed >= timeout {
            return HealthWaitOutcome::TimedOut {
                elapsed_secs: elapsed.as_secs(),
            };
        }
        on_progress(elapsed, port_is_open(running.port));
        std::thread::sleep(interval);
    }
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

/// Read the last `lines` lines **of the current launch only**: lines
/// from the last [`LAUNCH_MARKER`] separator onward (falling back to
/// the last `[shell] … starting sidecar` line for logs written before
/// v0.8.2, and to [`tail_log`] when there is no marker at all). This
/// is what the splash page shows — a `/yt-dlp/status` line from a
/// previous run can never again look like a live server (v0.8.2).
pub fn tail_log_since_launch(log_path: &Path, lines: usize) -> String {
    let Ok(text) = fs::read_to_string(log_path) else {
        return String::new();
    };
    let all: Vec<&str> = text.lines().collect();
    let start = all
        .iter()
        .rposition(|l| l.contains(LAUNCH_MARKER))
        .or_else(|| all.iter().rposition(|l| l.contains("starting sidecar:")));
    let Some(start) = start else {
        return tail_log(log_path, lines);
    };
    let section = &all[start..];
    let skip = section.len().saturating_sub(lines);
    section[skip..].join("\n")
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
        // The on-disk name is platform-specific (clipforge-server.exe
        // on Windows, clipforge-server elsewhere) — the old test
        // hardcoded the Unix name, so it never found the file on
        // Windows.
        let exe = dir.join(exe_names().into_iter().next().expect("exe_names"));
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

    #[test]
    fn format_utc_timestamp_known_dates() {
        // Values verified against `date -u -d @<secs>`.
        assert_eq!(format_utc_timestamp(0), "1970-01-01T00:00:00Z");
        assert_eq!(format_utc_timestamp(1_700_000_000), "2023-11-14T22:13:20Z");
        assert_eq!(format_utc_timestamp(1_763_940_000), "2025-11-23T23:20:00Z");
        // Leap days, including the divisible-by-400 century rule.
        assert_eq!(format_utc_timestamp(1_709_164_800), "2024-02-29T00:00:00Z");
        assert_eq!(format_utc_timestamp(951_782_400), "2000-02-29T00:00:00Z");
    }

    #[test]
    fn begin_launch_log_rotates_and_marks_launch() {
        let dir =
            std::env::temp_dir().join(format!("clipforge-test-launch-log-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let log = dir.join("clipforge-server.log");
        let bak = dir.join("clipforge-server.log.1");
        fs::write(&log, "stale line from a previous run\n").unwrap();

        begin_launch_log(&log, 8123);
        assert_eq!(
            fs::read_to_string(&bak).unwrap(),
            "stale line from a previous run\n"
        );
        let fresh = fs::read_to_string(&log).unwrap();
        assert!(fresh.starts_with("[shell] ==== ClipForge start "));
        assert!(fresh.contains(", port 8123 ===="));

        // A second launch rotates the first launch's log into the backup.
        begin_launch_log(&log, 8123);
        assert!(fs::read_to_string(&bak)
            .unwrap()
            .contains("==== ClipForge start "));
        assert!(fs::read_to_string(&log)
            .unwrap()
            .starts_with("[shell] ==== ClipForge start "));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn tail_log_since_launch_ignores_stale_lines() {
        let path = std::env::temp_dir().join("clipforge-test-since-launch.log");
        fs::write(
            &path,
            "[server] 2026-01-01T00:00:00Z GET /yt-dlp/status 200\n\
             [shell] ==== ClipForge start 2026-11-23T02:00:00Z, port 8000 ====\n\
             [shell] 2026-11-23T02:00:01Z starting sidecar: clipforge-server --port 8000 (port 8000, pid 1)\n\
             [server] 2026-11-23T02:00:02Z new line 1\n\
             [server] 2026-11-23T02:00:03Z new line 2\n",
        )
        .unwrap();
        // The stale /yt-dlp/status line from the previous run is gone.
        let tail = tail_log_since_launch(&path, 99);
        assert!(!tail.contains("yt-dlp/status"));
        assert!(tail.contains("==== ClipForge start "));
        assert!(tail.contains("new line 2"));
        assert_eq!(
            tail_log_since_launch(&path, 2),
            "[server] 2026-11-23T02:00:02Z new line 1\n[server] 2026-11-23T02:00:03Z new line 2"
        );
        // No separator at all: falls back to a plain tail.
        fs::write(&path, "a\nb\nc\n").unwrap();
        assert_eq!(tail_log_since_launch(&path, 2), "b\nc");
        // Pre-v0.8.2 log: no separator, but a starting-sidecar line.
        fs::write(
            &path,
            "old line\n[shell] starting sidecar: clipforge-server --port 8000 (port 8000, pid 1)\nnew line\n",
        )
        .unwrap();
        assert_eq!(
            tail_log_since_launch(&path, 99),
            "[shell] starting sidecar: clipforge-server --port 8000 (port 8000, pid 1)\nnew line"
        );
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn extract_failure_hint_bind_error() {
        let tail = "[server-err] OSError: [Errno 98] Address already in use\n";
        let hint = extract_failure_hint(tail, 8123).expect("bind error must be recognised");
        assert!(hint.contains("address already in use"));
        assert!(hint.contains("8123"));
    }

    #[test]
    fn extract_failure_hint_traceback() {
        let tail = "INFO: Started server process\n\
             Traceback (most recent call last):\n  \
             File \"clipforge_api/__main__.py\", line 42, in main\n    \
             raise ValueError(\"bad config\")\n\
             ValueError: bad config\n";
        let hint = extract_failure_hint(tail, 8000).expect("traceback must be recognised");
        assert!(hint.contains("ValueError: bad config"));
        assert!(hint.contains("crashed during startup"));
    }

    #[test]
    fn extract_failure_hint_none_for_plain_log() {
        assert_eq!(
            extract_failure_hint("INFO: Started server process [123]\n", 8000),
            None
        );
        assert_eq!(extract_failure_hint("", 8000), None);
    }

    #[test]
    fn port_is_open_true_for_listener_and_false_for_closed() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(port_is_open(port));
        drop(listener);
        // Port 1 on loopback should refuse (or be closed) in practice.
        assert!(!port_is_open(1));
    }

    /// Regression test for the v0.8.1 startup failure: a server that
    /// exits immediately must fail FAST (exit code + log hint), not
    /// after the whole 120 s health timeout with a generic message.
    #[test]
    fn wait_for_health_running_fails_fast_when_child_exits() {
        let dir =
            std::env::temp_dir().join(format!("clipforge-test-fail-fast-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let log = dir.join("clipforge-server.log");
        fs::write(
            &log,
            "[server-err] OSError: [Errno 98] Address already in use\n",
        )
        .unwrap();
        // A port with nothing listening on it.
        let probe = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = probe.local_addr().unwrap().port();
        drop(probe);

        #[cfg(unix)]
        let mut cmd = {
            use std::os::unix::process::CommandExt;
            let mut cmd = std::process::Command::new("/bin/sh");
            cmd.args(["-c", "exit 3"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            unsafe {
                cmd.pre_exec(|| {
                    libc::setsid();
                    Ok(())
                });
            }
            cmd
        };
        #[cfg(windows)]
        let mut cmd = {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            let mut cmd = std::process::Command::new("cmd");
            cmd.args(["/C", "exit 3"])
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            cmd
        };
        let child = cmd.spawn().expect("spawn exiting child");
        let mut running = RunningSidecar {
            child,
            log_path: log.clone(),
            source: SidecarSource::DevPython {
                repo_root: PathBuf::from("/tmp"),
            },
            port,
            pid_file: dir.join("clipforge-server.pid"),
            #[cfg(windows)]
            job: None,
        };
        let started = Instant::now();
        let outcome = wait_for_health_running(
            &mut running,
            Duration::from_secs(120),
            Duration::from_millis(50),
            &mut |_, _| {},
        );
        // Fail-fast: milliseconds, not the 120 s timeout.
        assert!(started.elapsed() < Duration::from_secs(10));
        match outcome {
            HealthWaitOutcome::Exited { code, hint } => {
                assert_eq!(code, Some(3));
                let hint = hint.expect("bind-error hint from the log");
                assert!(hint.contains("address already in use"));
            }
            other => panic!("expected Exited, got {other:?}"),
        }
        stop_sidecar(&mut running);
        let _ = fs::remove_dir_all(&dir);
    }

    /// Regression test for the other half of the v0.8.1 failure: a
    /// server that takes a while to answer must be waited on — and
    /// the progress callback must report it as *listening* before
    /// `/health` answers, so the splash can say "listening, waiting
    /// for it to finish starting" instead of looking hung.
    #[test]
    fn wait_for_health_running_ready_when_server_appears_late() {
        use std::io::Read;
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::Arc;

        // Fake server: binds immediately (so the port is open) but
        // only answers /health 1500 ms after the first request, the
        // way a slow PyInstaller cold start behaves. The hold MUST
        // exceed the 1000 ms probe timeout in health_body: with a
        // shorter hold the very first probe succeeds, wait_for_health
        // returns Ready before any progress tick runs, and the test
        // can never observe the port as "listening but not ready yet"
        // (that was the Windows CI failure — the 700 ms hold always
        // lost that race).
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let first_request = Arc::new(AtomicBool::new(false));
        let first_request_thread = first_request.clone();
        std::thread::spawn(move || {
            let start = Instant::now();
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                first_request_thread.store(true, Ordering::SeqCst);
                let mut buf = [0u8; 4096];
                let _ = stream.read(&mut buf);
                let elapsed = start.elapsed();
                if elapsed < Duration::from_millis(1500) {
                    std::thread::sleep(Duration::from_millis(1500) - elapsed);
                }
                let body = "{\"status\": \"ok\", \"version\": \"0.8.1\"}";
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });

        // A long-lived child standing in for the server process, in
        // its own process group so stopping it cannot touch the test
        // runner's group.
        #[cfg(unix)]
        let mut cmd = {
            use std::os::unix::process::CommandExt;
            let mut cmd = std::process::Command::new("/bin/sleep");
            cmd.arg("30")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            unsafe {
                cmd.pre_exec(|| {
                    libc::setsid();
                    Ok(())
                });
            }
            cmd
        };
        #[cfg(windows)]
        let mut cmd = {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            // NOTE: not `timeout /T 30` — timeout.exe refuses to run
            // with redirected stdin ("Input redirection is not
            // supported") and exits with code 1 at once, which made
            // wait_for_health_running report Exited instead of Ready.
            // `ping -n 31` sleeps ~30 s and does not care about stdin.
            let mut cmd = std::process::Command::new("cmd");
            cmd.args(["/C", "ping", "-n", "31", "127.0.0.1"])
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            cmd
        };
        let child = cmd.spawn().expect("spawn sleep child");
        let dir =
            std::env::temp_dir().join(format!("clipforge-test-slow-start-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let log = dir.join("clipforge-server.log");
        fs::write(&log, "").unwrap();
        let mut running = RunningSidecar {
            child,
            log_path: log.clone(),
            source: SidecarSource::DevPython {
                repo_root: PathBuf::from("/tmp"),
            },
            port,
            pid_file: dir.join("clipforge-server.pid"),
            #[cfg(windows)]
            job: None,
        };
        let saw_listening = Arc::new(AtomicBool::new(false));
        let saw_listening_cb = saw_listening.clone();
        let started = Instant::now();
        let outcome = wait_for_health_running(
            &mut running,
            Duration::from_secs(30),
            Duration::from_millis(50),
            &mut |_, listening| {
                if listening {
                    saw_listening_cb.store(true, Ordering::SeqCst);
                }
            },
        );
        assert_eq!(outcome, HealthWaitOutcome::Ready);
        assert!(started.elapsed() < Duration::from_secs(20));
        assert!(
            first_request.load(Ordering::SeqCst),
            "the fake server should have been probed"
        );
        assert!(
            saw_listening.load(Ordering::SeqCst),
            "progress must report the port as listening before /health answers"
        );
        stop_sidecar(&mut running);
        let _ = fs::remove_dir_all(&dir);
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
