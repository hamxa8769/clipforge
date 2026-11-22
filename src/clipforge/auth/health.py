"""Channel connection-health monitor for ClipForge v0.9.0 (Worker B).

Why this exists: before v0.9.0 nothing ever refreshed a token on its
own (see ``docs/channels-reliability.md`` R1–R8). YouTube tokens live
1 hour, X tokens 2 hours, TikTok 24 hours — so every channel was
guaranteed to flip to Expired, and the only signal was the card itself.

What this module does:

* :func:`assess_health` — pure, clock-based health for one channel:
  ``connected`` / ``expiring_soon`` / ``disconnected`` /
  ``not_connected``, with a plain-language reason and next step. No
  provider call is made here (no quota burn, no false disconnects on
  network blips); live verification stays in Test Connection.
* :func:`refresh_with_retry` — refresh a due token, retrying transient
  (network) failures with backoff, never retrying a dead grant.
* :func:`check_platform` / :func:`check_all` — one monitor pass:
  refresh every due token, and on a definitive failure report the
  channel Disconnected with the reason — and emit a notification —
  instead of silently dropping it. A token is **never deleted** here.
* :class:`ChannelHealthMonitor` — the background thread that runs a
  pass every 15 minutes inside the sidecar, plus
  :func:`ensure_background_monitor` (idempotent start, called lazily
  from the channels endpoints so the thread exists whenever the app
  is actually running).

Health state between runs lives in ``<data_dir>/channel_health.json``
(:class:`ChannelHealthStore`): last check, last error, consecutive
failures, last successful refresh. Tokens themselves stay in the
encrypted :class:`~clipforge.auth.token_store.TokenStore`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from clipforge.auth.oauth import (
    DEFINITIVE_REFRESH_FAILURES,
    OAuthError,
    is_retryable_refresh_error,
    refresh_access_token,
)
from clipforge.auth.providers import PLATFORMS, get_provider
from clipforge.auth.token_store import TokenRecord, TokenStore
from clipforge.db import SocialAccount
from clipforge.notifications import notify as _notify

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from clipforge.auth.secrets import SecretStore
    from clipforge.auth.settings_store import SettingsStore

#: Refresh a token this far before it expires (15 minutes). YouTube
#: tokens live 1 hour, so the monitor gets several chances per token.
REFRESH_BEFORE_EXPIRY_S = 900
#: Background monitor cadence (15 minutes).
MONITOR_INTERVAL_S = 900
#: First monitor pass this long after the thread starts — startup must
#: never wait on provider calls.
MONITOR_STARTUP_DELAY_S = 60
#: Refresh attempts (first try + retries) and backoff between them.
REFRESH_ATTEMPTS = 3
REFRESH_BACKOFF_S = (1.0, 2.0, 4.0)

HEALTH_CONNECTED = "connected"
HEALTH_EXPIRING_SOON = "expiring_soon"
HEALTH_DISCONNECTED = "disconnected"
HEALTH_NOT_CONNECTED = "not_connected"

# Env-var accounts (pre-OAuth): a configured env token counts as
# connected, but the backend knows no account name / avatar / expiry
# for it and cannot refresh it. (Moved here from channels.py so the
# monitor and the status cards share one definition.)
_ENV_TOKEN_VARS = {
    "youtube": "YOUTUBE_REFRESH_TOKEN",
    "instagram": "META_ACCESS_TOKEN",
    "facebook": "META_ACCESS_TOKEN",
    "tiktok": "TIKTOK_ACCESS_TOKEN",
}

__all__ = [
    "DEFINITIVE_REFRESH_FAILURES",
    "HEALTH_CONNECTED",
    "HEALTH_DISCONNECTED",
    "HEALTH_EXPIRING_SOON",
    "HEALTH_NOT_CONNECTED",
    "MONITOR_INTERVAL_S",
    "MONITOR_STARTUP_DELAY_S",
    "REFRESH_ATTEMPTS",
    "REFRESH_BACKOFF_S",
    "REFRESH_BEFORE_EXPIRY_S",
    "ChannelHealthMonitor",
    "ChannelHealthStore",
    "assess_health",
    "check_all",
    "check_platform",
    "ensure_background_monitor",
    "notify_channel_disconnected",
    "refresh_due",
    "refresh_with_retry",
    "stop_all_background_monitors",
]


def _human_duration(seconds: float) -> str:
    """'90 seconds' / '45 minutes' / '2 hours' / '3 days' for a delta."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def _can_refresh(record: TokenRecord | None, platform: str) -> bool:
    """True when a refresh / extend is possible for this channel.

    YouTube / TikTok / X need a stored refresh token; Meta has none by
    design and re-exchanges the current access token instead.
    """
    if record is None:
        return False
    return bool(record.refresh_token) or platform in ("instagram", "facebook")


def assess_health(
    record: TokenRecord | None,
    platform: str,
    *,
    source: str = "oauth",
    now: datetime | None = None,
) -> dict:
    """Clock-based health for one channel (no network, no quota).

    Returns ``health`` (``connected`` / ``expiring_soon`` /
    ``disconnected`` / ``not_connected``), ``health_reason`` and
    ``health_next_step`` in plain language, plus ``expires_in_s``,
    ``needs_refresh`` and ``can_refresh`` for the UI and the monitor.
    ``source`` is ``"oauth"`` or ``"env"`` (legacy env-var account).
    """
    now = now or datetime.now(UTC)
    spec = get_provider(platform)
    name = spec.display_name
    if record is None:
        if source == "env":
            return {
                "health": HEALTH_CONNECTED,
                "health_reason": (
                    "Connected via environment variable — ClipForge can't "
                    "check when this token expires or refresh it "
                    "automatically. Connect with OAuth on the Channels "
                    "page to get automatic refresh."
                ),
                "health_next_step": "",
                "expires_in_s": None,
                "needs_refresh": False,
                "can_refresh": False,
            }
        return {
            "health": HEALTH_NOT_CONNECTED,
            "health_reason": f"{name} is not connected yet.",
            "health_next_step": f"Press Connect for {name} on the Channels page.",
            "expires_in_s": None,
            "needs_refresh": False,
            "can_refresh": False,
        }
    remaining = record.seconds_until_expiry(now)
    can_refresh = _can_refresh(record, platform)
    if record.is_expired():
        ago = _human_duration(-remaining) if remaining is not None else "some time"
        if can_refresh:
            return {
                "health": HEALTH_DISCONNECTED,
                "health_reason": f"Token expired {ago} ago.",
                "health_next_step": (
                    f"Press Refresh token for {name} — it usually reconnects "
                    f"without signing in again. If that fails, press Connect."
                ),
                "expires_in_s": remaining,
                "needs_refresh": True,
                "can_refresh": True,
            }
        return {
            "health": HEALTH_DISCONNECTED,
            "health_reason": (
                f"Token expired {ago} ago, and this channel can't refresh "
                f"itself — no refresh token was stored."
            ),
            "health_next_step": f"Press Connect for {name} on the Channels page to sign in again.",
            "expires_in_s": remaining,
            "needs_refresh": True,
            "can_refresh": False,
        }
    if remaining is not None and remaining <= REFRESH_BEFORE_EXPIRY_S:
        if can_refresh:
            return {
                "health": HEALTH_EXPIRING_SOON,
                "health_reason": (
                    f"Token expires in {_human_duration(remaining)} — "
                    f"ClipForge will refresh it automatically before then."
                ),
                "health_next_step": (
                    "No action needed. If it still shows expired later, "
                    f"press Refresh token for {name}."
                ),
                "expires_in_s": remaining,
                "needs_refresh": True,
                "can_refresh": True,
            }
        return {
            "health": HEALTH_EXPIRING_SOON,
            "health_reason": (
                f"Token expires in {_human_duration(remaining)}, and this "
                f"channel can't refresh itself — no refresh token was stored."
            ),
            "health_next_step": f"Press Connect for {name} on the Channels page before it expires.",
            "expires_in_s": remaining,
            "needs_refresh": True,
            "can_refresh": False,
        }
    if remaining is None:
        return {
            "health": HEALTH_CONNECTED,
            "health_reason": "Token has no expiry on record — last check found it valid.",
            "health_next_step": "",
            "expires_in_s": None,
            "needs_refresh": False,
            "can_refresh": can_refresh,
        }
    return {
        "health": HEALTH_CONNECTED,
        "health_reason": f"Token valid for {_human_duration(remaining)}.",
        "health_next_step": "",
        "expires_in_s": remaining,
        "needs_refresh": False,
        "can_refresh": can_refresh,
    }


def refresh_due(record: TokenRecord | None, *, now: datetime | None = None) -> bool:
    """True when a stored token should be refreshed now (see assess)."""
    if record is None:
        return False
    return record.needs_refresh(before_s=REFRESH_BEFORE_EXPIRY_S, now=now)


def refresh_with_retry(
    platform: str,
    record: TokenRecord,
    *,
    client_id: str,
    client_secret: str,
    client: httpx.Client | None = None,
    attempts: int = REFRESH_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    backoff_s: tuple[float, ...] = REFRESH_BACKOFF_S,
    refresh_fn: Callable[..., dict] | None = None,
) -> tuple[TokenRecord | None, OAuthError | None]:
    """Refresh one token, retrying transient failures with backoff.

    Returns ``(new_record, None)`` on success or ``(None, error)`` on
    failure. Only ``network_error`` is retried; a provider refusal
    (``invalid_grant`` et al.) means the stored grant is dead and
    retrying cannot help. The new record keeps the old account facts,
    and keeps the old refresh token when the provider omits a new one
    (providers rotate refresh tokens, but often omit them on refresh).
    """
    call = refresh_fn or refresh_access_token
    last_error: OAuthError | None = None
    for attempt in range(max(1, attempts)):
        try:
            token = call(
                platform,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=record.refresh_token,
                access_token=record.access_token,
                client=client,
            )
        except OAuthError as exc:
            last_error = exc
            if is_retryable_refresh_error(exc) and attempt + 1 < attempts:
                sleep(backoff_s[min(attempt, len(backoff_s) - 1)])
                continue
            return None, exc
        new_record = TokenRecord.from_token_response(platform, token)
        new_record.refresh_token = new_record.refresh_token or record.refresh_token
        new_record.account_name = record.account_name
        new_record.avatar_url = record.avatar_url
        new_record.account_ref = record.account_ref
        return new_record, None
    return None, last_error


# ------------------------------------------------------------------ persisted state
class ChannelHealthStore:
    """Per-platform monitor state in ``<data_dir>/channel_health.json``.

    Small JSON file (same pattern as ``setup.json``): last check, last
    error, consecutive failures, last successful refresh. Survives
    server restarts, so a disconnect reason is still there after one.
    """

    def __init__(self, data_dir: str | Path):
        self._path = Path(data_dir) / "channel_health.json"
        self._lock = threading.Lock()

    def load_all(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, platform: str) -> dict:
        entry = self.load_all().get(platform)
        return dict(entry) if isinstance(entry, dict) else {}

    def update(self, platform: str, **fields: object) -> dict:
        """Merge ``fields`` into the platform's entry and persist it."""
        with self._lock:
            data = self.load_all()
            entry = dict(data.get(platform) or {})
            entry.update(fields)
            data[platform] = entry
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
            return dict(entry)


# ------------------------------------------------------------------ notifications
# Channel disconnects go through the shared notifications store
# (clipforge.notifications.notify -> clipforge.auth.notifications,
# the single store the header bell reads). One alert per channel per
# day at most: emit_notification dedupes on
# ``channel-disconnected:<platform>`` (unread, last 24 h), so a
# flapping channel cannot spam the activity centre.
def notify_channel_disconnected(
    db: Session,
    *,
    platform: str,
    level: str,
    title: str,
    message: str,
) -> dict:
    """Notify about a channel problem (deduped: once per channel per day)."""
    return _notify(
        db,
        level,
        title,
        message,
        action={"label": "Open Channels", "url": "/channels"},
        platform=platform,
        dedupe_key=f"channel-disconnected:{platform}",
    )


# ------------------------------------------------------------------ monitor pass
def _mark_account(db: Session, platform: str, status: str) -> None:
    account = (
        db.query(SocialAccount)
        .filter_by(provider=platform)
        .order_by(SocialAccount.created_at.desc())
        .first()
    )
    if account is not None:
        account.status = status
        db.commit()


def _env_source(platform: str) -> bool:
    env_var = _ENV_TOKEN_VARS.get(platform, "")
    return bool(env_var and os.environ.get(env_var))


def check_platform(
    platform: str,
    *,
    secrets: SecretStore,
    settings_store: SettingsStore,
    db: Session,
    health_store: ChannelHealthStore | None = None,
    client: httpx.Client | None = None,
    refresh_fn: Callable[..., dict] | None = None,
    notify: bool = True,
    now: datetime | None = None,
) -> dict:
    """One monitor pass for one platform.

    Loads the stored token, refreshes it when due, and returns the
    health dict (``assess_health`` fields plus ``platform``,
    ``display_name``, ``refreshed``, ``last_error``,
    ``last_checked_at`` and ``consecutive_failures``). A failed refresh
    never deletes the token: the channel is reported Disconnected with
    the reason, the account row is marked disconnected, and — for a
    definitive failure, or a transient failure on an already-expired
    token — a notification is emitted (deduped, once per channel per
    day). A transient failure on a still-valid token only records
    ``last_error``; the next run retries.
    """
    now = now or datetime.now(UTC)
    spec = get_provider(platform)
    store = TokenStore(secrets)
    health_store = health_store or ChannelHealthStore(secrets.data_dir)
    previous = health_store.get(platform)
    failures = int(previous.get("consecutive_failures") or 0)
    record = store.load(platform)
    source = "oauth" if record is not None else ("env" if _env_source(platform) else "none")
    assessed = assess_health(record, platform, source=source, now=now)
    result: dict = {
        "platform": platform,
        "display_name": spec.display_name,
        **assessed,
        "refreshed": False,
        "last_error": previous.get("last_error"),
        "last_checked_at": now.isoformat(),
        "consecutive_failures": failures,
    }

    if record is None:
        health_store.update(
            platform,
            health=result["health"],
            last_checked_at=now.isoformat(),
            last_error=None,
            consecutive_failures=0,
        )
        result["last_error"] = None
        result["consecutive_failures"] = 0
        return result

    if not assessed["needs_refresh"]:
        # Healthy and nothing due: a past transient error is moot.
        health_store.update(
            platform,
            health=result["health"],
            last_checked_at=now.isoformat(),
            last_error=None,
            consecutive_failures=0,
        )
        result["last_error"] = None
        result["consecutive_failures"] = 0
        return result

    if not assessed["can_refresh"]:
        # Due but un-refreshable: expired means disconnected now.
        if record.is_expired():
            _mark_account(db, platform, "disconnected")
            result["health"] = HEALTH_DISCONNECTED
            result["consecutive_failures"] = failures + 1
            health_store.update(
                platform,
                health=HEALTH_DISCONNECTED,
                last_checked_at=now.isoformat(),
                last_error=result["health_reason"],
                consecutive_failures=failures + 1,
            )
            result["last_error"] = result["health_reason"]
            if notify:
                notify_channel_disconnected(
                    db,
                    platform=platform,
                    level="error",
                    title=f"{spec.display_name} disconnected",
                    message=f"{result['health_reason']} {result['health_next_step']}",
                )
        else:
            health_store.update(
                platform,
                health=HEALTH_EXPIRING_SOON,
                last_checked_at=now.isoformat(),
            )
        return result

    # Due and refreshable — try, with retry + backoff.
    new_record, error = refresh_with_retry(
        platform,
        record,
        client_id=settings_store.get_credential(platform, "client_id") or "",
        client_secret=settings_store.get_credential(platform, "client_secret") or "",
        client=client,
        refresh_fn=refresh_fn,
    )
    if new_record is not None:
        store.save(new_record)
        _mark_account(db, platform, "active")
        refreshed = assess_health(new_record, platform, source="oauth", now=now)
        result.update(
            refreshed=True,
            health=refreshed["health"],
            health_reason=(
                f"Token refreshed automatically just now — valid for "
                f"{_human_duration(refreshed['expires_in_s'] or 0)}."
            ),
            health_next_step="",
            expires_in_s=refreshed["expires_in_s"],
            needs_refresh=False,
            last_error=None,
            consecutive_failures=0,
        )
        health_store.update(
            platform,
            health=result["health"],
            last_checked_at=now.isoformat(),
            last_error=None,
            consecutive_failures=0,
            last_refresh_at=now.isoformat(),
        )
        return result

    # Refresh failed. Only a network error is transient — every other
    # failure means the stored grant / exchange is dead, so the next
    # step is a full reconnect, not another silent retry.
    assert error is not None
    failures += 1
    transient = error.code == "network_error"
    result["last_error"] = error.message
    result["consecutive_failures"] = failures
    if transient and not record.is_expired():
        result["health"] = HEALTH_EXPIRING_SOON
        result["health_reason"] = (
            f"Token expires soon and automatic refresh failed "
            f"({error.message}) — will retry automatically."
        )
        result["health_next_step"] = (
            f"No action needed yet. If it still shows expired later, "
            f"press Refresh token for {spec.display_name}."
        )
        health_store.update(
            platform,
            health=HEALTH_EXPIRING_SOON,
            last_checked_at=now.isoformat(),
            last_error=error.message,
            consecutive_failures=failures,
        )
        return result
    _mark_account(db, platform, "disconnected")
    result["health"] = HEALTH_DISCONNECTED
    if transient:
        result["health_reason"] = (
            f"Token expired and automatic refresh failed ({error.message}) "
            f"— will retry automatically."
        )
        result["health_next_step"] = (
            f"Press Refresh token for {spec.display_name} to try now, or "
            f"wait for the next automatic retry."
        )
    else:
        result["health_reason"] = f"Automatic refresh failed: {error.message}"
        result["health_next_step"] = (
            f"Press Connect for {spec.display_name} on the Channels page to sign in again."
        )
    health_store.update(
        platform,
        health=HEALTH_DISCONNECTED,
        last_checked_at=now.isoformat(),
        last_error=error.message,
        consecutive_failures=failures,
    )
    if notify:
        notify_channel_disconnected(
            db,
            platform=platform,
            level="warning" if transient else "error",
            title=f"{spec.display_name} disconnected",
            message=f"{result['health_reason']} {result['health_next_step']}",
        )
    return result


def check_all(
    *,
    secrets: SecretStore,
    settings_store: SettingsStore,
    db: Session,
    health_store: ChannelHealthStore | None = None,
    client: httpx.Client | None = None,
    refresh_fn: Callable[..., dict] | None = None,
    notify: bool = True,
    now: datetime | None = None,
) -> dict:
    """One monitor pass over every platform, with a summary."""
    now = now or datetime.now(UTC)
    health_store = health_store or ChannelHealthStore(secrets.data_dir)
    platforms = [
        check_platform(
            platform,
            secrets=secrets,
            settings_store=settings_store,
            db=db,
            health_store=health_store,
            client=client,
            refresh_fn=refresh_fn,
            notify=notify,
            now=now,
        )
        for platform in PLATFORMS
    ]
    summary = {
        h: 0
        for h in (
            HEALTH_CONNECTED,
            HEALTH_EXPIRING_SOON,
            HEALTH_DISCONNECTED,
            HEALTH_NOT_CONNECTED,
        )
    }
    for entry in platforms:
        summary[entry["health"]] = summary.get(entry["health"], 0) + 1
    return {"checked_at": now.isoformat(), "platforms": platforms, "summary": summary}


# ------------------------------------------------------------------ background thread
class ChannelHealthMonitor:
    """Runs :func:`check_all` on a schedule inside the sidecar.

    The desktop app has no cron, so the sidecar owns the cadence: a
    daemon thread (never blocks shutdown, never kills the server on an
    unexpected error — a failed pass is retried next tick). One pass
    only does network I/O for tokens that are actually due, so the
    idle cost is a few secret-store reads every 15 minutes.
    """

    def __init__(
        self,
        secrets_fn: Callable[[], SecretStore],
        settings_fn: Callable[[], SettingsStore],
        session_factory_fn: Callable[[], Session],
        *,
        interval_s: float = MONITOR_INTERVAL_S,
        startup_delay_s: float = MONITOR_STARTUP_DELAY_S,
    ):
        self._secrets_fn = secrets_fn
        self._settings_fn = settings_fn
        self._session_factory_fn = session_factory_fn
        self.interval_s = interval_s
        self.startup_delay_s = startup_delay_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def check_now(self) -> dict:
        """Run one pass now (fresh DB session; closed afterwards)."""
        db = self._session_factory_fn()
        try:
            return check_all(
                secrets=self._secrets_fn(),
                settings_store=self._settings_fn(),
                db=db,
            )
        finally:
            db.close()

    def _run(self) -> None:
        if self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception:
                pass  # a failed pass must never kill the monitor thread
            self._stop.wait(self.interval_s)

    def ensure_started(self) -> bool:
        """Start the daemon thread once; True when (now) running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._thread = threading.Thread(
                target=self._run, name="clipforge-channel-health", daemon=True
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Stop the thread (tests / shutdown)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)


_MONITORS: dict[str, ChannelHealthMonitor] = {}
_MONITORS_LOCK = threading.Lock()


def ensure_background_monitor(
    data_dir: str | Path,
    secrets_fn: Callable[[], SecretStore],
    settings_fn: Callable[[], SettingsStore],
    session_factory_fn: Callable[[], Session],
) -> ChannelHealthMonitor:
    """Get (creating + starting) the monitor for one data dir.

    Idempotent: the second call for the same dir returns the running
    monitor instead of starting a second thread.
    """
    key = str(data_dir)
    with _MONITORS_LOCK:
        monitor = _MONITORS.get(key)
        if monitor is None:
            monitor = ChannelHealthMonitor(secrets_fn, settings_fn, session_factory_fn)
            _MONITORS[key] = monitor
    monitor.ensure_started()
    return monitor


def stop_all_background_monitors() -> None:
    """Stop every background monitor (tests / shutdown)."""
    with _MONITORS_LOCK:
        monitors = list(_MONITORS.values())
        _MONITORS.clear()
    for monitor in monitors:
        monitor.stop()
