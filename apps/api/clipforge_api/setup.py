"""Setup-wizard state (v0.5.0, worker 3).

GET/POST /api/setup/state — the single source of truth for the proper
first-run setup wizard (FFmpeg -> Gemini key -> channel -> niche+source
-> turn on the agent). The wizard is skippable step-by-step and resumable:
``current_step`` and per-step ``done`` flags survive restarts.

Storage: ``<data_dir>/setup.json`` — a plain JSON file next to
``settings.json``, owned entirely by this module. It deliberately does
NOT touch the agent engine or the LLM key store (worker 2 owns those):
no secret is ever accepted or stored here, only the niche / source
choices and which steps are done. The Gemini key itself is saved through
the LLM endpoints, never here.

Registered in ``clipforge_api/app.py`` in one clearly-marked block.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

router = APIRouter(prefix="/api/setup", tags=["setup"])

# Niche presets shown in the wizard / Agent page (free text also allowed).
NICHE_PRESETS = (
    "gaming",
    "tech",
    "vlogging",
    "education",
    "finance",
    "fitness",
    "cooking",
    "music",
)

STEP_KEYS = ("ffmpeg", "gemini", "channel", "niche_source", "agent")
SOURCE_TYPES = ("video", "channel")

DEFAULT_STATE: dict[str, Any] = {
    "current_step": 0,
    "steps": {key: False for key in STEP_KEYS},
    "niche": "",
    "source_type": "video",
    "source_url": "",
    "completed": False,
}


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. This module must not import app.py (circular import:
    # app.py registers this router).
    from clipforge.config import settings  # local import: cheap, no cycle

    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _path() -> Path:
    return _data_dir() / "setup.json"


def _normalize(raw: Any) -> dict[str, Any]:
    """Validate/coerce a stored or incoming state dict into the shape the
    UI expects. Unknown keys and bad values are dropped, never trusted."""
    state: dict[str, Any] = json.loads(json.dumps(DEFAULT_STATE))
    if not isinstance(raw, dict):
        return state
    steps = raw.get("steps")
    if isinstance(steps, dict):
        for key in STEP_KEYS:
            if steps.get(key) is True:
                state["steps"][key] = True
    current = raw.get("current_step")
    if isinstance(current, int) and 0 <= current < len(STEP_KEYS):
        state["current_step"] = current
    niche = raw.get("niche")
    if isinstance(niche, str):
        state["niche"] = niche.strip()[:60]
    source_type = raw.get("source_type")
    if source_type in SOURCE_TYPES:
        state["source_type"] = source_type
    source_url = raw.get("source_url")
    if isinstance(source_url, str):
        state["source_url"] = source_url.strip()[:2048]
    if raw.get("completed") is True:
        state["completed"] = True
    return state


def _load() -> dict[str, Any]:
    try:
        return _normalize(json.loads(_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _normalize(None)


def _save(state: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


class SetupStateUpdate(BaseModel):
    """Partial setup-state update. Omitted = keep; unknown fields are
    rejected (422) so a typo can never silently drop a step."""

    model_config = ConfigDict(extra="forbid")

    current_step: int | None = None
    steps: dict[str, bool] | None = None
    niche: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    completed: bool | None = None

    @field_validator("current_step")
    @classmethod
    def _check_step(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value < len(STEP_KEYS):
            raise ValueError(f"current_step must be 0..{len(STEP_KEYS) - 1}")
        return value

    @field_validator("steps", mode="before")
    @classmethod
    def _check_steps(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        # mode="before": inspect the raw payload — otherwise Pydantic
        # would silently coerce "yes" / 1 into True before we see it.
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("steps must be an object of step name -> boolean")
        unknown = sorted(set(value) - set(STEP_KEYS))
        if unknown:
            raise ValueError(f"unknown setup steps: {unknown}")
        bad = sorted(k for k, v in value.items() if not isinstance(v, bool))
        if bad:
            raise ValueError(f"setup steps must be booleans: {bad}")
        return value

    @field_validator("source_type")
    @classmethod
    def _check_source_type(cls, value: str | None) -> str | None:
        if value is not None and value not in SOURCE_TYPES:
            raise ValueError("source_type must be 'video' or 'channel'")
        return value


@router.get("/state")
def get_setup_state() -> dict[str, Any]:
    """Current wizard state (defaults when never started)."""
    return _load()


@router.post("/state")
def update_setup_state(payload: SetupStateUpdate) -> dict[str, Any]:
    """Record wizard progress. Partial: omitted fields are kept.

    A step can only be marked done through this endpoint — the UI also
    verifies the underlying fact (FFmpeg found, channel connected)
    before calling it, so a stale ``done`` never hides a real problem.
    """
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    current = _load()
    if "steps" in patch:
        current["steps"].update(patch.pop("steps"))
    current.update(patch)
    # Marking the last step done completes the wizard; un-completing is
    # explicit (completed=false) so a stray write cannot reopen it.
    if current["steps"].get("agent") is True and current["current_step"] >= len(STEP_KEYS) - 1:
        current["completed"] = True
    try:
        _save(current)
    except OSError as exc:
        raise HTTPException(500, f"could not save setup state: {exc}") from exc
    return current
