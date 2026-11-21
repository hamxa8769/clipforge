"""Performance profile endpoint (v0.8.0, Worker A).

Router: ``GET /perf/profile``, ``POST /perf/profile``.
Registered in ``clipforge_api/app.py`` by the coordinator — this
module never imports ``app.py`` (circular import: app.py registers
this router).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from clipforge import perf as perf_mod

router = APIRouter(prefix="/perf", tags=["perf"])


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. This module must not import app.py (circular import).
    from clipforge.config import settings  # local import: cheap, no cycle

    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


class ProfileIn(BaseModel):
    """Set the profile mode: auto (recommended) or a fixed profile."""

    profile: str


@router.get("/profile")
def get_profile():
    """Active profile: hardware detected, mode, and full settings."""
    try:
        return perf_mod.get_active_profile(data_dir=_data_dir())
    except Exception as exc:  # hardware detection never raises, but be safe
        raise HTTPException(500, f"Could not read the performance profile: {exc}") from exc


@router.post("/profile")
def set_profile(payload: ProfileIn):
    """Save a profile override ("auto", "low", "balanced", "high").

    422 (plain language) on an unknown mode. Returns the newly
    active profile.
    """
    mode = (payload.profile or "").strip().lower()
    try:
        perf_mod.save_override(mode, data_dir=_data_dir())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"Could not save the performance profile: {exc}") from exc
    return perf_mod.get_active_profile(data_dir=_data_dir())


__all__ = ["router"]
