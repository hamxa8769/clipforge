"""X (Twitter) platform router (v0.8.0, Worker C).

Router: ``/x/*``. Registered in ``clipforge_api/app.py`` in one
clearly-marked block (the coordinator owns that registration) — this
module never imports ``app.py`` (circular import: app.py registers
this router).

Endpoints:

  GET  /x/status   (honest connection status — see below)
  POST /x/connect  (OAuth 2.0 + PKCE authorize URL)
  POST /x/publish  (publish an APPROVED clip to X; dry-run by default)

Honesty: ``GET /x/status`` reports ``connected: true`` only when a
real token is stored, unexpired, and accepted by X
(``GET /2/users/me``). Its ``note`` always states that posting needs
a paid X API tier (Basic or higher) — the free tier is read-only.

Rights guardrail: publishing runs only on clips from the user's own
videos or Creative Commons / licensed / public-domain videos (the
clip's job must carry the own-content declaration). Any other source
is refused with a 422, in plain language.

Dry-run: like every other publish path in ClipForge, live posting
follows the effective dry-run setting (Settings override, else
``CLIPFORGE_DRY_RUN``) unless the request body passes ``dry_run``
explicitly. A dry run validates everything and returns what WOULD be
sent — nothing reaches X.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from clipforge.auth.oauth import _new_client
from clipforge.auth.providers import redirect_uri
from clipforge.auth.secrets import SecretStore
from clipforge.auth.settings_store import SettingsStore
from clipforge.auth.token_store import TokenStore
from clipforge.config import settings
from clipforge.db import ClipCandidate, Job, init_db, session_factory
from clipforge.x_platform import (
    X_NOT_CONNECTED_NOTE,
    X_PAID_TIER_NOTE,
    XError,
    build_authorize_url,
    fetch_account,
    get_x_client_id,
    new_pkce_pair,
    publish_tweet,
    save_pending_verifier,
    validate_text,
    validate_video,
)

router = APIRouter(prefix="/x", tags=["x"])

_engine = init_db()
_Session = session_factory()


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. This module must not import app.py (circular import).
    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _secrets() -> SecretStore:
    return SecretStore(_data_dir())


def _settings_store() -> SettingsStore:
    return SettingsStore(_data_dir(), _secrets())


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _effective_dry_run() -> bool:
    """Settings-page override wins; otherwise CLIPFORGE_DRY_RUN decides."""
    return _settings_store().effective_dry_run(settings.dry_run)


# ------------------------------------------------------------------ schemas
class XPublishIn(BaseModel):
    clip_id: str
    text: str = ""
    # None = follow the effective dry-run setting; True/False overrides it.
    dry_run: bool | None = None


# ------------------------------------------------------------------ endpoints
@router.get("/status")
def x_status():
    """X connection status — honest, never guessed.

    ``connected`` is true only when a token is stored, unexpired, and
    X accepts it (``GET /2/users/me``). Anything else is
    ``connected: false`` with a ``note`` saying what is missing and
    that posting needs a paid X API tier.
    """
    secrets = _secrets()
    record = TokenStore(secrets).load("x")
    if record is None:
        return {"connected": False, "account": None, "note": X_NOT_CONNECTED_NOTE}
    if record.is_expired():
        return {
            "connected": False,
            "account": None,
            "note": (
                "The stored X token has expired — reconnect X on the Channels page. "
                + X_PAID_TIER_NOTE
            ),
        }
    try:
        with _new_client(15.0) as client:
            account = fetch_account(record.access_token, client=client)
    except XError as exc:
        return {"connected": False, "account": None, "note": exc.detail()}
    return {
        "connected": True,
        "account": account.get("account") or record.account_name or None,
        "note": (
            "Connected to X. Posting uses the X API v2 — a paid Basic plan "
            "or higher is required to post; the free tier is read-only."
        ),
    }


@router.post("/connect")
def x_connect():
    """Return the X OAuth 2.0 authorize URL (PKCE) for the Connect button.

    422 (plain language) when the X app credentials are not configured
    yet — save the Client ID and Client Secret in Settings first.
    The PKCE verifier is stored server-side for the callback exchange;
    the URL itself carries only the challenge, never the verifier.
    """
    store = _secrets()
    client_id = get_x_client_id(store)
    if not client_id:
        raise HTTPException(
            422,
            "X is not set up yet — create an app at developer.x.com, then save "
            "its Client ID and Client Secret in Settings. " + X_PAID_TIER_NOTE,
        )
    uri = redirect_uri("x")
    state = secrets.token_urlsafe(24)
    verifier, challenge = new_pkce_pair()
    save_pending_verifier(_data_dir(), state, verifier)
    return {
        "url": build_authorize_url(
            client_id=client_id,
            redirect_uri=uri,
            state=state,
            code_challenge=challenge,
        ),
        "redirect_uri": uri,
        "state": state,
    }


@router.post("/publish")
def x_publish(payload: XPublishIn, db: Session = Depends(get_db)):
    """Publish an approved clip to X (text + clip video).

    Approval-first: the clip must be ``approved`` (409 otherwise).
    Rights-first: the clip's job must carry the own-content
    declaration (422 otherwise — ClipForge only publishes clips made
    from your own or licensed videos). Text (<=280 chars) and video
    (<=140s, file present) are validated before anything is sent.
    Dry-run (the default while dry-run is on) validates and returns
    what WOULD be sent; live posting needs dry-run off in Settings
    AND a connected X account. X rejections (e.g. tier limits, 403)
    come back as plain-language errors with the next step.
    """
    clip = db.get(ClipCandidate, payload.clip_id)
    if clip is None:
        raise HTTPException(404, f"clip {payload.clip_id} not found")
    if clip.status != "approved":
        raise HTTPException(
            409,
            f"Approval-first: clip {clip.id} is '{clip.status}', not 'approved'. "
            "Approve it in review before publishing it to X.",
        )
    job = db.get(Job, clip.job_id) if clip.job_id else None
    if job is not None and not job.own_content:
        raise HTTPException(
            422,
            "ClipForge only publishes clips made from your own videos or "
            "videos you hold a licence for — this clip's source has no "
            "own-content declaration. Do not publish another creator's video to X.",
        )
    violations = validate_text(payload.text) + validate_video(
        duration_s=clip.duration_s, video_path=clip.file_path
    )
    if violations:
        raise HTTPException(422, " ".join(violations))
    dry_run = payload.dry_run if payload.dry_run is not None else _effective_dry_run()
    if dry_run:
        return {
            "status": "dry_run",
            "clip_id": clip.id,
            "text": payload.text,
            "provider_post_id": None,
            "message": (
                "Dry run — nothing was sent to X. Turn dry-run off in Settings "
                "and connect X to post this clip live."
            ),
        }
    record = TokenStore(_secrets()).load("x")
    if record is None:
        raise HTTPException(422, X_NOT_CONNECTED_NOTE)
    if record.is_expired():
        raise HTTPException(
            422,
            "The stored X token has expired — reconnect X on the Channels page. "
            + X_PAID_TIER_NOTE,
        )
    try:
        result = publish_tweet(
            record.access_token,
            text=payload.text,
            video_path=clip.file_path,
            duration_s=clip.duration_s,
        )
    except XError as exc:
        raise HTTPException(502, exc.detail()) from exc
    return {
        "status": "published",
        "clip_id": clip.id,
        "text": payload.text,
        "provider_post_id": result["provider_post_id"],
        "message": f"Posted to X as tweet {result['provider_post_id']}.",
    }


__all__ = ["router"]
