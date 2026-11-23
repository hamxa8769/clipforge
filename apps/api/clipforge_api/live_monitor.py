"""Live stream monitoring (v0.10.0, Worker A) — ``/api/live/*``.

Router: ``GET/POST /api/live/*``. Registered in
``clipforge_api/app.py`` in one clearly-marked block — this module
never imports ``app.py`` (circular import: app.py registers this
router).

Endpoints:

  GET  /api/live/platforms          (what each platform can do, and
                                     what it needs — no key ever
                                     implied)
  GET  /api/live/status             (pre-flight: ffmpeg, yt-dlp,
                                     rolling transcription, YouTube
                                     credentials + chat availability)
  POST /api/live/monitor            (start monitoring a live stream
                                     URL, or replay a local file;
                                     202 with the session id)
  GET  /api/live/monitor            (all monitor sessions)
  GET  /api/live/monitor/{id}       (one session: elapsed, audio
                                     levels, chat, transcript tail,
                                     detected peaks + their job ids)
  POST /api/live/monitor/{id}/stop  (stop a monitor)
  POST /api/live/monitor/{id}/clip-now (clip the last N seconds now)

A monitor watches the live edge for peak moments (audio-energy and
chat-velocity jumps, plus "clip it" / custom trigger phrases in the
rolling transcript) and feeds each peak into the EXISTING pipeline
as a normal job — the peak is cut from the rolling buffer and the
job runs transcribe -> score -> clip -> SEO -> approval queue. The
router never invents a peak or a stream: with no live stream the
start call fails 422 in plain language, and with no YouTube
connection the status endpoint says chat is unavailable instead of
pretending otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from clipforge.live_monitor import (
    LIVE_MONITOR_NOTICE,
    MAX_HOURS_CAP,
    MAX_HOURS_DEFAULT,
    LiveMonitorError,
    check_monitor_requirements,
    detect_platform,
    get_monitor,
    list_monitors,
    platform_capabilities,
    start_monitor,
    stop_monitor,
)
from clipforge.trending import resolve_credentials

router = APIRouter(prefix="/api/live", tags=["live-monitor"])


def _data_dir() -> Path:
    from clipforge.config import settings  # local import: cheap, no cycle

    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


# ------------------------------------------------------------------ schemas
class MonitorStartIn(BaseModel):
    """Start a live monitor (``url``) or a replay monitor (``replay_path``).

    ``replay_path`` is a local file on the server machine, replayed
    at real-time speed through the same peak detector — the honest
    way to verify monitoring when no stream is live. The UI never
    sends it for a live run.
    """

    url: str = ""
    replay_path: str = ""
    max_hours: float = Field(default=MAX_HOURS_DEFAULT, ge=0.1, le=MAX_HOURS_CAP)
    buffer_min: int = Field(default=30, ge=5, le=120)
    trigger_words: list[str] = Field(default_factory=list)


class ClipNowIn(BaseModel):
    seconds: float = Field(default=30.0, ge=5.0, le=120.0)


# ------------------------------------------------------------------ capabilities + pre-flight
@router.get("/platforms")
def live_platforms():
    """What live monitoring can do per platform, and what each needs.

    YouTube chat needs a connected channel (OAuth); Twitch chat
    needs nothing; Kick chat is audio-only (no public chat API
    without auth). Nothing here claims a capability the user has
    not set up.
    """
    return {
        "platforms": [platform_capabilities(p) for p in ("youtube", "twitch", "kick")],
        "notice": LIVE_MONITOR_NOTICE,
    }


@router.get("/status")
def live_status():
    """Pre-flight for live monitoring, in plain language.

    ffmpeg + yt-dlp are required (blocking); rolling transcription
    is degraded without faster-whisper / an OpenAI key (peaks still
    fire from audio + chat); YouTube chat needs OAuth — an API key
    alone cannot read live chat, so with a key only the status says
    so instead of pretending chat works.
    """
    checked = check_monitor_requirements(_data_dir())
    youtube: dict = {"connected": False, "kind": "", "account": "", "chat": False, "chat_note": ""}
    try:
        from clipforge.auth.secrets import SecretStore  # noqa: PLC0415
        from clipforge.auth.settings_store import SettingsStore  # noqa: PLC0415

        secrets = SecretStore(_data_dir())
        creds = resolve_credentials(secrets, SettingsStore(_data_dir(), secrets))
        youtube = {
            "connected": True,
            "kind": creds.kind,
            "account": creds.account_name,
            "chat": creds.kind == "oauth",
            "chat_note": (
                "YouTube live chat is available (connected channel)."
                if creds.kind == "oauth"
                else (
                    "An API key cannot read YouTube live chat — connect the "
                    "channel (OAuth) for chat, or monitor audio-only."
                )
            ),
        }
    except Exception:  # noqa: BLE001 — no YouTube connection is a state, not an error
        youtube = {
            "connected": False,
            "kind": "",
            "account": "",
            "chat": False,
            "chat_note": (
                "No YouTube connection — YouTube monitoring still works "
                "for audio (yt-dlp needs no key), but live chat needs the "
                "channel connected. Twitch chat needs no connection at all."
            ),
        }
    return {
        "requirements": checked["requirements"],
        "all_ready": checked["all_ready"],
        "blocking": checked["blocking"],
        "youtube": youtube,
        "notice": LIVE_MONITOR_NOTICE,
    }


# ------------------------------------------------------------------ sessions
@router.post("/monitor", status_code=202)
def start_live_monitor(payload: MonitorStartIn):
    """Start monitoring a live stream (or a replay file).

    Validates the platform (YouTube / Twitch / Kick) and the
    requirements before starting — a non-live URL, an unsupported
    platform, or a missing ffmpeg/yt-dlp fails 422 in plain
    language, never a session that silently records nothing. The
    session id polls at GET /api/live/monitor/{id}; each detected
    peak appears there with its pipeline job id.
    """
    if payload.replay_path:
        path = Path(payload.replay_path)
        if not path.is_file():
            raise HTTPException(422, f"Replay file not found: {payload.replay_path}")
        if path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
            raise HTTPException(422, "Replay file must be a video (.mp4 / .mov / .mkv / .webm).")
        try:
            session = start_monitor(
                replay_path=path,
                data_dir=_data_dir(),
                max_hours=payload.max_hours,
                buffer_min=payload.buffer_min,
                trigger_words=payload.trigger_words,
            )
        except LiveMonitorError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "ok": True,
            "session_id": session.info.session_id,
            "mode": "replay",
            "platform": "replay",
            "url": str(path),
            "message": (
                "Replaying the file at real-time speed through the peak "
                "detector — this is a replay, not a live stream. Poll "
                f"GET /api/live/monitor/{session.info.session_id} for peaks."
            ),
            "notice": LIVE_MONITOR_NOTICE,
        }
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(
            422, "Give a live stream URL (YouTube, Twitch or Kick) or a replay file."
        )
    platform = detect_platform(url)
    if platform is None:
        raise HTTPException(
            422,
            "That URL is not a YouTube, Twitch or Kick live stream. "
            "Live monitoring works on those three platforms.",
        )
    # YouTube chat needs OAuth: pass the credential when one exists
    # so the session can start the chat reader; without it the
    # session runs audio-only and says so in its status.
    chat_credentials = None
    if platform == "youtube":
        try:
            from clipforge.auth.secrets import SecretStore  # noqa: PLC0415
            from clipforge.auth.settings_store import SettingsStore  # noqa: PLC0415

            secrets = SecretStore(_data_dir())
            creds = resolve_credentials(secrets, SettingsStore(_data_dir(), secrets))
            if creds.kind == "oauth":
                chat_credentials = creds
        except Exception:  # noqa: BLE001 — audio-only monitoring needs no credentials
            chat_credentials = None
    try:
        session = start_monitor(
            url=url,
            data_dir=_data_dir(),
            max_hours=payload.max_hours,
            buffer_min=payload.buffer_min,
            trigger_words=payload.trigger_words,
            chat_credentials=chat_credentials,
        )
    except LiveMonitorError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "ok": True,
        "session_id": session.info.session_id,
        "mode": "live",
        "platform": platform,
        "url": url,
        "chat": (
            "oauth"
            if chat_credentials is not None
            else ("anonymous-irc" if platform == "twitch" else "unavailable")
        ),
        "message": (
            f"Monitoring the {platform} live stream — peaks are cut from "
            "the live edge and sent to the pipeline. Poll "
            f"GET /api/live/monitor/{session.info.session_id} for peaks."
        ),
        "notice": LIVE_MONITOR_NOTICE,
    }


@router.get("/monitor")
def list_live_monitors():
    """All monitor sessions (live + replay), newest first."""
    return {"sessions": list_monitors(), "notice": LIVE_MONITOR_NOTICE}


@router.get("/monitor/{session_id}")
def get_live_monitor(session_id: str):
    """One monitor session: elapsed, levels, chat, transcript tail, peaks + job ids."""
    session = get_monitor(session_id)
    if session is None:
        raise HTTPException(404, f"monitor session {session_id} not found")
    return session.status()


@router.post("/monitor/{session_id}/stop")
def stop_live_monitor(session_id: str):
    """Stop a monitor. Peaks already cut keep their pipeline jobs."""
    session = stop_monitor(session_id)
    if session is None:
        raise HTTPException(404, f"monitor session {session_id} not found")
    return {
        "ok": True,
        "session_id": session_id,
        "status": session.info.status,
        "peaks": len(session.info.peaks),
        "message": "Monitor stopped — peaks already cut keep their pipeline jobs.",
    }


@router.post("/monitor/{session_id}/clip-now")
def clip_now(session_id: str, payload: ClipNowIn):
    """Clip the last N seconds of the monitored stream right now.

    The manual peak goes through the same cut + job path as an
    automatic peak. 409 when nothing has been recorded yet.
    """
    session = get_monitor(session_id)
    if session is None:
        raise HTTPException(404, f"monitor session {session_id} not found")
    try:
        peak = session.clip_now(payload.seconds)
    except LiveMonitorError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "ok": True,
        "session_id": session_id,
        "start_s": round(peak.start_s, 2),
        "end_s": round(peak.end_s, 2),
        "message": (
            f"Clipping the last {payload.seconds:g} seconds — the job will "
            "appear in the session's peaks."
        ),
    }


__all__ = ["router"]
