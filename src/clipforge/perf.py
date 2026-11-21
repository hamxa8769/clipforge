"""Performance profiles for ClipForge Pro (v0.8.0, Worker A).

Detects the machine's hardware (best-effort, stdlib only — never
crashes) and picks a performance profile the worker / pipeline /
batch queue read, so a slow machine processes one job at a time
while a strong one runs in parallel. The user can override the
automatic choice; the override is a small JSON file in the app data
dir (``perf_profile.json``) with ``{"mode": "auto"|"low"|"balanced"|"high"}``.

Recommendation thresholds (``recommend_profile``):

* ``low`` — no GPU detected, or <= 4 CPU cores, or <= 8 GB RAM.
* ``high`` — a GPU is present AND >= 8 cores AND >= 16 GB RAM.
* ``balanced`` — everything in between.

Profiles (``PROFILE_SETTINGS``) are fully specified per level so the
worker, pipeline and batch queue never guess a missing key.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

PROFILE_NAMES = ("low", "balanced", "high")
MODES = ("auto", *PROFILE_NAMES)
OVERRIDE_FILENAME = "perf_profile.json"

PROFILE_SETTINGS: dict[str, dict[str, object]] = {
    "low": {
        "whisper_model": "tiny",
        "preview": "480p",
        "max_concurrent_jobs": 1,
        "batch_max": 5,
        "animations": "reduced",
        "ffmpeg_preset": "veryfast",
        "max_parallel_clips": 1,
    },
    "balanced": {
        "whisper_model": "base",
        "preview": "720p",
        "max_concurrent_jobs": 2,
        "batch_max": 10,
        "animations": "full",
        "ffmpeg_preset": "fast",
        "max_parallel_clips": 2,
    },
    "high": {
        "whisper_model": "base",
        "preview": "1080p",
        "max_concurrent_jobs": 4,
        "batch_max": 20,
        "animations": "full",
        "ffmpeg_preset": "medium",
        "max_parallel_clips": 4,
    },
}


def _data_dir(data_dir: str | Path | None = None) -> Path:
    if data_dir:
        return Path(data_dir)
    from clipforge.config import settings  # local import: cheap, no cycle

    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _ram_gb_linux() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kb = float(line.split()[1])
                return round(kb / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _ram_gb_macos() -> float | None:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return round(int(out) / 1024**3, 1) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _ram_gb_windows() -> float | None:
    try:

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return round(status.ullTotalPhys / 1024**3, 1)
    except (OSError, AttributeError, ValueError):
        return None
    return None


def _gpu_name() -> str | None:
    """Best-effort GPU detection via nvidia-smi; None when absent/unknown.

    Never raises, never blocks long (5 s timeout). A machine without
    an NVIDIA GPU — or without the tool — simply reports no GPU.
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    try:
        out = subprocess.run(
            [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return out.splitlines()[0].strip() or None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def detect_hardware() -> dict:
    """Detect hardware with stdlib only. Never raises.

    Returns ``{cpu_cores, ram_gb, gpu, platform}`` where ``gpu`` is
    the GPU name or None, ``ram_gb`` is a float or None when it could
    not be read, and ``platform`` is e.g. "Windows"/"Linux"/"Darwin".
    """
    try:
        cores = os.cpu_count() or 1
    except Exception:
        cores = 1
    system = platform.system() or "unknown"
    ram: float | None = None
    try:
        if system == "Linux":
            ram = _ram_gb_linux()
        elif system == "Darwin":
            ram = _ram_gb_macos()
        elif system == "Windows":
            ram = _ram_gb_windows()
    except Exception:
        ram = None
    try:
        gpu = _gpu_name()
    except Exception:
        gpu = None
    return {
        "cpu_cores": int(cores),
        "ram_gb": ram,
        "gpu": gpu,
        "platform": system,
    }


def recommend_profile(hw: dict | None = None) -> str:
    """Recommend ``low`` / ``balanced`` / ``high`` for hardware.

    ``low`` when there is no GPU, or <= 4 cores, or <= 8 GB RAM
    (unknown RAM counts as <= 8 GB — assume the weaker machine).
    ``high`` when a GPU is present AND >= 8 cores AND >= 16 GB RAM.
    Otherwise ``balanced``. Never raises; missing info degrades to
    the safe (low) side.
    """
    try:
        hw = dict(hw or {})
        cores = int(hw.get("cpu_cores") or 1)
        ram = hw.get("ram_gb")
        ram_gb = float(ram) if ram is not None else 0.0
        gpu = hw.get("gpu")
    except (TypeError, ValueError):
        return "low"
    if not gpu or cores <= 4 or ram_gb <= 8:
        return "low"
    if cores >= 8 and ram_gb >= 16:
        return "high"
    return "balanced"


def _override_path(data_dir: str | Path | None = None) -> Path:
    return _data_dir(data_dir) / OVERRIDE_FILENAME


def load_override(data_dir: str | Path | None = None) -> str:
    """Read the persisted mode override; "auto" when absent/invalid."""
    try:
        data = json.loads(_override_path(data_dir).read_text(encoding="utf-8"))
        mode = str(data.get("mode", "auto")).lower()
        return mode if mode in MODES else "auto"
    except (OSError, ValueError, AttributeError):
        return "auto"


def save_override(mode: str, data_dir: str | Path | None = None) -> str:
    """Persist the mode override ("auto"|"low"|"balanced"|"high").

    Returns the stored mode. Raises ValueError on an unknown mode.
    A corrupt/unwritable file never crashes the caller — write
    errors are raised only from this explicit setter, and the API
    layer turns them into a plain-language 500.
    """
    mode = str(mode or "").lower()
    if mode not in MODES:
        raise ValueError(f"Unknown profile mode {mode!r} — use one of: {', '.join(MODES)}.")
    path = _override_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": mode}, indent=2), encoding="utf-8")
    tmp.replace(path)
    return mode


def get_active_profile(
    data_dir: str | Path | None = None,
    hw: dict | None = None,
) -> dict:
    """Resolve the active profile: override, or auto -> recommended.

    Returns ``{"profile": name, "mode": mode, "hardware": hw,
    "settings": {...}}`` — the settings dict is a copy, so callers
    cannot mutate the shared ``PROFILE_SETTINGS``.
    """
    mode = load_override(data_dir)
    hardware = hw if hw is not None else detect_hardware()
    name = mode if mode != "auto" else recommend_profile(hardware)
    return {
        "profile": name,
        "mode": mode,
        "hardware": hardware,
        "settings": dict(PROFILE_SETTINGS[name]),
    }


__all__ = [
    "MODES",
    "OVERRIDE_FILENAME",
    "PROFILE_NAMES",
    "PROFILE_SETTINGS",
    "detect_hardware",
    "get_active_profile",
    "load_override",
    "recommend_profile",
    "save_override",
]
