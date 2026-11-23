# ClipForge v0.9.3 — Server Reliability Diagnosis

- **Date:** 2026-11-23 (PKT)
- **Base:** `origin/main` @ `b415883` (v0.9.2)
- **Branch:** `diag/v0.9.3-server-reliability` (this doc only; no product code changed, nothing pushed)
- **Trigger:** user screenshot 2026-11-23 ~17:54 PKT, v0.9.2, Settings page:
  `Demo data — API unreachable` + `Server unreachable — settings cannot be changed` +
  `The server did not answer. Start the server, then reload this page.` —
  **while** the top bar said `Live — server connected`, the bottom status bar said
  `Server: running · port 8000`, and `FFmpeg: checking…` was stuck.
  User report: the server "keeps shutting down again and again and doesn't recover".
- **Method:** full read of the Rust shell (`apps/desktop/src-tauri/src/main.rs`,
  `sidecar.rs`, `ffmpeg.rs`, `index.html`), the Python server entry
  (`apps/api/clipforge_api/__main__.py`, `app.py`), the job queue / worker /
  transcription / secrets / requirements code, and the web UI status paths
  (`apps/web/lib/api.ts`, `components/TopBar.tsx`, `components/StatusBar.tsx`,
  `app/settings/page.tsx`, `lib/tauri.ts`, `components/RequirementsPanel.tsx`);
  plus live reproduction on this Linux VM (real server, real kill test, real
  slow-`/settings` test, real poller emulating the UI). Windows-only code
  (Job Objects, Credential Manager, Defender) was read, not executed — it is
  marked as such wherever it matters.

## TL;DR — three separate defects, not one

1. **No supervision after startup (the "doesn't recover" bug).** The Rust
   shell waits for `/health` once at startup and then never checks the child
   again. There is no watchdog, no restart, no liveness re-check anywhere in
   the shell. If the server dies mid-run, the app keeps showing the startup
   verdict (`Ready`) forever, and the only restart path (`retry_server`) lives
   on the splash page — which the window has already navigated away from. The
   web UI has no restart button at all.
2. **The UI contradiction is a partial outage, not a full death.** `/health`
   is a ~2 ms static reply with no I/O; `GET /settings` does 11 live keyring
   reads plus two subprocess probes (~350–550 ms on a fast Linux VM, seconds
   on Windows under Defender/Credential Manager). The web client gives up on
   any request after **3 s** (`apiGet`, `apps/web/lib/api.ts`). So `/settings`
   can time out client-side while `/health` still answers — TopBar stays
   `Live`, StatusBar stays `running · port 8000`, Settings says `unreachable`,
   and the FFmpeg pill sticks on `checking…`. Reproduced exactly (see §4.2).
   A *full* server death flips **all** indicators consistently at the next
   30 s poll — proven by the poller experiment (§4.3) — so a persistent
   contradiction cannot be a full death older than one poll interval. Either
   the screenshot caught the ≤30 s window after a death, or (more likely, if
   it persisted) `/settings` specifically was timing out while the server lived.
3. **What kills the server is abrupt, not a handled error.** No handled Python
   exception can kill the process (dispatcher guarded, endpoints return 500,
   lifespan guarded — all ruled out with cites in §2). The remaining real
   killers are all *abrupt*: a native-code segfault or OOM in the in-process
   faster-whisper transcription, OOM from reading a whole upload into RAM,
   or an external kill (Defender/AV, OS). Any of these leaves **no traceback**
   in the log — the log just stops — which matches "shuts down again and
   again" with no visible error. The user's own server log is the evidence
   that decides between them (§6).

---

## 1. Root cause(s) for the server dying — and never coming back

### 1.1 The "never comes back" half: there is no supervisor. (CONFIRMED, code)

- `launch_sidecar_thread` (`apps/desktop/src-tauri/src/main.rs:159-256`) spawns
  the sidecar and calls `await_server_health` exactly once. After
  `finish_launch` stores the child in `state.sidecar` (`main.rs:366-380`), **no
  code ever looks at the child again** except:
  - `retry_server` (`main.rs:434-470`): checks `try_wait()` only to decide
    whether a *retry* may re-wait — and `retry_server` is only invocable from
    the splash page (`apps/desktop/src/index.html:160-171`).
  - `stop_sidecar` (`sidecar.rs`, called on quit / window-close / update) —
    shutdown paths only.
- `grep try_wait apps/desktop/src-tauri/src/*.rs` finds only `main.rs:450`
  (retry) and `sidecar.rs:1283,1346,1361` (inside `wait_for_health_running`
  = startup, and `stop_sidecar` = shutdown). **No watchdog thread, no
  periodic health re-check, no restart loop exists.**
- `get_server_status` (`main.rs:412-414`) returns a stored `ServerStatus`
  snapshot. `set_phase(ServerPhase::Ready)` happens only at `main.rs:198`
  (adopted server) and `main.rs:322` (health wait). Nothing ever sets the
  phase back to `Failed`/`Starting` after startup. So after a mid-run death
  the Rust side still reports `Ready` — and nothing in the web UI even reads
  this Rust status (see §3).
- **Recovery path is unreachable in practice.** The splash page
  (`apps/desktop/src/index.html:128-171`) polls `get_server_status` every
  500 ms and navigates away to the server URL the moment the phase is `Ready`
  (`index.html:135-139`). After that the splash is gone. `retry_server` is
  never called from the web UI: `apps/web/lib/tauri.ts` exposes only
  `get_server_logs`, the updater commands, and `open_external_url` — no
  `retry_server`, no `get_server_status`. A grep for `retry_server |
  get_server_status` across `apps/web` returns nothing (only the
  `LogsDrawer` comment mentions `get_server_logs`). **Once the server dies,
  the user has no in-app button to restart it; only relaunching the whole app
  (or killing it and starting again) works.**

### 1.2 The "dies" half: every crash/exit path found, with verdicts

| # | Path | File / line | Verdict |
|---|------|-------------|---------|
| D1 | Native segfault / OOM in **in-process faster-whisper** transcription | `src/clipforge/transcribe.py:224-229` (`WhisperModel(model, device="auto", compute_type="int8")` imported lazily, run in-process); called from the dispatcher thread via `src/clipforge/worker.py:581-593` → `transcribe(...)`; dispatcher is `threading.Thread(..., daemon=True)` in `src/clipforge/job_queue.py:117-119` | **REAL RISK — top suspect.** A segfault in `faster-whisper`/CTranslate2/onnx native code, or OOM loading the ~500 MB `small` model (see `worker.py:636-637`: "Whisper model (about 500 MB for the default 'small' model)"), kills the **whole uvicorn process instantly with no traceback and no log line**. Jobs auto-run on creation (v0.9.0 auto-run, `app.py` `create_job` → `get_job_queue().enqueue`), so merely creating a YouTube job starts a transcription that can kill the server. Cannot be confirmed from code alone — needs the user's log (§6). |
| D2 | OOM from **upload read whole file into RAM** | `apps/api/clipforge_api/app.py` `upload_job`: `raw = await file.read()` then `stored.write_bytes(raw)`, cap 10 GiB (`MAX_SOURCE_BYTES`). No streaming, no memory guard | **REAL RISK.** A multi-GB upload on a low-RAM Windows machine can OOM-kill the process (Windows kills the process; no traceback). Only applies to file uploads, not YouTube-URL jobs — so not the explanation if the user only pasted links. |
| D3 | Defender / antivirus killing or quarantining the PyInstaller one-file exe | No code cite — external actor. Supporting evidence in-repo: v0.9.2's yt-dlp Error 32 was a Windows file lock whose "most likely locker is Windows Defender/antivirus real-time scanning" (`MEMORY.md` / v0.9.2 notes). One-file exe extracts to `%TEMP%/_MEIxxxxxx` on every launch; a quarantine/deletion of extracted files, or a behavioural block, kills the process abruptly | **PLAUSIBLE, external.** Same abrupt signature as D1/D2 (log just stops). Check Defender protection history + whether `clipforge-server.log` ends mid-line (§6). |
| D4 | Windows **Job Object kill-on-close** firing while the shell is alive | `sidecar.rs` `win::assign_child_to_new_job` + `create_kill_on_close_job` (kill-on-close); `RunningSidecar.job` held in `AppState.sidecar` | **RULED OUT for this incident** (shell was visibly alive showing the UI), but real by design on shell exit/crash: if the shell process exits for any reason, the OS kills the server tree. Also note `main.rs` CloseRequested handler (`main.rs`, `on_window_event`): closing the main window stops the sidecar and exits — by design, not a bug — so "server died" after the user closed the window is expected behaviour, not this bug. |
| D5 | A **second ClipForge launch** killing the live server | `main.rs:159-256` Step 1 adopts a healthy server; Step 2 `cleanup_stale_sidecar` (`sidecar.rs`) kills the pid-file server whenever `/health` does **not** answer — including a live server whose `/health` is temporarily slow (threadpool starvation, §D8). If the user double-clicks the icon again while `/health` is momentarily unresponsive, the second shell kills the first shell's server; the first shell never notices (§1.1) | **POSSIBLE edge.** Requires a double launch during a `/health` stall. Worth a log check: two `[shell] ==== ClipForge start` markers close together in `clipforge-server.log` / `.log.1`. |
| D6 | `RunningSidecar` **Drop impl SIGTERMs the process group** | `sidecar.rs` `impl Drop for RunningSidecar`: on drop without `stop_sidecar`, SIGTERM to the child's process group (Unix) / terminate job (Windows). Drop sites: after `stop_sidecar` (already reaped — harmless), and in `finish_launch` Exited branch (already dead) | **RULED OUT as a live-kill in normal flow** — every drop site is reached only after the child is stopped/reaped. Residual (negligible) race noted: after `child.wait()` reaps the pid, the drop still calls `getpgid(pid)` + `killpg`; if the OS reused the pid in between, the wrong group would be signalled. Window is microseconds; not this bug. |
| D7 | **Port conflict** with a stale server | `sidecar.rs` `pick_port` (TOCTOU window between probe and bind, documented in-code) + `extract_failure_hint` ("address already in use") | **RULED OUT mid-run; startup-only, and proven fail-fast.** Repro (§4.4): a second server on a busy port exits with `ERROR: [Errno 98] ... address already in use` + clean shutdown; the first server is unaffected. A bind failure can never kill an already-running server. |
| D8 | **Threadpool starvation making `/health` stop answering** (server alive, looks dead) | `__main__.py:355` `uvicorn.run(app, host=host, port=port, log_level="info")` — single worker, no `--workers`; all `def` (sync) endpoints run in uvicorn's 40-thread pool. `POST /jobs/{id}/run` `wait=true` (default, `app.py` `run_job_endpoint`) blocks a pool thread in `JobQueue.wait_for(job_id, timeout=None)` (`job_queue.py:282`, `timeout=None` = wait forever) until the job finishes; same for `/retry` | **REAL RISK (partial outage, not death).** Enough concurrent long `wait=true` calls (double-clicked Run, several tabs, batch) can exhaust the 40 threads; then `/health` (also sync, same pool) stops answering and the UI flips to Demo even though the process lives. No thread is ever reclaimed while a job runs. |
| D9 | Handled Python exception killing the process | Dispatcher: `job_queue.py:491-500` `_dispatch_loop` wraps `_dispatch_once` in try/except + traceback + keep going; runner exceptions are caught and the queue released (`job_queue.py:565+`); FastAPI/Starlette converts handler exceptions to 500s; lifespan pre-flight wrapped in try/except (`app.py` `_lifespan`) | **RULED OUT.** No unguarded exception path to the main thread was found. `grep os._exit\|sys.exit` across `src/clipforge` + `apps/api` finds only `__main__` entry points and two `scripts/...py` guards (`ffmpeg_install.py:1447`, `ytdlp_install.py:1211` — CLI-only, not imported by the server path). |
| D10 | **Requirements pre-flight blocking / killing startup** (v0.9.2) | `app.py` `_lifespan` + `_requirements_payload`: wrapped in try/except ("the check must never stop startup"); missing/broken tools only log a warning | **RULED OUT as a death cause.** But the live check is expensive: `check_requirements` (`src/clipforge/requirements.py`, default `timeout=5.0` per probe, 3 probes + Whisper probe) runs on **every** `GET /requirements/status` and at startup; probe timeouts are 10 s each in `probe_ffmpeg`/`probe_yt_dlp`/`probe_ffprobe`. It contributes to D8 load, not to death. |
| D11 | Background monitor / scheduler threads crashing the process | `src/clipforge/auth/health.py:630-696` monitor thread: `try/except: pass # a failed pass must never kill the monitor thread`; daemon thread | **RULED OUT.** Guarded by design; a dead monitor thread is silent, not fatal. |
| D12 | SQLite "database is locked" / two-database split (v0.9.1) | `sidecar.rs` `sidecar_env` pins `DATABASE_URL` to the app-data dir; `__main__.py` `_prepare_environment` | **RULED OUT as a death cause** (v0.9.1 fixed the split; a lock error is a 500, not an exit). A locked DB can stall requests, feeding D8. |

**Net on "why it dies":** the code contains **no in-process crash path that
leaves a traceback** — which is itself the finding. The deaths the user sees
are abrupt kills (D1/D2/D3, possibly D5). The single most useful next
evidence is the tail of `%APPDATA%\com.clipforge.app\logs\clipforge-server.log`
(and `.log.1`): an abrupt end with no traceback = D1/D2/D3; a traceback =
re-open D9; two launch markers = D5.

---

## 2. Root cause(s) for the UI contradicting itself

There is **no single "server status"** in this app. There are **three
independent status sources**, polled at different rates, against endpoints
with wildly different costs — and the Settings page does not poll at all.

### 2.1 The three sources

| # | What the user sees | Reads | Poll | Code |
|---|--------------------|-------|------|------|
| S1 | Splash / Rust shell status (`get_server_status`) | `AppState.phase` — set once at startup, never updated afterwards (§1.1) | 500 ms, splash page only, gone after navigation | `apps/desktop/src-tauri/src/main.rs:412-414`, `apps/desktop/src/index.html:128-139` |
| S2 | TopBar `Live — server connected` / `Demo mode — server unreachable` | `GET /health` (+ `GET /perf/profile`, same call) | every **30 s**, independent timer | `apps/web/components/TopBar.tsx:12-26` |
| S3 | StatusBar `Server: running · port 8000` + FFmpeg pill | `GET /health` + `GET /settings` + `GET /perf/profile`, all in one `Promise.all` | every **30 s**, independent timer (different phase from S2) | `apps/web/components/StatusBar.tsx:43-56` |
| S4 | Settings page `Live settings from the server` / `Server unreachable — settings cannot be changed` + `The server did not answer…` | `GET /settings`, **once on mount, never retried, never polled** | once | `apps/web/app/settings/page.tsx:106-122` (`useEffect(load)` once; `load` has no interval) |

The header (S2) and the status bar (S3) can disagree with each other for up
to 30 s simply because their timers are independent and each keeps its last
good value until its own next poll succeeds-or-fails. The Settings page (S4)
can disagree with both **forever**: it loads once; if that one load fails it
never tries again until a manual page reload. There is no shared status store,
no "last known good + age" display, and no retry button on the page.

### 2.2 Why `/health` answers while `/settings` fails — the cost asymmetry

- `GET /health` (`app.py`, `health()`): returns a static dict. No keyring,
  no settings file, no database — by explicit design ("Liveness probe only").
  **Measured: ~2 ms** (5 consecutive curls: 1.7–3.3 ms).
- `GET /settings` (`app.py`, `get_settings()` → `SettingsStore.public_view`,
  `src/clipforge/auth/settings_store.py:301-331`): on **every call** it does
  - **11 live keyring reads** — `credential_view()` for `llm/api_key`,
    `gemini/api_key`, and 4 platforms × `client_id`/`client_secret`, each via
    `SecretStore.get()` (`src/clipforge/auth/secrets.py:171-190`), which
    calls `keyring.get_password(...)` live every time. **There is no caching
    of secret values** (only the `SecretStore` *instance* is cached,
    `app.py` `_SECRETS_CACHE` — the v0.8.1 fix cached the store, not the
    reads). On Windows each read is a live Credential Manager call; the
    v0.8.1 incident was exactly "`/health` hung on keyring" — the same hang
    still exists for `/settings`, it was only moved off `/health`.
  - **2 subprocess probes** — `ffmpeg_info()` → `probe_ffmpeg(...)` and
    `ytdlp_info()` → `probe_yt_dlp(...)`, each spawning the binary with
    `--version`/`-version` (10 s timeout per probe; `probe_yt_dlp` walks many
    candidate paths). On Windows every spawn goes through Defender real-time
    scanning.
  - **Measured on this Linux VM (no keyring, fast local binaries): ~350–550 ms
    per call** — 175× the cost of `/health`. First call 551 ms, then
    ~350 ms. On the user's Windows machine the same call is plausibly
    seconds: 11 Credential Manager round-trips + Defender-scanned spawns.
- The web client gives up after **3 s on every GET**: `apiGet`
  (`apps/web/lib/api.ts`, `signal: AbortSignal.timeout(3000)`), and `null`
  is returned for *any* failure — timeout, connection refused, 500, bad JSON
  are indistinguishable. So a `/settings` that takes >3 s server-side is
  reported to the UI as "server unreachable", while `/health` (2 ms) keeps
  succeeding. **The UI cannot tell "server dead" from "server slow" — and
  `/settings` is the endpoint most likely to be slow.**

### 2.3 The exact screenshot state, reproduced

With a deliberately slow `ffmpeg` (a shim that sleeps 4 s then execs the real
binary — standing in for Defender-slowed spawning / a slow Credential Manager
on Windows), the live server produced exactly the screenshot (§4.2):

- `GET /health` → `200` in **1.6 ms** → TopBar `Live`, StatusBar `running`.
- `GET /settings` with a 3 s client timeout (as `apiGet` does) → `000`,
  curl exit 28 (timeout) at 3.002 s → Settings page `Server unreachable`,
  StatusBar `settings == null` → FFmpeg pill `checking…`.
- Same `GET /settings` with a 30 s timeout → `200` in **4.40 s**. The server
  was alive the whole time; only the client's 3 s budget was exceeded.

### 2.4 Full death behaves differently — and that is the proof

The poller experiment (§4.3) emulates TopBar + StatusBar + Settings logic
with a 5 s poll: after `SIGKILL`, **all three indicators flipped
consistently at the very next poll** (~4 s later) and stayed flipped. In the
real app the poll is 30 s, so the same consistent flip happens within 30 s
of a full death. Conclusion: **a persistent contradiction cannot be a full
server death older than one poll interval.** What the screenshot shows is
either (a) a screenshot taken inside the ≤30 s window after a death, or
(b) — the persistent case — the partial outage of §2.2/§2.3. The fix must
handle (b), not just (a).

### 2.5 Ruled out: port / base-URL mismatch

- The desktop app is built with `--with-web-static` (`.github/workflows/
  release.yml`, `desktop-windows` job) → `scripts/build_exe.py`
  `_build_web_static` → `npm run build:static` → `apps/web/scripts/
  build-static.mjs` sets `NEXT_PUBLIC_API_URL=""`. `resolveApiUrl("")`
  returns `""` (`lib/api.ts`), so all fetches are **relative (same-origin)**:
  the UI calls the API on whatever port the shell navigated to. There is no
  baked `localhost:8000` in the desktop build.
- The *other* build path, `_build_web` (`--with-web`, the standalone zip),
  bakes `NEXT_PUBLIC_API_URL=http://localhost:8000` — but that is the
  standalone server zip, not the desktop app the user runs. Not this bug.
- `StatusBar`'s `portLabel()` reads the port from `API_URL` when set, else
  `window.location.port` — in the desktop build that is the real server port.
  `Server: running · port 8000` therefore means the UI really was served
  from port 8000, not a stale constant.

---

## 3. Root cause of stuck "FFmpeg: checking…"

`apps/web/components/StatusBar.tsx:66-70`:

```tsx
{settings ? (
  <Pill ok={ffmpeg?.found === true} label="FFmpeg" value={...} />
) : (
  <Pill warn label="FFmpeg" value={serverUp ? "checking…" : "unknown"} />
)}
```

The FFmpeg pill does **not** come from a live FFmpeg probe and does **not**
come from the Rust `check_ffmpeg` command. It is derived from `GET
/settings` (`settings.ffmpeg`). When `getSettings()` returns `null` —
which happens whenever `/settings` is slow (>3 s), errors, or the server is
down — while `getHealth()` still succeeds, the pill renders `checking…`
**forever**: every 30 s poll re-runs the same failing `/settings` call, there
is no error state, no retry, and no timeout message. The Rust-side FFmpeg
check (`apps/desktop/src-tauri/src/ffmpeg.rs`, `check_ffmpeg`, no timeout on
`Command::output`) is only used by the *splash* page banner, never by the
status bar — so the status bar has no fallback source. The pill's text is
also misleading: nothing is "checking" — the check already failed and was
swallowed (`load().catch(() => {})` in the interval, `StatusBar.tsx:52`).

---

## 4. Reproduction log (this Linux VM, 2026-11-23 ~13:00 PKT)

All commands run from `~/workspace/cf-w-diag` (worktree of `b415883`).
Repro servers used throwaway data dirs under `/tmp/cf-diag/`; all stopped
afterwards (`pgrep` clean). One self-inflicted incident during the repro: a
`pgrep -f` pattern matched my own shell's command line and the `kill -9`
killed the exec session itself; the port-8000 server still died as intended
and the test was re-run cleanly with an anchored `^python3 -m ...` match and
a launcher script (`/tmp/cf-diag/start8000.sh`). Nothing else was affected.

### 4.1 Baseline latency — `/health` vs `/settings` vs `/requirements/status`

Started exactly as the sidecar does
(`CLIPFORGE_DATA_DIR` + `DATABASE_URL` set, `PYTHONPATH=apps/api:src`,
`python3 -m clipforge_api --host 127.0.0.1 --port 8000`). Startup to first
`/health` 200 took ~10 s on this VM (cold import + lifespan requirements
check) — the reason the shell waits 120 s.

```
GET /health               x5:  200  0.0033s / 0.0020s / 0.0020s / 0.0018s / 0.0017s
GET /settings             x5:  200  0.5519s / 0.3490s / 0.3712s / 0.3462s / 0.3545s
GET /requirements/status  x1:  200  0.4005s
/settings body: ffmpeg found /usr/bin/ffmpeg 8.1.2 (PATH); yt-dlp found
  ~/.local/bin/yt-dlp 2026.10.26; secret_backend: encrypted-file
```

### 4.2 Partial outage — slow `/settings`, fast `/health` (the screenshot, reproduced)

Slow-ffmpeg shim (`/tmp/cf-diag/slowbin/ffmpeg`: `sleep 4; exec
/usr/bin/ffmpeg "$@"`), server on port 8001 with that dir first on `PATH`:

```
GET /health                              -> 200  0.0016s
GET /settings  (curl --max-time 3, as apiGet) -> 000  3.0025s, curl exit 28 (timeout)
GET /settings  (curl --max-time 30)           -> 200  4.4008s
```

Server alive throughout. With the real UI code this is: TopBar `Live`,
StatusBar `running · port 8000` + `FFmpeg: checking…`, Settings
`Server unreachable` — the screenshot, exactly.

### 4.3 Full death — poller emulating TopBar + StatusBar + Settings (5 s poll)

`SIGKILL` to the exact python pid (15163 → re-run pid 15249) mid-poll:

```
t= 0.4s TopBar=[Live] StatusBar Server=[running · port 8000] FFmpeg=[found] Settings=[live]
t= 5.8s ... (same)
t=11.2s ... (same)
[kill -9 at ~t=12s]
t=16.2s TopBar=[Demo mode — server unreachable] StatusBar Server=[unreachable] FFmpeg=[unknown] Settings=[Server unreachable]
t=21.2s ... (same, all flipped, stays flipped through t=66.2s)
```

All indicators flipped **consistently at the first poll after death** (~4 s
later at a 5 s poll; ≤30 s at the real 30 s poll). After the kill: no
clipforge process remained, port 8000 connection-refused, **nothing
restarted it** (only the untouched port-8001 control server was left).
Killed server's log ends abruptly with a normal `GET /health 200 OK` line —
**0 lines matching traceback/error** — the signature of an abrupt kill.

### 4.4 Port conflict — startup-only, fail-fast (ruled out mid-run)

Second server on occupied port 8000 while the first was alive:

```
ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8000): address already in use
INFO:  Application startup complete. / Waiting for application shutdown. / Application shutdown complete.
```

First server unaffected.

---

## 5. Prioritised fix list for Workers B/C

Order matters: (1)–(3) stop the user-visible lie and restore recovery;
(4)–(6) remove the slow-endpoint trap; (7)–(9) reduce the deaths themselves.

### P0 — restore recovery + honest status

1. **Rust watchdog + auto-restart (the core fix).** In
   `apps/desktop/src-tauri/src/main.rs` / `sidecar.rs`: after `Ready`, spawn a
   supervisor thread that polls `GET /health` (or `try_wait()` on the child)
   every ~5 s. On child exit or `/health` failing N consecutive times:
   set phase to `Failed` (so `get_server_status` tells the truth), then
   restart the sidecar with the same launch path (respecting the
   adopt/re-wait rules), up to e.g. 3 attempts with backoff, then surface
   `Failed` + log tail. This single change fixes "doesn't recover" and the
   frozen-`Ready` lie. Also reap/restart when the adopted server dies.
2. **Give the web UI a restart path.** Expose `retry_server` (and
   `get_server_status`) through `apps/web/lib/tauri.ts` and add a
   "Server unreachable — Restart server" action in `TopBar`/`StatusBar`
   (visible when health is null) and on the Settings page's dead state.
   The splash-only Retry is unreachable after navigation — that is why the
   user is stuck.
3. **One shared status source in the web UI.** Replace the three independent
   pollers with a single status hook/context (one `/health` poll, e.g. every
   10 s, shared by TopBar, StatusBar, Settings) that records
   `last_success_at` and distinguishes `down` from `slow`/`unknown`, and
   shows the age ("last seen 45 s ago"). Settings page must re-poll (or at
   least retry on focus / on "Retry" click) instead of loading once.

### P1 — stop `/settings` from timing out while the server is alive

4. **Make `GET /settings` cheap.** In `src/clipforge/auth/settings_store.py`
   `public_view()`: cache secret *values* (short TTL, e.g. 60 s, or cache
   `credential_view` results and invalidate on `set_credential`), and cache
   the `ffmpeg_info`/`ytdlp_info` probe results (invalidate on install /
   recheck). 11 live keyring reads + 2 subprocess spawns per call is the
   direct cause of the 3 s client timeout on Windows.
5. **Raise / split the client timeouts.** In `apps/web/lib/api.ts`: `apiGet`
   3 s is too tight for `/settings` and `/requirements/status` (which run
   live probes; server-side worst case is ~15–30 s). Either raise the
   default, or give the slow endpoints their own longer timeout, and —
   critically — **distinguish timeout from connection-refused in the UI**
   ("server is slow" vs "server is down"). Never render "unreachable" for a
   timeout.
6. **Fix the FFmpeg pill.** In `apps/web/components/StatusBar.tsx`: derive
   FFmpeg from `GET /ffmpeg/status` (cheap, pollable, already exists) or
   from the Rust `check_ffmpeg` command, not from `GET /settings`; add a
   real error state instead of permanent `checking…` (e.g. `unknown — could
   not check`, with retry).

### P2 — reduce the deaths themselves

7. **Move transcription out of the server process (or guard it).** The
   in-process `faster-whisper` model load in the dispatcher thread
   (`src/clipforge/transcribe.py:224-229`, `worker.py:581-593`) is the top
   abrupt-death suspect. Options in order: (a) run transcription in a
   subprocess with a memory cap + crash isolation (a dead transcriber fails
   the job, not the server); (b) at minimum, log model-load start/finish
   with RSS, and refuse to load the model when free memory is below a
   threshold. Record the decision + evidence in the log so the next death
   is attributable.
8. **Stream uploads; never `read()` a whole file into RAM.** In
   `apps/api/clipforge_api/app.py` `upload_job`: stream `UploadFile` to disk
   in chunks (`shutil.copyfileobj` / async chunks) instead of
   `raw = await file.read()` + `write_bytes(raw)`. Add a server-side
   size/memory guard.
9. **Stop long `wait=true` calls from starving `/health`.** In
   `apps/api/clipforge_api/app.py` `run_job_endpoint` / `retry_job`: do not
   block a uvicorn worker thread indefinitely on `queue.wait_for(timeout=
   None)` (`job_queue.py:282`). Either cap the wait (e.g. 25 s, then return
   202 + `queue_position` and let the UI poll), or move `/health` to an
   async endpoint on a separate lightweight path. Also consider
   `uvicorn.run(..., timeout_keep_alive=...)` hygiene — but do not add
   `--workers >1` without fixing the SQLite single-writer assumptions first.

### P3 — evidence + hygiene (do with the above, not instead)

10. **Log server death forensically.** On Rust restart (fix 1), record the
    child exit code/signal + last 40 log lines in the splash and in
    `clipforge-server.log`; on the Python side, log RSS + model-load events
    (fix 7). Without this the next "it died again" is still unactionable.
11. **Second-launch guard (D5).** In `main.rs` Step 2: before
    `cleanup_stale_sidecar` kills a pid-file server, re-probe `/health`
    with a longer timeout and check the *other* shell is not alive
    (e.g. a lock file / mutex); never kill a server that answered `/health`
    within the last few seconds.
12. **Rust `check_ffmpeg` timeout.** `ffmpeg.rs` `run_version` uses
    `Command::output()` with no timeout; a hung `ffmpeg -version` blocks
    the Tauri setup thread at startup. Add a timeout (and the same for any
    future probes).

---

## 6. Evidence still needed from the user's PC

These decide between D1 / D2 / D3 (the abrupt killers) — please collect
before or alongside the fixes:

1. `%APPDATA%\com.clipforge.app\logs\clipforge-server.log` **and**
   `clipforge-server.log.1` — full tail (last 100 lines). Abrupt end with no
   traceback → D1/D2/D3; traceback → re-open D9; two `==== ClipForge start`
   markers minutes apart → D5.
2. What was the app doing when it died — creating/running a job (YouTube URL
   or file upload?), idle on Settings, or just after launch? A death during
   transcription points at D1; during a large upload, D2.
3. Windows Event Viewer → Windows Logs → Application, around the death time:
   any `Application Error` for `clipforge-server.exe` (faulting module
   `onnxruntime`/`ctranslate2`/`*dll` → D1; `Out of memory` → D1/D2).
4. Windows Security → Protection history: any quarantine/block of
   `clipforge-server.exe` or `%TEMP%\_MEI*` around the death time → D3.
5. Rough machine RAM + size of the video/upload involved.
6. Whether the app was launched twice (double-clicked icon while already
   running) around the deaths → D5.

## Appendix — file:line index of the key evidence

- `apps/desktop/src-tauri/src/main.rs:159-256` launch thread (startup-only);
  `:198`, `:322` only `Ready` transitions; `:366-380` `finish_launch`;
  `:412-414` `get_server_status` (stale snapshot); `:434-470` `retry_server`;
  `:450` only post-startup `try_wait`.
- `apps/desktop/src-tauri/src/sidecar.rs`: `wait_for_health_running` (startup
  only); `stop_sidecar`; `Drop for RunningSidecar`; `cleanup_stale_sidecar`;
  `pick_port` TOCTOU.
- `apps/desktop/src/index.html:128-171` splash poll + navigate-away on Ready +
  Retry (splash-only).
- `apps/web/lib/tauri.ts` — no `retry_server`/`get_server_status` exposed.
- `apps/web/lib/api.ts` — `apiGet` 3 s timeout; `resolveApiUrl`.
- `apps/web/components/TopBar.tsx:12-26`, `StatusBar.tsx:43-70` — independent
  30 s polls; `:66-70` FFmpeg `checking…` branch.
- `apps/web/app/settings/page.tsx:106-122` — Settings loads once.
- `apps/api/clipforge_api/__main__.py:355` — `uvicorn.run(...)` single worker.
- `apps/api/clipforge_api/app.py` — `health()` (no I/O); `get_settings()` →
  `public_view()`; `_lifespan` (guarded); `upload_job` (`await file.read()`);
  `run_job_endpoint` (`wait=true` → `queue.wait_for`).
- `src/clipforge/auth/settings_store.py:301-331` `public_view` (11 keyring
  reads + 2 probes); `src/clipforge/auth/secrets.py:171-190` `SecretStore.get`
  (live `get_password`, no value cache).
- `src/clipforge/requirements.py` `check_requirements` (live probes, 5 s
  default / 10 s probe timeouts); `probe_ffmpeg`/`probe_yt_dlp` in
  `ffmpeg_install.py:507` / `ytdlp_install.py:425`.
- `src/clipforge/job_queue.py:117-119` dispatcher thread; `:282`
  `wait_for(timeout=None)`; `:491-500` guarded loop.
- `src/clipforge/worker.py:581-593` in-process `transcribe(...)` call;
  `src/clipforge/transcribe.py:224-229` `WhisperModel(...)` in-process.
- `apps/web/scripts/build-static.mjs` (`NEXT_PUBLIC_API_URL=""`) +
  `.github/workflows/release.yml` `desktop-windows` (`--with-web-static`) —
  same-origin fetches; port mismatch ruled out.
