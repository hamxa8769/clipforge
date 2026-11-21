"""X (Twitter) platform support for ClipForge v0.8.0 (Worker C).

Official X API v2 only — no scraping, no unofficial uploaders.

What this module does
---------------------
- OAuth 2.0 authorization-code flow with PKCE (required by X): build the
  authorize URL (:func:`build_authorize_url`) and exchange a code for
  tokens (:func:`exchange_code`). The authorize URL is what
  ``POST /x/connect`` returns; the callback exchange lives in
  ``clipforge_api/app.py`` (coordinator-owned) and can use
  :func:`record_from_token_response` + :func:`load_pending_verifier`
  from here — ``clipforge.auth.oauth.exchange_code`` cannot be reused
  for X because it resolves the provider spec via
  ``clipforge.auth.providers.get_provider``, which has no X entry.
- Validate a stored token (:func:`fetch_account`, ``GET /2/users/me``).
- Publish a clip (:func:`publish_tweet`): chunked v1.1 media upload
  (INIT / APPEND / FINALIZE) then ``POST /2/tweets`` with the media id.

Honesty rules (enforced here, not just in the UI)
--------------------------------------------------
- Posting to X needs an X API **Basic plan (paid) or higher** — the
  free tier is read-only and cannot post. Every not-connected /
  not-configured message says so; :func:`x_status` never reports
  ``connected=True`` unless a real token is stored, unexpired, and
  accepted by ``GET /2/users/me``.
- Video rules enforced before any upload: video <= 140s, 9:16 vertical
  is supported, tweet text <= 280 characters. Violations are reported
  as plain-language errors, never attempted against the API.
- Tokens live in the encrypted :class:`clipforge.auth.token_store.TokenStore`
  (``oauth_token:x``) via the same :class:`SecretStore` the other
  platforms use. Client id/secret live in the same store under
  ``app:x:client_id`` / ``app:x:client_secret`` (see
  ``clipforge.auth.settings_store.SECRET_NAMES``) with ``X_CLIENT_ID`` /
  ``X_CLIENT_SECRET`` env fallback. No token or secret is ever logged
  or returned by any function here.

Rights guardrail: X publishing only ever runs on clips made from the
user's own videos or Creative Commons / licensed / public-domain
videos. The router enforces it (job ``own_content``); this module
never downloads or re-uploads another creator's video.

Live X calls are NOT verified in this environment (no X API key, no
developer app). :func:`publish_tweet` and :func:`exchange_code` are
tested with a mocked transport only — see tests/unit/test_x_platform.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenRecord

# ------------------------------------------------------------------ constants
X_AUTH_URL = "https://twitter.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_API_BASE = "https://api.x.com/2"
X_MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
X_USER_ME_URL = f"{X_API_BASE}/users/me"
X_TWEET_URL = f"{X_API_BASE}/tweets"

#: X API v2 scopes ClipForge requests. ``tweet.write`` is what posts;
#: ``offline.access`` is what yields a refresh token.
X_SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]

#: X video limit for a tweet: 140 seconds. 9:16 vertical is supported.
X_MAX_VIDEO_S = 140.0
#: Tweet text limit.
X_MAX_TEXT_CHARS = 280
#: Chunk size for the v1.1 chunked media upload.
_MEDIA_CHUNK_SIZE = 4 * 1024 * 1024

#: Secret names in the SecretStore (same pattern as the other platforms).
X_CLIENT_ID_SECRET = "app:x:client_id"
X_CLIENT_SECRET_SECRET = "app:x:client_secret"

#: The paid-tier fact every X message must carry where applicable.
X_PAID_TIER_NOTE = (
    "Posting to X needs an X API Basic plan (paid) or higher — the free "
    "tier is read-only and cannot post. Create / upgrade the app at "
    "developer.x.com, then save its Client ID and Client Secret in Settings."
)

X_NOT_CONNECTED_NOTE = "X is not connected. Connect it from the Channels page. " + X_PAID_TIER_NOTE


class XError(RuntimeError):
    """An X step failed. ``next_step`` is plain language for the user."""

    def __init__(self, message: str, next_step: str = ""):
        super().__init__(message)
        self.message = message
        self.next_step = next_step

    def detail(self) -> str:
        """Single plain-language string for an API error response."""
        if self.next_step:
            return f"{self.message} Next step: {self.next_step}"
        return self.message


# ------------------------------------------------------------------ credentials
def get_x_client_id(secrets: SecretStore) -> str | None:
    """Stored X OAuth client id, or the ``X_CLIENT_ID`` env fallback."""
    return secrets.get(X_CLIENT_ID_SECRET) or os.environ.get("X_CLIENT_ID") or None


def get_x_client_secret(secrets: SecretStore) -> str | None:
    """Stored X OAuth client secret, or the ``X_CLIENT_SECRET`` env fallback."""
    return secrets.get(X_CLIENT_SECRET_SECRET) or os.environ.get("X_CLIENT_SECRET") or None


def x_configured(secrets: SecretStore) -> bool:
    """True when the X app credentials (client id + secret) are saved."""
    return bool(get_x_client_id(secrets) and get_x_client_secret(secrets))


# ------------------------------------------------------------------ PKCE + authorize URL
def new_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for X OAuth 2.0 (PKCE S256).

    X requires PKCE for OAuth 2.0. The verifier is stored server-side
    (see :func:`save_pending_verifier`) until the callback exchanges it.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """The X authorize URL the user opens in their browser to grant access."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(X_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{X_AUTH_URL}?{urlencode(params)}"


# ------------------------------------------------- pending OAuth state (PKCE verifiers)
_PENDING_FILENAME = "x_oauth_pending.json"


def _pending_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _PENDING_FILENAME


def save_pending_verifier(data_dir: str | Path, state: str, verifier: str) -> None:
    """Remember a PKCE verifier for an in-flight authorize URL.

    The verifier is a short-lived, single-use secret: it is stored in
    the app data dir (same pattern as ``setup.json``), never returned
    by any endpoint, and deleted when consumed.
    """
    path = _pending_path(data_dir)
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = {}
    if not isinstance(pending, dict):
        pending = {}
    pending[state] = {"verifier": verifier, "created_at": datetime.now(UTC).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def load_pending_verifier(data_dir: str | Path, state: str) -> str | None:
    """Consume (read + delete) the PKCE verifier for ``state``.

    Returns ``None`` when the state is unknown or already used — the
    callback must then ask the user to press Connect again.
    """
    path = _pending_path(data_dir)
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pending, dict):
        return None
    entry = pending.pop(state, None)
    path.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    if not isinstance(entry, dict):
        return None
    verifier = entry.get("verifier")
    return verifier if isinstance(verifier, str) and verifier else None


# ------------------------------------------------------------------ token exchange
def _new_client(timeout: float) -> httpx.Client:
    """httpx client that ignores proxy env vars (same reason as
    ``clipforge.auth.oauth._new_client``: a malformed proxy config must
    not break platform calls)."""
    return httpx.Client(timeout=timeout, trust_env=False)


def _provider_error_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error_description") or payload.get("error") or payload.get("detail")
        if error:
            return str(error)
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("detail") or first.get("message") or first)
    return f"X returned HTTP {resp.status_code} with no error detail."


def exchange_code(
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    client: httpx.Client | None = None,
) -> dict:
    """Exchange an authorization code for X tokens. Returns the raw token dict.

    Raises :class:`XError` (plain language + next step) on any failure.
    A 403 here usually means the app's API plan does not include the
    requested access — see :data:`X_PAID_TIER_NOTE`.
    """
    own_client = client is None
    client = client or _new_client(30.0)
    try:
        resp = client.post(
            X_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            auth=(client_id, client_secret),
        )
    except httpx.HTTPError as exc:
        raise XError(
            f"Could not reach X: {exc}",
            "Check your internet connection and try Connect again.",
        ) from exc
    finally:
        if own_client:
            client.close()
    if resp.status_code >= 400:
        detail = _provider_error_message(resp)
        if resp.status_code == 403:
            raise XError(
                f"X refused the connection (HTTP 403): {detail}",
                "The X app's API plan may not include posting access. " + X_PAID_TIER_NOTE,
            )
        raise XError(
            f"X refused the connection (HTTP {resp.status_code}): {detail}",
            "Check the Client ID / Secret saved in Settings, then press Connect again.",
        )
    data = resp.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise XError(
            "X answered without an access token.",
            "Press Connect again; if it keeps failing, check the X app in developer.x.com.",
        )
    return data


def record_from_token_response(token: dict) -> TokenRecord:
    """Build a :class:`TokenRecord` from an X token response.

    (``TokenRecord.from_token_response`` cannot be used: it resolves the
    provider spec, and X has no entry in ``clipforge.auth.providers``.)
    """
    try:
        ttl = int(token.get("expires_in") or 7200)
    except (TypeError, ValueError):
        ttl = 7200
    return TokenRecord(
        platform="x",
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        scopes=list(X_SCOPES),
        obtained_at=datetime.now(UTC),
    )


# ------------------------------------------------------------------ validation
def validate_text(text: str) -> list[str]:
    """Tweet-text violations (empty = valid)."""
    violations: list[str] = []
    if not (text or "").strip():
        violations.append("Tweet text is empty — write the text for the post first.")
    elif len(text) > X_MAX_TEXT_CHARS:
        violations.append(
            f"Tweet text is {len(text)} characters — X allows at most "
            f"{X_MAX_TEXT_CHARS}. Shorten it before publishing."
        )
    return violations


def validate_video(*, duration_s: float | None, video_path: str | None) -> list[str]:
    """Video violations for an X post (empty = valid).

    X video must be <= 140 seconds; 9:16 vertical is supported. The
    file must exist — the media upload reads it.
    """
    violations: list[str] = []
    if not video_path:
        violations.append("This clip has no video file to upload to X.")
        return violations
    if not Path(video_path).is_file():
        violations.append(f"Clip video file not found: {video_path}")
    if duration_s is not None and duration_s > X_MAX_VIDEO_S:
        violations.append(
            f"Clip is {duration_s:.0f}s — X video posts allow at most "
            f"{int(X_MAX_VIDEO_S)}s. Trim it before publishing to X."
        )
    return violations


# ------------------------------------------------------------------ token check / publish
def fetch_account(access_token: str, *, client: httpx.Client | None = None) -> dict:
    """Validate a token via ``GET /2/users/me``.

    Returns ``{"account": "@username", "name": ...}``. Raises
    :class:`XError` when the token is rejected or unreachable — the
    caller reports ``connected=False`` then, never a guessed account.
    """
    own_client = client is None
    client = client or _new_client(15.0)
    try:
        resp = client.get(
            X_USER_ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"user.fields": "username,name"},
        )
    except httpx.HTTPError as exc:
        raise XError(
            f"Could not reach X: {exc}",
            "Check your internet connection and try again.",
        ) from exc
    finally:
        if own_client:
            client.close()
    if resp.status_code != 200:
        detail = _provider_error_message(resp)
        if resp.status_code in (401, 403):
            raise XError(
                f"X refused the stored token (HTTP {resp.status_code}): {detail}",
                "Reconnect X on the Channels page. "
                + (
                    "A 403 usually means the API plan does not include this access — "
                    + X_PAID_TIER_NOTE
                    if resp.status_code == 403
                    else ""
                ),
            )
        raise XError(
            f"X returned HTTP {resp.status_code}: {detail}",
            "Try again in a moment; if it keeps failing, reconnect X.",
        )
    data = resp.json().get("data") or {}
    username = str(data.get("username") or "")
    return {
        "account": f"@{username}" if username else None,
        "name": str(data.get("name") or ""),
        "user_id": str(data.get("id") or ""),
    }


def _raise_for_x_error(resp: httpx.Response, *, what: str) -> None:
    """Turn a failed X API response into an XError with a next step."""
    detail = _provider_error_message(resp)
    if resp.status_code == 403:
        raise XError(
            f"X refused the post (HTTP 403): {detail}",
            "This usually means the X API plan does not include posting — "
            "the free tier is read-only. " + X_PAID_TIER_NOTE,
        )
    if resp.status_code == 401:
        raise XError(
            f"X refused the post (HTTP 401): {detail}",
            "The stored X token was rejected — reconnect X on the Channels page.",
        )
    if resp.status_code == 429:
        raise XError(
            f"X rate-limited the post (HTTP 429): {detail}",
            "Wait a while and try again — X limits how often an app can post.",
        )
    raise XError(
        f"X refused the {what} (HTTP {resp.status_code}): {detail}",
        "Try again in a moment; if it keeps failing, reconnect X.",
    )


def _upload_media(
    access_token: str,
    video_path: str,
    *,
    client: httpx.Client,
) -> str:
    """Chunked v1.1 media upload. Returns the media id string."""
    headers = {"Authorization": f"Bearer {access_token}"}
    data = Path(video_path).read_bytes()
    total = len(data)
    resp = client.post(
        X_MEDIA_UPLOAD_URL,
        headers=headers,
        data={
            "command": "INIT",
            "total_bytes": str(total),
            "media_type": "video/mp4",
            "media_category": "tweet_video",
        },
    )
    if resp.status_code >= 400:
        _raise_for_x_error(resp, what="media upload (INIT)")
    try:
        media_id = str(resp.json()["media_id_string"])
    except (ValueError, KeyError) as exc:
        raise XError(
            "X answered the media upload without a media id.",
            "Try again in a moment; if it keeps failing, reconnect X.",
        ) from exc
    for index, offset in enumerate(range(0, total, _MEDIA_CHUNK_SIZE)):
        chunk = data[offset : offset + _MEDIA_CHUNK_SIZE]
        resp = client.post(
            X_MEDIA_UPLOAD_URL,
            headers=headers,
            data={"command": "APPEND", "media_id": media_id, "segment_index": str(index)},
            files={"media": ("chunk.mp4", chunk, "video/mp4")},
        )
        if resp.status_code >= 400:
            _raise_for_x_error(resp, what="media upload (APPEND)")
    resp = client.post(
        X_MEDIA_UPLOAD_URL,
        headers=headers,
        data={"command": "FINALIZE", "media_id": media_id},
    )
    if resp.status_code >= 400:
        _raise_for_x_error(resp, what="media upload (FINALIZE)")
    try:
        info = resp.json().get("processing_info") or {}
    except ValueError:
        info = {}
    # Video processing is async: poll until succeeded (bounded — the
    # tweet call itself would fail on an unprocessed id).
    for _ in range(30):
        state = info.get("state")
        if state in (None, "succeeded"):
            return media_id
        if state == "failed":
            raise XError(
                "X could not process the uploaded video.",
                "Check the clip is a supported MP4 (H.264) under the X size limits and try again.",
            )
        wait_s = int(info.get("check_after_secs") or 5)
        import time as _time  # noqa: PLC0415 (local: keeps the import cheap)

        _time.sleep(min(wait_s, 10))
        resp = client.get(
            X_MEDIA_UPLOAD_URL,
            headers=headers,
            params={"command": "STATUS", "media_id": media_id},
        )
        if resp.status_code >= 400:
            _raise_for_x_error(resp, what="media upload (STATUS)")
        try:
            info = resp.json().get("processing_info") or {}
        except ValueError:
            info = {}
    raise XError(
        "X took too long to process the uploaded video.",
        "Try publishing again in a few minutes.",
    )


def publish_tweet(
    access_token: str,
    *,
    text: str,
    video_path: str | None = None,
    duration_s: float | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Publish a tweet (text + optional clip video) via X API v2.

    Validates text/video rules first — violations raise :class:`XError`
    before any network call. Returns ``{"provider_post_id", "text",
    "media_id", "status": "published"}``. Raises :class:`XError` with
    a plain-language message + next step when X rejects the post
    (e.g. tier limits → 403).
    """
    violations = validate_text(text) + validate_video(duration_s=duration_s, video_path=video_path)
    if violations:
        raise XError(
            " ".join(violations),
            "Fix the issues above, then publish again.",
        )
    own_client = client is None
    client = client or _new_client(60.0)
    try:
        media_id: str | None = None
        if video_path:
            media_id = _upload_media(access_token, video_path, client=client)
        payload: dict = {"text": text}
        if media_id:
            payload["media"] = {"media_ids": [media_id]}
        resp = client.post(
            X_TWEET_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise XError(
            f"Could not reach X: {exc}",
            "Check your internet connection and try again.",
        ) from exc
    finally:
        if own_client:
            client.close()
    if resp.status_code >= 400:
        _raise_for_x_error(resp, what="tweet")
    try:
        tweet_id = str(resp.json()["data"]["id"])
    except (ValueError, KeyError) as exc:
        raise XError(
            "X answered without a tweet id.",
            "Check X to see if the post went out; if not, try again.",
        ) from exc
    return {
        "status": "published",
        "provider_post_id": tweet_id,
        "text": text,
        "media_id": media_id,
    }


__all__ = [
    "XError",
    "X_API_BASE",
    "X_AUTH_URL",
    "X_MAX_TEXT_CHARS",
    "X_MAX_VIDEO_S",
    "X_MEDIA_UPLOAD_URL",
    "X_NOT_CONNECTED_NOTE",
    "X_PAID_TIER_NOTE",
    "X_SCOPES",
    "X_TOKEN_URL",
    "X_TWEET_URL",
    "X_USER_ME_URL",
    "build_authorize_url",
    "exchange_code",
    "fetch_account",
    "get_x_client_id",
    "get_x_client_secret",
    "load_pending_verifier",
    "new_pkce_pair",
    "publish_tweet",
    "record_from_token_response",
    "save_pending_verifier",
    "validate_text",
    "validate_video",
    "x_configured",
]
