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

import os
from datetime import UTC, datetime
from pathlib import Path

from clipforge.ffmpeg_install import probe_ffmpeg, probe_ffprobe
from clipforge.whisper_system import DEFAULT_PROBE_TIMEOUT_S, detect_faster_whisper
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


def probe_whisper(
    model: str = DEFAULT_WHISPER_MODEL, timeout: float = DEFAULT_PROBE_TIMEOUT_S
) -> dict:
    """Whisper backend availability, without running a transcription.

    ``engine`` is ``"faster-whisper"`` (local), ``"whisper-api"`` (an
    OpenAI-compatible key is configured) or ``"none"``. ``status`` is:

    * ``ok`` — a backend exists and is ready (local model already
      downloaded, or the API key path which needs no model);
    * ``degraded`` — faster-whisper is installed but the model is not
      downloaded yet (transcription works, but the first run downloads
      the model, ~500 MB for the default 'small' model, with no
      progress shown), or the local install is broken but the API key
      path still works (the broken install is named in the detail —
      it is never hidden);
    * ``missing`` — no backend at all: no faster-whisper install and
      no ``OPENAI_API_KEY``;
    * ``broken`` — a faster-whisper install exists but fails to
      import (missing native DLL, broken dependency, …) and there is
      no API key to fall back on. A package that exists but does not
      run is reported ``broken`` with its path, never "installed".

    Detection covers both ways a local install can exist (v0.10.2):
    importable in this server process, or installed in a system
    Python — the frozen ``clipforge-server.exe`` cannot see the
    system Python's packages, so a ``pip install faster-whisper``
    in PowerShell is found by probing system Pythons and used via a
    subprocess (see :mod:`clipforge.whisper_system`). Either way the
    reported ``version`` is the real package version and ``path`` /
    ``source`` say where it was found. The model cache is shared
    between both paths (same ``HF_HUB_CACHE`` / ``HF_HOME``), so a
    model pre-downloaded with system Python counts as cached here.

    No key *value* is ever read or returned — presence only.
    """
    # Always probe fresh: the user may have just run `pip install
    # faster-whisper` and pressed Re-check — a cached "missing"
    # would lie to them for a minute.
    detection = detect_faster_whisper(timeout=max(timeout, 20.0), fresh=True)
    api_key = bool(os.environ.get("OPENAI_API_KEY"))
    if detection["available"]:
        cached = _whisper_model_cached(model)
        via = detection["via"]
        where = (
            f"faster-whisper {detection['version'] or 'unknown version'} via {detection['source']}"
        )
        if cached:
            return {
                "found": True,
                "engine": "faster-whisper",
                "model": model,
                "model_cached": True,
                "status": "ok",
                "version": detection["version"],
                "path": detection["python"] or detection["package_path"],
                "source": detection["source"],
                "via": via,
                "broken": False,
                "broken_reason": "",
                "detail": (
                    f"{where} and the '{model}' model is already downloaded "
                    "— transcription starts at once."
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
            "version": detection["version"],
            "path": detection["python"] or detection["package_path"],
            "source": detection["source"],
            "via": via,
            "broken": False,
            "broken_reason": "",
            "detail": (
                f"{where} but the '{model}' model is not downloaded yet "
                "— the first transcription downloads it (about 500 MB) "
                "with no progress shown. If you already downloaded the "
                "model with another Python, point this app at the same "
                "cache with the HF_HUB_CACHE environment variable, or set "
                "OPENAI_API_KEY instead to use the Whisper API and skip "
                "that download."
            ),
            "fix_action": "settings",
            "fix_label": "Set an OpenAI key in Settings to skip the download",
        }
    if detection["broken"]:
        # The broken reason already names its own fix when one is known
        # (e.g. PyAV 19: `pip install "av<19"`). A plain
        # `--force-reinstall faster-whisper` would re-pull the same broken
        # dependency (av 19) in that case, so only suggest it when the
        # reason does not already carry a specific fix.
        if "av<19" in detection["broken_reason"]:
            broken_fix = "apply the fix above, then press Re-check."
        else:
            broken_fix = (
                "reinstall it with 'python -m pip install "
                "--force-reinstall faster-whisper' (in the same "
                "PowerShell/Python you installed it with), then press "
                "Re-check."
            )
        broken_detail = (
            f"{detection['broken_reason']} — transcription with the local "
            f"install will fail until it is fixed: {broken_fix}"
        )
        if api_key:
            return {
                "found": True,
                "engine": "whisper-api",
                "model": model,
                "model_cached": False,
                "status": "degraded",
                "version": "",
                "path": detection["python"],
                "source": detection["source"],
                "via": "none",
                "broken": True,
                "broken_reason": detection["broken_reason"],
                "detail": (
                    f"{broken_detail} Transcription still works via the Whisper API key for now."
                ),
                "fix_action": "settings",
                "fix_label": "Fix the faster-whisper install, or keep using the API key",
            }
        return {
            "found": False,
            "engine": "none",
            "model": model,
            "model_cached": False,
            "status": "broken",
            "version": detection["version"],
            "path": detection["python"],
            "source": detection["source"],
            "via": "none",
            "broken": True,
            "broken_reason": detection["broken_reason"],
            "detail": broken_detail,
            "fix_action": "settings",
            "fix_label": "Reinstall faster-whisper or set OPENAI_API_KEY in Settings",
        }
    if api_key:
        return {
            "found": True,
            "engine": "whisper-api",
            "model": model,
            "model_cached": False,
            "status": "ok",
            "version": "",
            "path": "",
            "source": "OPENAI_API_KEY",
            "via": "none",
            "broken": False,
            "broken_reason": "",
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
        "version": "",
        "path": "",
        "source": "",
        "via": "none",
        "broken": False,
        "broken_reason": "",
        "detail": (
            "No transcription backend: faster-whisper is not installed "
            "and OPENAI_API_KEY is not set, so there is nothing to "
            "transcribe with. Install it in PowerShell with "
            "'python -m pip install faster-whisper' — the app detects "
            "a system-Python install on Re-check, no restart needed — "
            "or set OPENAI_API_KEY in Settings instead."
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

    wh = probe_whisper(whisper_model, timeout=timeout)
    requirements.append(
        _requirement(
            "whisper",
            "Whisper transcription",
            wh["status"],
            version=wh.get("version") or (wh["engine"] if wh["engine"] != "none" else ""),
            path=wh.get("path", ""),
            source=wh.get("source", ""),
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
