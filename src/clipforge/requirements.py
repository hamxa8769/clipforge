"""Complete pre-flight requirements verification for ClipForge (v0.9.2).

The user reported (v0.9.1) a job failing *mid-download* with a yt-dlp
error even though yt-dlp was installed. The cause was a gap in how
requirements were checked:

* v0.9.1 verified yt-dlp by actually running ``yt-dlp --version``
  (:func:`clipforge.ytdlp_install.probe_yt_dlp`), but FFmpeg /
  ffprobe were only checked for *existence on disk*
  (``resolve_ffmpeg_path``) — a broken FFmpeg counted as installed,
  the job gate passed, and the render failed mid-way with a raw
  ffmpeg error.
* Whisper had no pre-flight at all: the backend was only picked when
  transcription started, so a missing backend (or a first-run model
  download of ~500 MB with no progress) surfaced mid-pipeline.
* Nothing verified anything at server startup, and there was no
  single endpoint that answered "is everything the pipeline needs
  actually working right now?" — ``GET /dependencies`` only reported
  presence, and ``/ffmpeg/status`` / ``/yt-dlp/status`` were two
  separate shapes.

This module is the single place that answers that question. Every
check *runs* the tool (``--version`` / ``-version``) or, for Whisper,
proves a backend exists — a binary that exists but does not run is
reported ``broken`` with its path, never "installed". The API exposes
it at ``GET /requirements/status`` and ``POST /requirements/recheck``,
the server runs it once at startup (see ``clipforge_api.app``), and
the job gates in :mod:`clipforge.workflow` (``require_tool``) and the
agent pre-flight (:func:`clipforge.agent._require_pipeline_tools`)
use the same probes — so a run cannot start a download it cannot
finish: a genuinely missing/broken tool blocks the start with a
plain-language message naming the requirement and the one-click fix.

Licence behaviour is untouched (v0.9.1 advisory only): requirements
are about tools, never about rights.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path

from clipforge.ffmpeg_install import probe_ffmpeg, probe_ffprobe
from clipforge.ytdlp_install import probe_yt_dlp

#: Statuses a requirement can be in. ``degraded`` means usable but
#: with a caveat the user should know about (Whisper: the model is not
#: downloaded yet, so the first transcription downloads ~500 MB) —
#: it does NOT block a run.
STATUSES = ("ok", "missing", "broken", "degraded")

#: The Whisper model the pipeline transcribes with by default
#: (see :func:`clipforge.transcribe.transcribe`).
DEFAULT_WHISPER_MODEL = "small"


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _requirement(
    req_id: str,
    label: str,
    status: str,
    *,
    version: str = "",
    path: str = "",
    source: str = "",
    detail: str = "",
    fix_action: str = "",
    fix_label: str = "",
) -> dict:
    return {
        "id": req_id,
        "label": label,
        "status": status,
        "version": version,
        "path": path,
        "source": source,
        "detail": detail,
        "fix_action": fix_action,
        "fix_label": fix_label,
    }


def probe_tool(
    name: str,
    data_dir: str | Path | None = None,
    stored_path: str | None = None,
    timeout: float = 10.0,
) -> dict:
    """Verified probe for ``yt-dlp`` / ``ffmpeg`` / ``ffprobe``.

    Dispatches to the matching ``probe_*`` (each runs the binary's
    ``--version`` / ``-version``). Returns the probe dict: ``found``,
    ``path``, ``version``, ``broken``, ``broken_reason``, ``source``.
    Unknown names fall back to a PATH lookup (presence only).
    """
    import shutil  # noqa: PLC0415  (fallback path only)

    if name == "yt-dlp":
        return probe_yt_dlp(data_dir, stored_path, timeout=timeout)
    if name == "ffmpeg":
        return probe_ffmpeg(data_dir, stored_path, timeout=timeout)
    if name == "ffprobe":
        return probe_ffprobe(data_dir, stored_path, timeout=timeout)
    found = shutil.which(name)
    return {
        "found": found is not None,
        "path": found or "",
        "version": "",
        "broken": False,
        "broken_reason": "",
        "source": "PATH" if found else "",
    }


# ------------------------------------------------------------------ Whisper
def _whisper_model_cached(model: str) -> bool:
    """True when faster-whisper's ``model`` is already downloaded.

    faster-whisper downloads from Hugging Face into the hub cache
    (``$HF_HUB_CACHE`` / ``$HF_HOME/hub`` /
    ``~/.cache/huggingface/hub``), under
    ``models--Systran--faster-whisper-<model>/snapshots/*/model.bin``.
    A cached model means the first transcription starts at once; an
    uncached one means a ~500 MB download (default 'small' model) with
    no progress shown. Pure filesystem check, no network, no import.
    """
    hub = os.environ.get("HF_HUB_CACHE")
    if not hub:
        hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
        hub = str(Path(hf_home) / "hub")
    repo = Path(hub) / f"models--Systran--faster-whisper-{model}"
    snapshots = repo / "snapshots"
    if not snapshots.is_dir():
        return False
    for snap in snapshots.iterdir():
        if snap.is_dir() and (snap / "model.bin").is_file():
            return True
    return False


def probe_whisper(model: str = DEFAULT_WHISPER_MODEL) -> dict:
    """Whisper backend availability, without running a transcription.

    ``engine`` is ``"faster-whisper"`` (local), ``"whisper-api"`` (an
    OpenAI-compatible key is configured) or ``"none"``. ``status`` is:

    * ``ok`` — a backend exists and is ready (local model already
      downloaded, or the API key path which needs no model);
    * ``degraded`` — faster-whisper is installed but the model is not
      downloaded yet: transcription works, but the first run downloads
      the model (~500 MB for the default 'small' model) with no
      progress shown;
    * ``missing`` — no backend at all: no faster-whisper install and
      no ``OPENAI_API_KEY``.

    No key *value* is ever read or returned — presence only.
    """
    local = importlib.util.find_spec("faster_whisper") is not None
    api_key = bool(os.environ.get("OPENAI_API_KEY"))
    if local:
        cached = _whisper_model_cached(model)
        if cached:
            return {
                "found": True,
                "engine": "faster-whisper",
                "model": model,
                "model_cached": True,
                "status": "ok",
                "detail": (
                    f"faster-whisper is installed and the '{model}' model "
                    "is already downloaded — transcription starts at once."
                ),
                "fix_action": "",
                "fix_label": "",
            }
        return {
            "found": True,
            "engine": "faster-whisper",
            "model": model,
            "model_cached": False,
            "status": "degraded",
            "detail": (
                f"faster-whisper is installed but the '{model}' model is "
                "not downloaded yet — the first transcription downloads "
                "it (about 500 MB) with no progress shown. Set "
                "OPENAI_API_KEY instead to use the Whisper API and skip "
                "that download."
            ),
            "fix_action": "settings",
            "fix_label": "Set an OpenAI key in Settings to skip the download",
        }
    if api_key:
        return {
            "found": True,
            "engine": "whisper-api",
            "model": model,
            "model_cached": False,
            "status": "ok",
            "detail": (
                "Whisper API key is configured — transcription uses the "
                "API and needs no model download."
            ),
            "fix_action": "",
            "fix_label": "",
        }
    return {
        "found": False,
        "engine": "none",
        "model": model,
        "model_cached": False,
        "status": "missing",
        "detail": (
            "No transcription backend: faster-whisper is not installed "
            "and OPENAI_API_KEY is not set, so there is nothing to "
            "transcribe with."
        ),
        "fix_action": "settings",
        "fix_label": "Install faster-whisper or set OPENAI_API_KEY in Settings",
    }


def _sibling_ffprobe_path(stored_ffmpeg_path: str | None) -> str | None:
    """ffprobe next to a saved ffmpeg binary, if that layout exists.

    The saved ``ffmpeg_path`` is the ffmpeg binary itself, not an
    ffprobe — probing it *as* ffprobe would wrongly report the ffmpeg
    binary as a working ffprobe. A manual FFmpeg install keeps both
    binaries in one ``bin`` dir, so the sibling is the right stored
    candidate; ``None`` when there is no saved path (the probe then
    falls back to the bundled binary, PATH and known locations).
    """
    if not stored_ffmpeg_path:
        return None
    ffmpeg = Path(stored_ffmpeg_path)
    name = "ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"
    sibling = ffmpeg.parent / name
    return str(sibling) if sibling.is_file() else None


# ------------------------------------------------------------------ the check
def check_requirements(
    data_dir: str | Path | None = None,
    *,
    stored_ffmpeg_path: str | None = None,
    stored_yt_dlp_path: str | None = None,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    timeout: float = 5.0,
) -> dict:
    """Verify every requirement the pipeline needs, by running it.

    Returns ``{"requirements": [...], "all_ready": bool,
    "blocking": [...], "checked_at": iso}``. Each requirement is
    ``{id, label, status, version, path, source, detail, fix_action,
    fix_label}`` where ``status`` is ``ok`` / ``missing`` / ``broken``
    / ``degraded``. ``all_ready`` is False when anything is
    ``missing`` or ``broken`` (``degraded`` does not block a run —
    it is a caveat, not a failure). ``blocking`` lists the ids that
    are missing or broken, in check order.
    """
    if data_dir is None:
        data_dir = os.environ.get("CLIPFORGE_DATA_DIR") or "./data"
    requirements: list[dict] = []

    yt = probe_yt_dlp(data_dir, stored_yt_dlp_path, timeout=timeout)
    if yt["found"]:
        requirements.append(
            _requirement(
                "yt-dlp",
                "yt-dlp",
                "ok",
                version=yt["version"],
                path=yt["path"],
                source=yt["source"],
                detail=(
                    f"yt-dlp {yt['version']} is installed and runs — YouTube downloads will work."
                ),
            )
        )
    elif yt["broken"]:
        requirements.append(
            _requirement(
                "yt-dlp",
                "yt-dlp",
                "broken",
                path=yt["path"],
                source=yt["source"],
                detail=yt["broken_reason"],
                fix_action="install-yt-dlp",
                fix_label="Reinstall yt-dlp",
            )
        )
    else:
        requirements.append(
            _requirement(
                "yt-dlp",
                "yt-dlp",
                "missing",
                detail=(
                    "yt-dlp is not installed — ClipForge needs it to "
                    "download YouTube sources. If you just installed it "
                    "yourself (PowerShell, pip or winget), press "
                    "'Re-check' first — no restart needed."
                ),
                fix_action="install-yt-dlp",
                fix_label="Install yt-dlp",
            )
        )

    ff = probe_ffmpeg(data_dir, stored_ffmpeg_path, timeout=timeout)
    if ff["found"]:
        requirements.append(
            _requirement(
                "ffmpeg",
                "FFmpeg",
                "ok",
                version=ff["version"],
                path=ff["path"],
                source=ff["source"],
                detail="FFmpeg is installed and runs — clipping will work.",
            )
        )
    elif ff["broken"]:
        requirements.append(
            _requirement(
                "ffmpeg",
                "FFmpeg",
                "broken",
                path=ff["path"],
                source=ff["source"],
                detail=ff["broken_reason"],
                fix_action="install-ffmpeg",
                fix_label="Reinstall FFmpeg",
            )
        )
    else:
        requirements.append(
            _requirement(
                "ffmpeg",
                "FFmpeg",
                "missing",
                detail=(
                    "FFmpeg is not installed — ClipForge needs it to "
                    "render clips and extract audio."
                ),
                fix_action="install-ffmpeg",
                fix_label="Install FFmpeg",
            )
        )

    fp = probe_ffprobe(data_dir, _sibling_ffprobe_path(stored_ffmpeg_path), timeout=timeout)
    if fp["found"]:
        requirements.append(
            _requirement(
                "ffprobe",
                "ffprobe",
                "ok",
                version=fp["version"],
                path=fp["path"],
                source=fp["source"],
                detail="ffprobe is installed and runs — source files can be validated.",
            )
        )
    elif fp["broken"]:
        requirements.append(
            _requirement(
                "ffprobe",
                "ffprobe",
                "broken",
                path=fp["path"],
                source=fp["source"],
                detail=fp["broken_reason"],
                fix_action="install-ffmpeg",
                fix_label="Reinstall FFmpeg (ffprobe ships with it)",
            )
        )
    else:
        requirements.append(
            _requirement(
                "ffprobe",
                "ffprobe",
                "missing",
                detail=(
                    "ffprobe is not installed — it ships with FFmpeg and "
                    "ClipForge needs it to validate source files."
                ),
                fix_action="install-ffmpeg",
                fix_label="Install FFmpeg (ffprobe ships with it)",
            )
        )

    wh = probe_whisper(whisper_model)
    requirements.append(
        _requirement(
            "whisper",
            "Whisper transcription",
            wh["status"],
            version=wh["engine"] if wh["engine"] != "none" else "",
            detail=wh["detail"],
            fix_action=wh["fix_action"],
            fix_label=wh["fix_label"],
        )
    )

    blocking = [r["id"] for r in requirements if r["status"] in ("missing", "broken")]
    return {
        "requirements": requirements,
        "all_ready": not blocking,
        "blocking": blocking,
        "checked_at": _utcnow_iso(),
    }


__all__ = [
    "DEFAULT_WHISPER_MODEL",
    "STATUSES",
    "check_requirements",
    "probe_tool",
    "probe_whisper",
]
