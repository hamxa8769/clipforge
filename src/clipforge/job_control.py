"""Per-job run control for ClipForge v0.9.0 (pause / resume / cancel).

One :class:`JobControl` per running job, held in a process-wide
registry (:func:`get_control`). The worker checks it between pipeline
stages and between clips inside the render stage; the long-running
subprocesses (ffmpeg render, yt-dlp download, live capture) register
their :class:`subprocess.Popen` here so a cancel kills the actual
process instead of only flipping a flag.

Honest limits (also stated in docs/API-CONTRACT.md §2):

* Pause takes effect **between stages and between clips**. A clip
  already rendering finishes first; a transcription already running
  (faster-whisper is one long native call) finishes first. The job
  then sits in ``paused`` until resumed — nothing is lost, the run
  continues from the next stage / clip.
* Cancel kills the ffmpeg / yt-dlp subprocess mid-render / mid-
  download. A transcription already running cannot be interrupted
  mid-call, so the cancel lands as soon as that stage returns.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

from clipforge.errors import JobCancelled

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, JobControl] = {}


def _terminate_process(proc: subprocess.Popen) -> None:
    """Stop a child process: polite terminate, then kill after 3 s."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # The process was started in its own process group
            # (see _popen_kwargs); CTRL_BREAK_EVENT reaches the whole
            # group, which is what stops ffmpeg's child threads too.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ValueError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def popen_kwargs() -> dict:
    """kwargs so a spawned ffmpeg / yt-dlp can be killed as a group.

    POSIX: new session → own process group, killed with killpg.
    Windows: new process group → CTRL_BREAK_EVENT reaches the group.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class JobControl:
    """Pause / cancel flags + the live subprocess for one job."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.pause_requested = threading.Event()
        self.cancel_requested = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    # ---------------------------------------------------------- process
    def register_process(self, proc: subprocess.Popen | None) -> None:
        with self._lock:
            self._process = proc

    def kill_process(self) -> None:
        """Terminate the registered subprocess (if still running)."""
        with self._lock:
            proc = self._process
        if proc is not None:
            _terminate_process(proc)

    # ---------------------------------------------------------- state
    def request_pause(self) -> None:
        self.pause_requested.set()

    def request_resume(self) -> None:
        self.pause_requested.clear()

    def request_cancel(self) -> None:
        self.cancel_requested.set()
        # Waking a paused wait is part of cancelling: the worker must
        # see the cancel instead of sleeping in the pause loop.
        self.pause_requested.clear()
        self.kill_process()

    @property
    def is_paused(self) -> bool:
        return self.pause_requested.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self.cancel_requested.is_set()

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` when a cancel was requested."""
        if self.cancel_requested.is_set():
            raise JobCancelled("Job cancelled by the user.")

    def wait_if_paused(self) -> None:
        """Block while paused; raise JobCancelled if cancelled meanwhile.

        Checked between pipeline stages and between clips. Polls every
        0.2 s so a cancel during a pause is noticed within a moment.
        """
        while self.pause_requested.is_set():
            if self.cancel_requested.is_set():
                raise JobCancelled("Job cancelled by the user.")
            time.sleep(0.2)
        self.check_cancelled()


def get_control(job_id: str) -> JobControl:
    """The control object for a job (created on first use)."""
    with _REGISTRY_LOCK:
        control = _REGISTRY.get(job_id)
        if control is None:
            control = JobControl(job_id)
            _REGISTRY[job_id] = control
        return control


def drop_control(job_id: str) -> None:
    """Forget a job's control object once its run has finished."""
    with _REGISTRY_LOCK:
        _REGISTRY.pop(job_id, None)


def wait_for_process(
    proc: subprocess.Popen, control: JobControl | None, timeout: float | None = None
) -> int:
    """Wait for a Popen to finish, honouring cancel (and a timeout).

    Polls every 0.1 s; when the job is cancelled the process is
    terminated and :class:`JobCancelled` is raised. When ``timeout``
    (seconds) elapses first the process is terminated and
    :class:`TimeoutError` is raised. Returns the exit code otherwise.
    """
    started = time.monotonic()
    while True:
        # Cancel first: request_cancel() already killed the process,
        # so checking poll() first would report a normal exit for a
        # job the user cancelled.
        if control is not None and control.is_cancelled:
            control.kill_process()
            raise JobCancelled("Job cancelled by the user.")
        code = proc.poll()
        if code is not None:
            return code
        if timeout is not None and time.monotonic() - started > timeout:
            if control is not None:
                control.kill_process()
            else:
                _terminate_process(proc)
            raise TimeoutError(f"process ran longer than {timeout:g}s")
        time.sleep(0.1)


__all__ = [
    "JobCancelled",
    "JobControl",
    "drop_control",
    "get_control",
    "popen_kwargs",
    "wait_for_process",
]
