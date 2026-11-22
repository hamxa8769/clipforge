"""OAuth initiation, callback exchange, refresh and health checks.

Official OAuth only — no Selenium, no cookie login, no unofficial
uploaders (those get accounts flagged; see the research report §7 and
§9c). The flow is the standard authorization-code flow with a loopback
redirect (``http://127.0.0.1:<port>/auth/callback/<platform>``), the same
shape Postiz's provider contract uses (``generateAuthUrl`` /
``authenticate`` / ``refreshToken``) — design reference only, fresh code.

``httpx.Client`` is injectable everywhere so tests can pass a
``MockTransport`` instead of touching the network.
"""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode

import httpx

from clipforge.auth.providers import get_provider

_STATE_TTL_S = 600  # authorize URL is valid for 10 minutes


class OAuthError(RuntimeError):
    """An OAuth step failed. ``code`` is machine-readable for the UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ error mapping
# Postiz-style error mapping: provider error strings become the system
# types the UI shows ("reconnect", "not configured", ...). The raw
# provider message is kept in the detail for debugging, never the token.
_ERROR_MAP = {
    "invalid_grant": (
        "invalid_grant",
        "Authorization expired or already used — press Connect again.",
    ),
    "invalid_client": (
        "invalid_client",
        "Client id/secret rejected — check the app credentials in Settings.",
    ),
    "invalid_request": (
        "invalid_request",
        "The authorization request was rejected — check the redirect URI "
        "matches the registered one.",
    ),
    "redirect_uri_mismatch": (
        "redirect_uri_mismatch",
        "Redirect URI mismatch — copy the exact redirect URI from Settings into the platform app.",
    ),
    "access_denied": (
        "access_denied",
        "Access denied on the platform — the connection was not granted.",
    ),
}


def map_provider_error(payload: dict) -> OAuthError:
    """Turn a provider token/profile error payload into an OAuthError."""
    error = str(
        payload.get("error") or payload.get("error_code") or payload.get("code") or "provider_error"
    )
    detail = str(payload.get("error_description") or payload.get("message") or "")
    code, message = _ERROR_MAP.get(
        error, ("provider_error", f"Provider refused the request ({error}).")
    )
    if detail and code == "provider_error":
        message = f"Provider refused the request ({error}): {detail}"
    return OAuthError(code, message)


def _raise_for_provider_error(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            # Google/Meta style: {"error": {"code": ..., "message": ...}}
            raise map_provider_error(err)
        if err:
            raise map_provider_error(payload)
    raise OAuthError(
        "provider_error",
        f"Provider returned HTTP {resp.status_code} with no error detail.",
    )


# ------------------------------------------------------------------ state (CSRF)
class StateStore:
    """Single-use, expiring OAuth state tokens (in-memory, process-local)."""

    def __init__(self) -> None:
        self._states: dict[str, tuple[str, float]] = {}

    def new(self, platform: str) -> str:
        state = secrets.token_urlsafe(24)
        self._states[state] = (platform, time.time() + _STATE_TTL_S)
        return state

    def consume(self, state: str, platform: str) -> None:
        """Validate and burn a state token; raises OAuthError when invalid."""
        entry = self._states.pop(state, None)
        if entry is None:
            raise OAuthError(
                "state_mismatch",
                "State token unknown or already used — start Connect again.",
            )
        stored_platform, expires_at = entry
        if stored_platform != platform:
            raise OAuthError(
                "state_mismatch",
                "State token was issued for a different platform — start Connect again.",
            )
        if time.time() > expires_at:
            raise OAuthError(
                "state_mismatch",
                "Authorization link expired (10 minutes) — start Connect again.",
            )


# ------------------------------------------------------------------ authorize URL
def build_authorize_url(platform: str, *, client_id: str, redirect_uri: str, state: str) -> str:
    """The URL the user opens in their browser to grant access."""
    spec = get_provider(platform)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(spec.scopes),
        "state": state,
        **spec.auth_params,
    }
    if platform == "tiktok":
        params["scope"] = ",".join(spec.scopes)  # TikTok wants comma scopes
    return f"{spec.auth_url}?{urlencode(params)}"


# ------------------------------------------------------------------ token exchange
def _new_client(timeout: float) -> httpx.Client:
    """httpx client that ignores proxy env vars.

    The VM / user machine may carry a malformed proxy config (e.g. a bad
    NO_PROXY entry) that makes plain ``httpx.Client()`` raise InvalidURL
    at construction time. A desktop app calling platform APIs directly
    must not fail — or 500 a Test Connection — because of that.
    """
    return httpx.Client(timeout=timeout, trust_env=False)


def _token_request(
    platform: str,
    form: dict[str, str],
    *,
    client: httpx.Client | None = None,
    auth: tuple[str, str] | None = None,
) -> dict:
    spec = get_provider(platform)
    own_client = client is None
    client = client or _new_client(30.0)
    try:
        resp = client.post(spec.token_url, data=form, auth=auth)
    except httpx.HTTPError as exc:
        raise OAuthError("network_error", f"Could not reach {spec.display_name}: {exc}") from exc
    finally:
        if own_client:
            client.close()
    _raise_for_provider_error(resp)
    data = resp.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(
            "provider_error",
            f"{spec.display_name} answered without an access token.",
        )
    return data


def exchange_code(
    platform: str,
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    client: httpx.Client | None = None,
) -> dict:
    """Exchange an authorization code for tokens. Returns the raw token dict."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        **get_provider(platform).token_params,
    }
    return _token_request(platform, form, client=client)


def refresh_access_token(
    platform: str,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str | None = None,
    access_token: str | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Refresh an expired access token.

    YouTube/TikTok use a standard refresh_token grant. Meta has no refresh
    tokens: a short-lived user token is exchanged for a 60-day long-lived
    one (``fb_exchange_token``) — the same exchange the social-auto-engine
    refresh helper (MIT) performs — so "refresh" for Meta re-exchanges the
    current access token and extends it. X is a confidential client: its
    token endpoint requires HTTP Basic auth (client_id:client_secret),
    not credentials in the form body — the generic form-body refresh
    used to fail against X, so X has its own branch here.
    """
    if platform == "x":
        if not refresh_token:
            raise OAuthError(
                "no_refresh_token",
                "X gave no refresh token — reconnect the channel.",
            )
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        return _token_request(platform, form, client=client, auth=(client_id, client_secret))
    if platform in ("instagram", "facebook"):
        if not access_token:
            raise OAuthError(
                "no_token",
                "No Meta access token to extend — reconnect the channel.",
            )
        form = {
            "grant_type": "fb_exchange_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "fb_exchange_token": access_token,
        }
        return _token_request(platform, form, client=client)
    if not refresh_token:
        raise OAuthError(
            "no_refresh_token",
            f"{get_provider(platform).display_name} gave no refresh token — reconnect the channel.",
        )
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return _token_request(platform, form, client=client)


# ------------------------------------------------------------------ profile / health
def fetch_profile(platform: str, access_token: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch the connected account's display name / avatar / ref.

    Returns ``{"account_name": ..., "avatar_url": ..., "account_ref": ...}``.
    Missing fields stay empty — the UI shows "Connected account" instead of
    inventing a name.
    """
    spec = get_provider(platform)
    own_client = client is None
    client = client or _new_client(30.0)
    try:
        if platform == "youtube":
            resp = client.get(spec.profile_url, headers={"Authorization": f"Bearer {access_token}"})
            _raise_for_provider_error(resp)
            items = resp.json().get("items") or []
            snippet = (items[0].get("snippet") if items else {}) or {}
            return {
                "account_name": snippet.get("title") or "",
                "avatar_url": (snippet.get("thumbnails") or {}).get("default", {}).get("url") or "",
                "account_ref": (items[0].get("id") if items else "") or "",
            }
        if platform == "tiktok":
            resp = client.get(spec.profile_url, headers={"Authorization": f"Bearer {access_token}"})
            _raise_for_provider_error(resp)
            user = resp.json().get("data", {}).get("user") or {}
            return {
                "account_name": user.get("display_name") or "",
                "avatar_url": user.get("avatar_url") or "",
                "account_ref": user.get("open_id") or "",
            }
        # Meta (instagram / facebook): Graph /me.
        resp = client.get(spec.profile_url, params={"access_token": access_token})
        _raise_for_provider_error(resp)
        data = resp.json()
        picture = (data.get("picture") or {}).get("data") or {}
        return {
            "account_name": data.get("name") or "",
            "avatar_url": picture.get("url") or "",
            "account_ref": data.get("id") or "",
        }
    except httpx.HTTPError as exc:
        raise OAuthError("network_error", f"Could not reach {spec.display_name}: {exc}") from exc
    finally:
        if own_client:
            client.close()


def check_health(platform: str, access_token: str, *, client: httpx.Client | None = None) -> dict:
    """Lightweight auth check: does the token still work? Returns {ok, message}."""
    spec = get_provider(platform)
    try:
        profile = fetch_profile(platform, access_token, client=client)
    except OAuthError as exc:
        return {"ok": False, "message": exc.message, "code": exc.code}
    name = profile.get("account_name") or "connected account"
    return {"ok": True, "message": f"{spec.display_name} token OK — {name}."}


# ------------------------------------------------------------------ refresh retry
#: Refresh failures that mean the grant itself is dead: retrying cannot
#: help, so the monitor reports Disconnected instead of burning retries.
DEFINITIVE_REFRESH_FAILURES = frozenset(
    {"invalid_grant", "invalid_client", "no_refresh_token", "no_token"}
)


def is_retryable_refresh_error(error: OAuthError) -> bool:
    """True when a refresh failure is worth retrying (transient network).

    Only ``network_error`` is retryable: a provider refusal
    (``invalid_grant`` et al.) means the stored grant is dead and the
    user must reconnect, and a bare ``provider_error`` carries no
    status, so retrying it blindly could hammer a 4xx.
    """
    return error.code == "network_error"


__all__ = [
    "DEFINITIVE_REFRESH_FAILURES",
    "OAuthError",
    "StateStore",
    "build_authorize_url",
    "check_health",
    "exchange_code",
    "fetch_profile",
    "is_retryable_refresh_error",
    "map_provider_error",
    "refresh_access_token",
]
