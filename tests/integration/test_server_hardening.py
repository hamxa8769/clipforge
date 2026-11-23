"""Regression tests for the v0.9.3 server hardening (ClipForge v0.9.3).

The user's report: the server keeps shutting down again and again
while the app is running and then doesn't recover. Two halves:

* **Python** — one bad request must never kill the process: a global
  exception handler answers a JSON 500 (and logs the traceback to
  the server log) instead of the request taking the server down, and
  the launcher runs uvicorn as a single explicit worker.
* **Rust** — the supervisor (``start_supervisor`` in
  ``apps/desktop/src-tauri/src/main.rs``, policy in ``sidecar.rs``)
  restarts a dead/hung sidecar on the same port with backoff; see
  ``apps/desktop/src-tauri/tests/server_supervisor.rs`` for its
  source-level tests (the desktop crate cannot link on this Linux
  VM, so those run in CI on Windows).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")

import clipforge_api.app  # noqa: E402,F401  (ensures sys.modules entry exists)
from clipforge_api.app import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app_mod = sys.modules["clipforge_api.app"]

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
API_DIR = ROOT / "apps" / "api"


def test_global_exception_handler_is_registered() -> None:
    """The app must have a catch-all handler for unhandled errors."""
    assert Exception in app.exception_handlers


def test_one_bad_request_returns_500_and_server_stays_up(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A handler that raises must get a JSON 500 — and the next
    request must still work. Before v0.9.3 there was no global
    handler: the traceback still ended the request, but nothing
    guaranteed the process survived a bad request, and nothing
    logged the crash in one place with the request that caused it.
    """
    client = TestClient(app, raise_server_exceptions=False)

    def _boom() -> dict:
        raise RuntimeError("boom: a bad request killed the handler")

    monkeypatch.setattr(app_mod, "_requirements_payload", _boom)
    with caplog.at_level(logging.ERROR, logger="clipforge_api.app"):
        response = client.get("/requirements/status")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal_error"
    assert "still running" in body["detail"]
    # The crash is logged with the request and the traceback, so the
    # server log (what the splash page shows) explains it.
    assert "unhandled error on GET /requirements/status" in caplog.text
    assert "RuntimeError: boom" in caplog.text
    # And the server is still up: the very next request answers.
    assert client.get("/health").status_code == 200


def test_http_exceptions_keep_their_own_status() -> None:
    """The catch-all must not swallow 404s / 422s into 500s."""
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/jobs/does-not-exist").status_code == 404
    assert client.post("/jobs", json={"source_type": "nope"}).status_code == 422


def _child_env(data_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop("CLIPFORGE_DATABASE_URL", None)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), str(API_DIR)])
    env["CLIPFORGE_DATA_DIR"] = str(data_dir)
    return env


def test_health_is_async_so_pool_starvation_cannot_silence_it() -> None:
    """``/health`` must be async: a sync endpoint runs on uvicorn's
    40-thread pool, and long ``wait=true`` job calls starved that pool
    until /health stopped answering while the process was alive.

    NOTE: capping the wait itself was tried and reverted (see the
    ``NOTE (v0.9.3)`` in ``run_job_endpoint``'s docstring): a cap
    breaks the synchronous contract a real render relies on, so async
    /health is the fix-9 implementation that fits without a contract
    change.
    """
    import inspect  # noqa: PLC0415

    assert inspect.iscoroutinefunction(app_mod.health)


def test_upload_is_streamed_small_file_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small upload still creates a job and stores the exact bytes
    — the streaming rewrite must not change the happy path."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(app, raise_server_exceptions=False)
    payload = b"fake-mp4-bytes" * 100
    resp = client.post(
        "/jobs/upload",
        files={"file": ("clip.mp4", payload, "video/mp4")},
        data={"title": "stream test", "own_content_declaration": "true"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    stored = Path(body["source"])
    assert stored.is_file()
    assert stored.read_bytes() == payload
    client.post(f"/jobs/{body['id']}/cancel")


def test_upload_over_cap_is_rejected_without_partial_file_or_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-cap upload is refused while streaming (the cap is
    enforced chunk by chunk): 422, no partial file left behind, no
    job created. The old code only checked the size after reading
    the whole file into RAM."""
    import clipforge.ingest  # noqa: PLC0415

    monkeypatch.setattr(clipforge.ingest, "MAX_SOURCE_BYTES", 10)
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(app, raise_server_exceptions=False)
    # Compare job-id sets, not whole job dicts: the dispatcher runs
    # in the background and may still change an earlier job's status
    # (and the patched 10-byte cap can even fail an earlier job's
    # ingest) — what matters here is that no NEW job was created.
    before_ids = {j["id"] for j in client.get("/jobs").json()}
    resp = client.post(
        "/jobs/upload",
        files={"file": ("big.mp4", b"x" * 100, "video/mp4")},
        data={"title": "too big"},
    )
    assert resp.status_code == 422, resp.text
    assert "10 GiB" in resp.json()["detail"]
    assert list((tmp_path / "data" / "uploads").glob("*")) == []
    assert {j["id"] for j in client.get("/jobs").json()} == before_ids


def test_launcher_runs_uvicorn_as_one_explicit_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` must call ``uvicorn.run`` with ``workers=1``: the
    job queue, dispatcher thread and SQLite DB are in-process state,
    so a second worker would be a second queue and a second writer.
    """
    import uvicorn  # noqa: PLC0415
    from clipforge_api.__main__ import main  # noqa: PLC0415

    calls: dict = {}

    def _fake_run(*args, **kwargs) -> None:  # noqa: ANN001, ANN002
        calls["args"] = args
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    assert main(["--port", "8123"]) == 0
    assert calls["workers"] == 1
    assert calls["log_level"] == "info"
    assert calls["port"] == 8123
    assert calls["host"] == "127.0.0.1"


def test_bind_failure_exits_2_with_plain_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A held port must exit 2 with a plain line — including the
    "Address already in use" text the Rust shell extracts from the
    log — instead of a bare traceback."""
    import uvicorn  # noqa: PLC0415
    from clipforge_api.__main__ import main  # noqa: PLC0415

    def _fake_run(*args, **kwargs) -> None:  # noqa: ANN001, ANN002
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    assert main(["--port", "8123"]) == 2
    err = capsys.readouterr().err
    assert "could not start the ClipForge server on 127.0.0.1:8123" in err
    assert "Address already in use" in err


def test_launcher_configures_stderr_crash_logging(tmp_path: Path) -> None:
    """In a fresh interpreter (no pre-existing root handlers) the
    launcher must install a stderr root handler: that is what puts
    crash tracebacks into clipforge-server.log via the desktop
    shell's pipe. Runs in a subprocess so the import order and the
    logging state are real, like ``test_server_launch_db.py``.
    """
    data_dir = tmp_path / "data"
    script = tmp_path / "check_launch_logging.py"
    script.write_text(
        textwrap.dedent(
            """\
            import logging
            import sys
            import uvicorn

            def _fake_run(*args, **kwargs):
                pass

            uvicorn.run = _fake_run
            from clipforge_api.__main__ import main
            assert main(["--port", "8123"]) == 0
            handlers = logging.getLogger().handlers
            assert any(
                isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
                for h in handlers
            ), f"no stderr root handler: {handlers!r}"
            print("OK")
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        env=_child_env(data_dir),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
