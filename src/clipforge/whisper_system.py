"""Detecting a working ``faster-whisper`` install — in-process or system Python.

Why this module exists (v0.10.2 root cause)
--------------------------------------------
The Windows server ships as a frozen PyInstaller one-file exe
(``clipforge-server.exe``). A frozen exe runs from its own bundled
archive: it never sees the system Python's ``site-packages``. So a
user who runs ``python -m pip install faster-whisper`` in PowerShell
installs the package into system Python — and the server, which only
probed with ``importlib.util.find_spec("faster_whisper")`` *inside
itself* (``clipforge.requirements.probe_whisper``) and only imported
it lazily *inside itself* (``clipforge.transcribe``), kept reporting
Whisper missing. The pip install was fine; the server was looking in
the wrong Python.

The fix is NOT to bundle faster-whisper into the exe: ctranslate2
brings large native DLLs, an AVX2 CPU requirement and a big size
increase, and bundling would still leave the user's own install (and
their pre-downloaded ~500 MB model) unused. Instead the server now
detects a system-Python install and, when the frozen exe lacks the
import, runs transcription in that Python as a subprocess:

* :func:`detect_faster_whisper` — one call that answers "is there a
  working faster-whisper anywhere I can use, and how do I reach it?"
  It tries the in-process import first (dev runs from source, and any
  future build that does bundle it), then scans system Pythons
  (``PATH``, the ``py`` launcher and well-known Windows install dirs)
  by *running* a tiny probe script in each — a package that exists
  but fails to import (missing native DLL, broken ctranslate2, …) is
  reported ``broken`` with its error, never "installed".
* :data:`TRANSCRIBE_CODE` — the ``python -c`` program the server runs
  in the detected Python to transcribe one WAV file; it prints a
  single JSON line (words with timestamps) on stdout.

Both paths share the same Hugging Face model cache: the subprocess
inherits the server's environment, so ``HF_HUB_CACHE`` / ``HF_HOME``
(or the default ``%USERPROFILE%\\.cache\\huggingface``) resolves to
the same directory — a model the user pre-downloaded with
``python -c "from faster_whisper import WhisperModel;
WhisperModel('small')"`` is found by either path. If the model lives
somewhere else, point both Pythons at it with ``HF_HUB_CACHE``.

A side benefit: transcription in a subprocess is the fix the v0.9.3
diagnosis asked for — a native-code crash or OOM inside the model
load can no longer kill the whole uvicorn server process; it fails
one job with a plain message instead.
"""

from __future__ import annotations

import glob
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: How long a cached detection result is trusted. Transcription is
#: called per live-monitor chunk, so re-probing (which spawns a
#: Python that imports ctranslate2) on every call would be slow;
#: the requirements panel always probes fresh (``fresh=True``) so a
#: just-finished ``pip install`` shows up on Re-check immediately.
_DETECTION_TTL_S = 60.0

#: Default per-candidate timeout for the probe script. Importing
#: faster-whisper loads ctranslate2, which takes a few seconds on a
#: cold machine — 5 s (the requirements check's tool timeout) is too
#: short for it.
DEFAULT_PROBE_TIMEOUT_S = 30.0

#: ``python -c`` program used to probe one candidate Python. Prints
#: exactly one line on stdout and always exits 0 when it ran:
#: ``FW_OK <version>\\t<package file>``, ``FW_MISSING`` (no module
#: named ``faster_whisper``), or ``FW_BROKEN <reason>`` (the import
#: exists but fails — e.g. a missing native DLL). A missing
#: *dependency* of faster-whisper (``ctranslate2``, ``onnxruntime``)
#: is ``FW_BROKEN``, not ``FW_MISSING``: the package is there, it
#: just cannot run. Stderr is ignored (import warnings land there).
PROBE_CODE = """# clipforge-fw-probe-v1
import sys
try:
    import faster_whisper
except ModuleNotFoundError as e:
    if e.name == "faster_whisper":
        print("FW_MISSING")
    else:
        print("FW_BROKEN missing dependency: " + str(e)[:300])
    sys.exit(0)
except Exception as e:
    print("FW_BROKEN " + repr(e)[:300])
    sys.exit(0)
try:
    from importlib.metadata import version as _v
    _ver = _v("faster-whisper")
except Exception:
    _ver = getattr(faster_whisper, "__version__", "")
# PyAV 19 removed the `metadata_errors` argument to av.open(), but
# faster-whisper 1.2.1 still passes it in decode_audio() — a fresh
# `pip install faster-whisper` today gets av 19 and EVERY
# transcription crashes with TypeError. The import above succeeds,
# so only a real call proves it: open a tiny in-memory WAV the same
# way faster-whisper does. A TypeError naming metadata_errors means
# this install can never transcribe — report it broken with the fix
# (`pip install "av<19"`), never "installed".
try:
    import av as _av
    import io as _io
    import wave as _wave
    _buf = _io.BytesIO()
    with _wave.open(_buf, "wb") as _w:
        _w.setnchannels(1)
        _w.setsampwidth(2)
        _w.setframerate(16000)
        _w.writeframes(b"\\x00\\x00" * 1600)
    _buf.seek(0)
    _c = _av.open(_buf, mode="r", metadata_errors="ignore")
    _c.close()
except TypeError as _e:
    if "metadata_errors" in str(_e):
        try:
            from importlib.metadata import version as _v2
            _avv = _v2("av")
        except Exception:
            _avv = "19+"
        print(
            "FW_BROKEN faster-whisper " + _ver + " is incompatible with PyAV " + _avv
            + ": it passes metadata_errors= to av.open(), which PyAV 19 removed, "
            + "so every transcription crashes. Fix: pip install \\"av<19\\" "
            + "in the same Python, then press Re-check."
        )
        sys.exit(0)
except Exception:
    pass
print("FW_OK " + _ver + "\\t" + str(getattr(faster_whisper, "__file__", "")))
"""

#: ``python -c`` program used to transcribe one WAV file in the
#: detected system Python. ``argv``: ``wav_path model language``
#: (empty language = auto-detect). Prints a single JSON line on
#: stdout: ``{"words": [{"text", "start", "end"}, ...],
#: "language": ...}``. Everything else (model download progress,
#: warnings) goes to stderr, which the caller only reads on failure.
TRANSCRIBE_CODE = """# clipforge-fw-transcribe-v1
import json, sys
wav_path, model, language = sys.argv[1], sys.argv[2], sys.argv[3] or None
from faster_whisper import WhisperModel
fw = WhisperModel(model, device="auto", compute_type="int8")
segments, info = fw.transcribe(wav_path, language=language, word_timestamps=True)
words = []
for seg in segments:
    for w in seg.words or []:
        words.append({"text": w.word.strip(), "start": float(w.start), "end": float(w.end)})
print(json.dumps({"words": words, "language": getattr(info, "language", None) or language or "en"}))
"""

_CACHE: dict = {"at": 0.0, "result": None}


def clear_detection_cache() -> None:
    """Forget the cached :func:`detect_faster_whisper` result."""
    _CACHE["at"] = 0.0
    _CACHE["result"] = None


def _candidate_pythons() -> list[tuple[list[str], str]]:
    """System Pythons worth probing, as (argv prefix, display string).

    Order: this process's Python (only when NOT frozen — a frozen
    exe's ``sys.executable`` is the exe itself, not a Python), then
    ``python`` / ``python3`` from ``PATH``, then the Windows ``py``
    launcher, then well-known Windows install dirs. Duplicates are
    removed. The Microsoft Store stub (``%LOCALAPPDATA%\\Microsoft\\
    WindowsApps\\python.exe``, a 0-byte reparse point that opens the
    Store instead of running code) is skipped on Windows.
    """
    candidates: list[tuple[list[str], str]] = []
    seen: set[str] = set()

    def _add(argv: list[str], display: str) -> None:
        key = "\x00".join(argv)
        if key in seen:
            return
        seen.add(key)
        candidates.append((argv, display))

    if not getattr(sys, "frozen", False):
        _add([sys.executable], sys.executable)
    for name in ("python", "python3"):
        found = shutil.which(name)
        if not found:
            continue
        if sys.platform == "win32":
            try:
                if Path(found).stat().st_size == 0:
                    continue  # Store stub, not a real Python
            except OSError:
                continue
        _add([found], found)
    if sys.platform == "win32":
        py_launcher = shutil.which("py")
        if py_launcher:
            _add([py_launcher, "-3"], "py -3 (Python Launcher for Windows)")
            _add([py_launcher], "py (Python Launcher for Windows)")
        for base in (
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
        ):
            if not base:
                continue
            for exe in glob.glob(
                os.path.join(base, "Programs", "Python", "Python3*", "python.exe")
            ):
                _add([exe], exe)
            for exe in glob.glob(os.path.join(base, "Python3*", "python.exe")):
                _add([exe], exe)
    return candidates


def _run_probe(argv: list[str], timeout: float) -> dict:
    """Run :data:`PROBE_CODE` in one candidate Python; parse its line.

    Returns ``{"found", "broken", "version", "package_path",
    "broken_reason"}``. A candidate that cannot start, hangs or
    crashes is simply not a usable Python (``found`` False, not
    broken) — "broken" is reserved for a Python that runs but whose
    faster-whisper import fails.
    """
    try:
        proc = subprocess.run(
            [*argv, "-c", PROBE_CODE],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "found": False,
            "broken": False,
            "version": "",
            "package_path": "",
            "broken_reason": "",
        }
    line = (proc.stdout or "").strip().splitlines()
    first = line[0] if line else ""
    if first.startswith("FW_OK"):
        rest = first[len("FW_OK") :].strip()
        version, _, package_path = rest.partition("\t")
        return {
            "found": True,
            "broken": False,
            "version": version.strip(),
            "package_path": package_path.strip(),
            "broken_reason": "",
        }
    if first.startswith("FW_BROKEN"):
        return {
            "found": False,
            "broken": True,
            "version": "",
            "package_path": "",
            "broken_reason": first[len("FW_BROKEN") :].strip()
            or "faster-whisper fails to import in this Python",
        }
    # FW_MISSING, empty output, or anything else: not installed here.
    return {
        "found": False,
        "broken": False,
        "version": "",
        "package_path": "",
        "broken_reason": "",
    }


def _in_process_version(module: object) -> str:
    try:
        return importlib.metadata.version("faster-whisper")
    except Exception:  # noqa: BLE001  (not installed as a dist, or metadata unreadable)
        return str(getattr(module, "__version__", ""))


def _av_metadata_errors_broken() -> str | None:
    """Reason a faster-whisper install can never transcribe, or None.

    PyAV 19 removed the ``metadata_errors`` argument to ``av.open()``,
    but faster-whisper 1.2.1 still passes it in ``decode_audio()`` —
    a fresh ``pip install faster-whisper`` today gets av 19 and every
    transcription crashes with ``TypeError``. The import succeeds, so
    only a real call proves it: open a tiny in-memory WAV the same
    way faster-whisper does. Any other outcome (kwarg accepted, av
    missing — which the faster-whisper import would already have
    caught) means "not this problem". Mirrors the check in
    :data:`PROBE_CODE` (which runs the same call in system Pythons).
    """
    try:
        import io
        import wave

        import av

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 1600)
        buf.seek(0)
        container = av.open(buf, mode="r", metadata_errors="ignore")
        container.close()
    except TypeError as exc:
        if "metadata_errors" not in str(exc):
            return None
        try:
            av_version = importlib.metadata.version("av")
        except Exception:  # noqa: BLE001  (metadata unreadable — version is a nicety)
            av_version = "19+"
        try:
            fw_version = importlib.metadata.version("faster-whisper")
        except Exception:  # noqa: BLE001
            fw_version = "1.2.1"
        return (
            f"faster-whisper {fw_version} is incompatible with PyAV {av_version}: "
            "it passes metadata_errors= to av.open(), which PyAV 19 removed, "
            'so every transcription crashes. Fix: pip install "av<19" in the '
            "same Python, then press Re-check."
        )
    except Exception:  # noqa: BLE001  (kwarg accepted, or av missing — not this problem)
        return None
    return None


def _not_found() -> dict:
    return {
        "available": False,
        "via": "none",
        "python_argv": None,
        "python": "",
        "version": "",
        "package_path": "",
        "source": "",
        "broken": False,
        "broken_reason": "",
    }


def detect_faster_whisper(timeout: float = DEFAULT_PROBE_TIMEOUT_S, *, fresh: bool = False) -> dict:
    """Find a working faster-whisper, in-process or in system Python.

    Returns ``{"available", "via", "python_argv", "python",
    "version", "package_path", "source", "broken", "broken_reason"}``
    where ``via`` is ``"in-process"`` (imported into this server
    process — dev runs from source), ``"system-python"`` (reachable
    only by running the detected Python as a subprocess — the frozen
    exe case) or ``"none"``. ``python_argv`` is the argv prefix to
    run code in that Python (``None`` for in-process); ``python`` is
    the display string for the UI; ``source`` names where it was
    found. When nothing usable exists but some Python has a
    faster-whisper that fails to import, ``broken`` is True with the
    import error in ``broken_reason`` — never reported as installed.

    Results are cached for :data:`_DETECTION_TTL_S` unless
    ``fresh=True`` (the requirements panel always probes fresh).
    """
    now = time.monotonic()
    if not fresh and _CACHE["result"] is not None and now - _CACHE["at"] < _DETECTION_TTL_S:
        return dict(_CACHE["result"])

    result = _detect_fresh(timeout)
    _CACHE["at"] = now
    _CACHE["result"] = result
    return dict(result)


def _detect_fresh(timeout: float) -> dict:
    # 1. In-process first: dev runs from source, and any future build
    # that bundles faster-whisper. A spec that exists but an import
    # that fails is a broken install, not a missing one.
    if importlib.util.find_spec("faster_whisper") is not None:
        try:
            import faster_whisper as _fw  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001  (DLL load, missing dep, …)
            result = _not_found()
            result["broken"] = True
            result["broken_reason"] = (
                f"faster-whisper is installed in the server Python but fails to import: {exc!r}"
            )
            result["python"] = sys.executable
            result["source"] = "in-process (server Python)"
            return result
        av_problem = _av_metadata_errors_broken()
        if av_problem is not None:
            result = _not_found()
            result["broken"] = True
            result["broken_reason"] = av_problem
            result["python"] = sys.executable
            result["source"] = "in-process (server Python)"
            return result
        return {
            "available": True,
            "via": "in-process",
            "python_argv": None,
            "python": sys.executable,
            "version": _in_process_version(_fw),
            "package_path": str(getattr(_fw, "__file__", "")),
            "source": "in-process (server Python)",
            "broken": False,
            "broken_reason": "",
        }

    # 2. System Python(s): the frozen-exe case. A `pip install
    # faster-whisper` in PowerShell lands here, invisible to the
    # frozen process but perfectly usable as a subprocess.
    first_broken: dict | None = None
    first_broken_display = ""
    for argv, display in _candidate_pythons():
        probe = _run_probe(argv, timeout)
        if probe["found"]:
            return {
                "available": True,
                "via": "system-python",
                "python_argv": argv,
                "python": display,
                "version": probe["version"],
                "package_path": probe["package_path"],
                "source": f"system Python ({display})",
                "broken": False,
                "broken_reason": "",
            }
        if probe["broken"] and first_broken is None:
            first_broken = probe
            first_broken_display = display
    if first_broken is not None:
        result = _not_found()
        result["broken"] = True
        result["broken_reason"] = (
            f"faster-whisper is installed in {first_broken_display} but cannot "
            f"transcribe: {first_broken['broken_reason']}"
        )
        result["python"] = first_broken_display
        result["source"] = f"system Python ({first_broken_display})"
        return result
    return _not_found()


def parse_transcript_json(stdout: str) -> dict:
    """Parse the one JSON line :data:`TRANSCRIBE_CODE` prints.

    The child prints exactly one JSON object; scan from the last
    line up so a stray stdout line cannot break parsing.
    """
    lines = [ln.strip() for ln in (stdout or "").strip().splitlines() if ln.strip()]
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("words"), list):
            return payload
    raise ValueError("transcription subprocess did not return a JSON transcript")


__all__ = [
    "DEFAULT_PROBE_TIMEOUT_S",
    "PROBE_CODE",
    "TRANSCRIBE_CODE",
    "clear_detection_cache",
    "detect_faster_whisper",
    "parse_transcript_json",
]
