# Connecting a channel — ClipForge v0.3.0

Every channel connects through the platform's **official OAuth flow**.
ClipForge never asks for your password, cookies, or a pasted token, and
never uses Selenium / unofficial uploaders — those get accounts flagged
(see the research report §7, §9c).

The flow is the same for all four platforms:

1. **Create the developer app** on the platform (steps per platform below).
2. **Settings page** — paste the app's **client id** and **client secret**,
   copy the **redirect URI** shown there into the platform app exactly
   as shown. The secret is stored encrypted on the server (OS keyring
   where available, else an encrypted file in the data folder); the UI
   only ever shows its last 4 characters.
3. **Channels page → Connect** — the platform's own sign-in page opens.
   Grant access, then close the tab and return to ClipForge.
4. The card shows **Connected** with the account name, scopes granted,
   daily cap, and token expiry. **Expired** means the token ran out —
   press **Reconnect** (or **Refresh token** where the platform allows
   it). **Disconnect** deletes the stored token.

If a step fails, **Test connection** on the Settings page reports the
exact provider error (e.g. `invalid_client`, token expired, redirect
URI mismatch) instead of a generic failure.

## Redirect URI

All four platforms redirect back to the same loopback address:

```
http://127.0.0.1:8000/auth/callback/<platform>
```

- The desktop app serves the API on port 8000 by default. If it picked a
  different port, set the desktop port mode to manual 8000, or set
  `CLIPFORGE_REDIRECT_BASE` (e.g. `http://127.0.0.1:8123`) before
  starting the server — the Settings page always shows the URI the
  server is actually using, so copy that one.
- The URI must match **exactly** what is registered in the platform
  app, or the platform refuses the callback (`redirect_uri_mismatch`).

## YouTube

1. Google Cloud Console → create a project → enable **YouTube Data API v3**.
2. APIs & Services → Credentials → Create **OAuth client ID** (Desktop app).
3. Add `http://127.0.0.1:8000/auth/callback/youtube` to **Authorized
   redirect URIs**. Copy the client ID + secret into Settings.
4. **OAuth verification:** until Google verifies the app, only test users
   added in Cloud Console → OAuth consent screen → Test users can
   connect. Public posting also needs the project's quota (videos.insert
   costs quota — see `src/clipforge/publishing/limits.py`, VERIFY_LIVE).
5. Scopes granted: `youtube.upload`, `youtube.readonly`. ClipForge asks
   for offline access, so it gets a refresh token and can refresh
   without asking you to sign in again.
6. Fallback while verification is pending: **manual-post pack** — download
   the clip + SEO copy from the queue page and upload in YouTube Studio.

## Instagram

1. Meta for Developers → create an app → add the **Instagram Graph API**
   product (and **Facebook Login**).
2. Connect a **professional Instagram account (Business or Creator)**
   linked to a **Facebook Page** — personal accounts cannot publish via
   the API. Add the redirect URI
   `http://127.0.0.1:8000/auth/callback/instagram` to Valid OAuth
   Redirect URIs.
3. **App Review + Business Verification** are required for
   `instagram_content_publish`. Until they pass, only test users and the
   app's own account can connect.
4. Scopes: `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, `pages_read_engagement`. Tokens are extended to
   60-day long-lived tokens (Refresh token button).
5. Fallback while review is pending: **manual-post pack** — post the clip
   in the Instagram app.

## Facebook

1. Same Meta app as Instagram. Add a **Facebook Page** (not a personal
   profile) to the app; redirect URI
   `http://127.0.0.1:8000/auth/callback/facebook`.
2. **App Review + Business Verification** are required for
   `pages_manage_posts`. Until they pass, only test users can connect.
3. Scopes: `pages_show_list`, `pages_read_engagement`,
   `pages_manage_posts`. Tokens are extended to 60-day long-lived tokens.
4. Fallback while review is pending: **manual-post pack** — post the Reel
   on the Page.

## TikTok

1. TikTok for Developers → create an app → add **Login Kit** and
   **Content Posting API**. Redirect URI
   `http://127.0.0.1:8000/auth/callback/tiktok`.
2. Scopes: `user.info.basic`, `video.upload`.
3. **Content Posting API audit is pending** (and required): unaudited
   apps can only create **private / inbox drafts** — max 5 pending
   drafts per user per 24h. Public Direct Post needs the passed audit
   (submit the app with a demo video showing the posting screen and the
   AI-content label flow).
4. Fallback until the audit passes: **inbox draft** — the clip is uploaded
   as a private draft to your TikTok inbox; open TikTok and post it from
   there. Nothing is posted publicly without the audit.

## Staying connected (v0.9.0)

Channels stay connected on their own now — see
`docs/channels-reliability.md` for the full root-cause list and
design. In short:

- **Automatic refresh:** a background monitor refreshes each token
  before it expires (15 minutes early), with retry + backoff on
  network failures. YouTube (1 h), X (2 h) and TikTok (24 h) tokens no
  longer lapse just because nobody pressed a button.
- **Meta:** connecting now exchanges the ~1-hour token for a 60-day
  long-lived token immediately.
- **Reconnect button:** tries a silent refresh first; only a dead
  grant (409) sends you through the full sign-in again.
- **Health:** every card shows `Connected` / `Expiring soon` /
  `Disconnected` with the reason in plain words and what to do next
  (`GET /channels/health` for the same data + a summary). The
  Dashboard's "Needs attention" list shows disconnected / expiring
  channels too.
- **Never silent:** a disconnect writes a notification (header bell,
  `GET /api/notifications`) — at most one per channel per day — and
  the token is never deleted by a failed refresh.

If a channel still shows Disconnected, press **Reconnect** on its
card; if that asks you to sign in again, the stored grant is dead
(revoked on the platform, or the app credentials changed) and only a
fresh sign-in helps.

| Secret | Storage |
|---|---|
| OAuth tokens (per platform) | Encrypted blob in the secret store (see below) |
| App client id / secret, LLM key | Same secret store |
| Machine key (file fallback only) | `<data_dir>/secrets/.machine.key`, mode 0600 |
| Non-secret preferences | `<data_dir>/settings.json` (plain JSON, no secrets) |
| Database | Only account label / ref / scopes / status — never a token |

Secret store backend (shown on the Settings page):

- **OS keyring** — Windows Credential Manager, macOS Keychain, Linux
  Secret Service. Preferred; used automatically when a working backend
  exists.
- **Encrypted file** — Fernet (AES-128-CBC + HMAC) files under
  `<data_dir>/secrets/`, keyed by the machine key above. Used when no
  keyring backend is available (e.g. headless Linux). The Settings page
  says which backend is active.

Legacy environment-variable accounts (`YOUTUBE_REFRESH_TOKEN`,
`META_ACCESS_TOKEN`, `TIKTOK_ACCESS_TOKEN`) still count as connected
(shown as source `env`), but new connections should use OAuth above.

## Testing the OAuth flow without real keys

Without platform apps/keys you can still verify everything except the
platform's own pages:

1. Start the server, open **Channels** — all four cards say
   **Not connected** with the review notice and fallback for each.
2. Open **Settings** — all credentials show **Not set**; Test connection
   answers `… is not connected — press Connect on the Channels page first.`
3. Press **Connect** with no credentials saved — the API answers `409`
   and the UI tells you to save the app credentials first.
4. Save dummy credentials (any string) in Settings — the card still says
   Not connected, but Connect now opens the platform's real authorize
   URL (Google/Facebook/TikTok sign-in). The platform will refuse the
   dummy client id — that refusal comes from the platform, proving the
   URL, scopes, and redirect URI are correctly formed.
5. With the test suite (`pytest tests/unit/test_auth_*.py
   tests/integration/test_auth_api.py`) the full exchange is exercised
   with mocked provider HTTP: connect → connected card → refresh →
   disconnect.

What **cannot** be verified without real platform apps/keys: the actual
grant on the platform's sign-in page, the token exchange against the
live provider, profile fetch, long-lived Meta exchange, the TikTok
audit, and any live upload.
