"""v0.9.0 job-control primitive tests (Worker C).

Pause blocks until resumed, cancel raises JobCancelled (and kills a
live subprocess), and the registry hands out one control per job.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from clipforge.errors import JobCancelled
from clipforge.job_control import drop_control, get_control, wait_for_process


def test_pause_blocks_until_resumed():
    control = get_control("test-pause")
    try:
        control.request_pause()
        done = threading.Event()

        def _waiter():
            control.wait_if_paused()
            done.set()

        thread = threading.Thread(target=_waiter)
        thread.start()
        time.sleep(0.3)
        assert not done.is_set()
        control.request_resume()
        assert done.wait(timeout=5)
        thread.join(timeout=5)
    finally:
        drop_control("test-pause")


def test_cancel_during_pause_raises():
    control = get_control("test-cancel-pause")
    try:
        control.request_pause()
        with pytest.raises(JobCancelled):
            # Cancel wakes the pause wait — it must not hang.
            def _canceller():
                time.sleep(0.2)
                control.request_cancel()

            threading.Thread(target=_canceller).start()
            control.wait_if_paused()
    finally:
        drop_control("test-cancel-pause")


def test_registry_returns_same_control_per_job():
    a = get_control("test-same")
    b = get_control("test-same")
    assert a is b
    drop_control("test-same")
    assert get_control("test-same") is not a
    drop_control("test-same")


def test_cancel_kills_a_live_process():
    control = get_control("test-kill")
    try:
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        control.register_process(proc)
        assert proc.poll() is None

        def _canceller():
            time.sleep(0.2)
            control.request_cancel()

        threading.Thread(target=_canceller).start()
        with pytest.raises(JobCancelled):
            wait_for_process(proc, control)
        assert proc.poll() is not None
    finally:
        drop_control("test-kill")
