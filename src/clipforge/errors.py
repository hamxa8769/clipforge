"""Plain-language errors for ClipForge v0.9.0.

Every failure the user sees goes through :func:`plain_error`: what
failed, why, and what to do next — never a raw traceback, never a
``TypeError: ...`` prefix, never a raw JSON blob. The worker stores
the plain message on the job (``job.error``) plus a short
``error_title`` / ``error_action`` in ``meta_json`` so the UI can show
the reason and a Retry button without parsing anything.

Known failures are mapped by exception type; anything unknown falls
back to a generic message with the exception's own words (sanitised:
traceback lines and ``ClassName:`` prefixes stripped, capped at 500
chars). No secret ever reaches here — messages are built from the
exception text only, and :func:`sanitize_error_message` drops lines
that look like tracebacks or paths to credential files.
"""

from __future__ import annotations

import re

from clipforge.workflow import MissingDependencyError

_MAX_MESSAGE_CHARS = 500

# Lines that must never reach the user: tracebacks, file paths inside
# the app, and anything that looks like a credential value. Note: a
# ``XError: ...`` line is NOT dropped here — the message itself often
# starts with one (``ValueError: ...``), and the class-prefix strip
# below handles that; dropping it would eat the whole message.
_TRACEBACK_RE = re.compile(r"(?m)^(Traceback|File \"|  File |    ).*$")
_CLASS_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*Error: ")
_SECRET_HINT_RE = re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*\S+")


def sanitize_error_message(message: str) -> str:
    """Strip tracebacks / class prefixes / secret-looking fragments."""
    text = str(message or "").strip()
    # Drop every traceback-looking line, keep the plain prose.
    lines = [line for line in text.splitlines() if not _TRACEBACK_RE.match(line.strip())]
    text = " ".join(line.strip() for line in lines if line.strip())
    text = _CLASS_PREFIX_RE.sub("", text)
    text = _SECRET_HINT_RE.sub(r"\1: (hidden)", text)
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[: _MAX_MESSAGE_CHARS - 3].rstrip() + "..."
    return text or "Something went wrong."


class JobCancelled(RuntimeError):
    """Raised inside the pipeline when the user cancels a running job.

    Not an error: the job ends as ``cancelled`` with a plain message,
    and no error notification is created for it.
    """


def plain_error(exc: BaseException, stage: str | None = None) -> dict:
    """Map an exception to ``{title, message, action, code}``.

    ``stage`` (ingest / transcribe / score / render / seo) is used
    only for the generic fallback title, so the user knows *where* the
    job stopped.
    """
    from clipforge.ingest import IngestError  # noqa: PLC0415 (avoid cycle)
    from clipforge.live import LiveCaptureError  # noqa: PLC0415
    from clipforge.render import RenderError  # noqa: PLC0415

    if isinstance(exc, JobCancelled):
        return {
            "title": "Job cancelled",
            "message": "You cancelled this job. Nothing was published.",
            "action": "Start it again any time from the job page.",
            "code": "job_cancelled",
        }
    if isinstance(exc, MissingDependencyError):
        return {
            "title": f"{exc.dependency} is missing",
            "message": f"{exc.detail} {exc.required_action}",
            "action": exc.required_action,
            "code": exc.code,
        }
    if isinstance(exc, LiveCaptureError):
        return {
            "title": "Live capture stopped",
            "message": sanitize_error_message(str(exc)),
            "action": "Check the stream link and licence, then try again.",
            "code": "live_capture_failed",
        }
    if isinstance(exc, IngestError):
        from clipforge.ingest import looks_like_download_lock  # noqa: PLC0415

        # v0.10.3: the ingest stage classifies its own failures
        # (IngestError.code), so the user gets the specific reason and
        # the right next step instead of the generic ingest message.
        # The message itself is already plain language; only the title
        # and action are specialised here.
        code = getattr(exc, "code", None)
        if code == "youtube_unavailable":
            return {
                "title": "This video isn't available on YouTube",
                "message": sanitize_error_message(str(exc)),
                "action": (
                    "Try a different link — a removed or private video "
                    "can't be downloaded by any app."
                ),
                "code": "youtube_unavailable",
            }
        if code == "youtube_bot_check":
            return {
                "title": "YouTube asked to sign in",
                "message": sanitize_error_message(str(exc)),
                "action": (
                    "Wait a little while and retry the job. If it keeps "
                    "happening, the link may need YouTube cookies — try a "
                    "different video first."
                ),
                "code": "youtube_bot_check",
            }
        if code == "youtube_live":
            return {
                "title": "This is a live stream",
                "message": sanitize_error_message(str(exc)),
                "action": "Use Live capture for live streams, not a normal job.",
                "code": "youtube_live",
            }
        if code == "youtube_unreachable":
            return {
                "title": "Couldn't reach YouTube",
                "message": sanitize_error_message(str(exc)),
                "action": "Check your internet connection and retry the job.",
                "code": "youtube_unreachable",
            }
        if code in {"download_stalled", "download_timeout"}:
            return {
                "title": "The download stalled",
                "message": sanitize_error_message(str(exc)),
                "action": (
                    "Check your internet connection and retry the job. "
                    "If it keeps stalling, try a shorter video."
                ),
                "code": code,
            }
        if code == "youtube_age_gated":
            return {
                "title": "This video is age-restricted",
                "message": sanitize_error_message(str(exc)),
                "action": "Try a different link — age-restricted videos need a signed-in download.",
                "code": "youtube_age_gated",
            }
        if code == "youtube_geo_blocked":
            return {
                "title": "This video isn't available in your country",
                "message": sanitize_error_message(str(exc)),
                "action": "Try a different link.",
                "code": "youtube_geo_blocked",
            }
        if code == "youtube_playlist":
            return {
                "title": "That's a playlist, not a video",
                "message": sanitize_error_message(str(exc)),
                "action": "Open the video itself and paste its link instead.",
                "code": "youtube_playlist",
            }
        if looks_like_download_lock(str(exc)):
            # v0.9.2: a download that died on a Windows file lock
            # (usually antivirus real-time scanning holding a fragment).
            # The message itself is already plain language; give it the
            # specific title and the exclusion fix as the action.
            return {
                "title": "The download was blocked by a file lock",
                "message": sanitize_error_message(str(exc)),
                "action": (
                    "Add the ClipForge data folder to your antivirus "
                    "exclusions (Windows Security > Virus & threat protection "
                    "> Exclusions), then retry the job."
                ),
                "code": "download_file_lock",
            }
        return {
            "title": "Could not use this video",
            "message": sanitize_error_message(str(exc)),
            "action": "Check the file or link and try again — or pick a different source.",
            "code": "ingest_failed",
        }
    if isinstance(exc, RenderError):
        return {
            "title": "Rendering failed",
            "message": sanitize_error_message(str(exc)),
            "action": "Retry the job. If it keeps failing, try a shorter clip.",
            "code": "render_failed",
        }
    if isinstance(exc, FileNotFoundError):
        return {
            "title": "A needed tool is missing",
            "message": sanitize_error_message(str(exc)),
            "action": "Press 'Install FFmpeg' in Settings, then retry the job.",
            "code": "tool_missing",
        }
    if isinstance(exc, TimeoutError):
        return {
            "title": "This step took too long",
            "message": "The step was stopped because it ran longer than expected.",
            "action": "Retry the job. If it keeps timing out, try a shorter video.",
            "code": "step_timeout",
        }
    where = f" during {stage}" if stage else ""
    return {
        "title": "Something went wrong",
        "message": f"The job stopped{where}: {sanitize_error_message(str(exc))}",
        "action": "Retry the job. If it keeps failing, check the run log on the job page.",
        "code": "job_failed",
    }


__all__ = ["JobCancelled", "plain_error", "sanitize_error_message"]
