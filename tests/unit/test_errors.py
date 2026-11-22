"""v0.9.0 plain-language error tests (Worker C).

No failure the user sees may carry a traceback, a ``ClassName:``
prefix, or a secret-looking fragment — only what failed, why, and
what to do next.
"""

from __future__ import annotations

from clipforge.errors import JobCancelled, plain_error, sanitize_error_message
from clipforge.ingest import IngestError
from clipforge.render import RenderError
from clipforge.workflow import MissingDependencyError


def test_missing_dependency_names_what_to_do():
    err = MissingDependencyError(
        code="ffmpeg_missing",
        dependency="ffmpeg",
        kind="tool",
        detail="FFmpeg renders the clips",
        required_action="Press 'Install FFmpeg' in ClipForge.",
    )
    out = plain_error(err)
    assert out["title"] == "ffmpeg is missing"
    assert "Press 'Install FFmpeg'" in out["message"]
    assert out["code"] == "ffmpeg_missing"
    assert out["action"] == "Press 'Install FFmpeg' in ClipForge."


def test_ingest_error_is_plain():
    out = plain_error(IngestError("source file does not exist: /tmp/nope.mp4"), "ingest")
    assert out["title"] == "Could not use this video"
    assert "does not exist" in out["message"]
    assert "Traceback" not in out["message"]
    assert out["action"]


def test_render_error_is_plain():
    out = plain_error(RenderError("ffmpeg render failed for x: boom"), "render")
    assert out["title"] == "Rendering failed"
    assert out["code"] == "render_failed"


def test_cancel_is_not_an_error():
    out = plain_error(JobCancelled("Job cancelled by the user."))
    assert out["title"] == "Job cancelled"
    assert out["code"] == "job_cancelled"


def test_unknown_error_never_shows_traceback_or_class_prefix():
    exc = RuntimeError(
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in f\n'
        "RuntimeError: boom in the pipeline"
    )
    out = plain_error(exc, "score")
    assert "Traceback" not in out["message"]
    assert "x.py" not in out["message"]
    assert "during score" in out["message"]
    assert "boom in the pipeline" in out["message"]


def test_sanitize_strips_class_prefix_and_caps_length():
    assert sanitize_error_message("ValueError: something broke") == "something broke"
    long = sanitize_error_message("x" * 600)
    assert len(long) <= 500 and long.endswith("...")
    assert sanitize_error_message("") == "Something went wrong."


def test_sanitize_hides_secret_like_fragments():
    msg = sanitize_error_message("call failed api_key: sk-live-12345 and token=abc")
    assert "sk-live-12345" not in msg
    assert "abc" not in msg
