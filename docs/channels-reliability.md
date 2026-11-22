# Channels that stay connected — root causes & reliability (ClipForge v0.9.0)

User report: *"channels disconnect again and again."* This note records
what the code actually did before v0.9.0, why a channel kept dropping,
and what changed. No real tokens or keys appear anywhere in this file.

## What "disconnect" looked like

A channel showed **Connected** right after Connect, then — hours later,
without anyone touching anything — showed **Expired — reconnect** (or
**Not connected**), and the only way back the UI offered was the full
OAuth sign-in again. Nothing told the user it had happened: no banner,
no notification, no log row. The user discovered it by opening Channels.

## Root causes (verified in code, v0.8.2)

### R1 — Nothing ever refreshed a token on its own (the main cause)

`POST /channels/{platform}/refresh` was the *only* refresh path, and it
only ran when the user pressed **Refresh token** by hand
(`apps/api/clipforge_api/app.py::refresh_channel`). There was no
background job, no startup refresh, no scheduler entry — the scheduler
only runs due *posts* on demand. Meanwhile the access tokens are
short-lived: YouTube 1 hour, X 2 hours, TikTok 24 hours
(`clipforge/auth/providers.py::default_token_ttl_s`, X
`clipforge/x_platform.py::record_from_token_response`). So a YouTube
channel was *guaranteed* to flip to Expired within an hour of
connecting. The system was not broken in an edge case; expiry was the
normal path and nobody handled it.

### R2 — Meta tokens were never extended to 60-day long-lived tokens

`clipforge/auth/oauth.py::refresh_access_token` knows the Meta exchange
(`fb_exchange_token` → 60-day token), and the docs promise it — but the
OAuth callback (`app.py::oauth_callback`) stored the **short-lived**
(~1 hour) token returned by `exchange_code` and never called the
exchange. The only way to get a 60-day token was pressing Refresh by
hand within that first hour. Miss the hour and the short-lived token
was dead, the exchange refused it, and only a full reconnect helped.

### R3 — X refresh was broken (wrong auth method)

`refresh_access_token("x", …)` went through the generic
`_token_request`, which posts `client_id` + `client_secret` in the form
body. X requires confidential clients to authenticate with HTTP Basic
auth (`Authorization: Basic base64(client_id:client_secret)`). The
generic refresh therefore failed against X, the 2-hour X token lapsed,
and the Channels page offered X no Refresh button at all — only a full
reconnect. X was un-refreshable by design.

### R4 — Reconnecting could *destroy* the stored refresh token

The OAuth callback built a fresh `TokenRecord` from the token response
and saved it over the old one. When a provider omits `refresh_token`
in a response — Google does this on some re-grants — the stored
refresh token was overwritten with `None`. The manual refresh endpoint
already preserved the old refresh token when the provider omitted a
new one (`refresh_channel`: "keep the old one"); the callback did not.
So pressing Reconnect could leave the channel *less* refreshable than
before.

### R5 — "Expired" was treated as "reconnect from scratch"

`clipforge/auth/channels.py::channel_status` is purely clock-based:
`status = "expired" if record.is_expired() else "connected"`. It never
calls the provider, and `SettingsStore._test_platform` returned
"token expired — press Reconnect" without ever *attempting* a refresh.
An expired-but-refreshable token (YouTube, TikTok, X) was presented as
a full-reconnect job when a silent refresh would have fixed it. The
clock was right; the next step was wrong.

### R6 — OAuth state is in-memory only, so a restart breaks Connect

`StateStore` (`clipforge/auth/oauth.py`) keeps CSRF state tokens in a
process-local dict. The sidecar restarts (and v0.8.1 was precisely
about the server being killed / kept alive across app restarts), so a
state issued before a restart is "unknown or already used" after it and
the callback fails. A connect that straddles a restart fails and looks
like "connecting doesn't stick".

### R7 — No live verification on `/channels`, and the one live check (X) is too strict

`/channels` never verifies a token against the provider: a revoked
token still shows **Connected** until its clock expiry. Conversely
`GET /x/status` (`apps/api/clipforge_api/x_platform.py::x_status`)
live-checks on *every* call, and any `XError` — including a transient
network blip — reports `connected: false`. So the health signal was
simultaneously too lax (revoked tokens look fine) and too strict (a
network hiccup looks like a disconnect).

### R8 — Disconnects were silent

When a token lapsed, nothing was recorded anywhere: no notification, no
`AutomationEvent`, no dashboard banner. The only signal was the card
itself, on a page the user had to open. A channel could be dead for
days before anyone noticed.

### R9 — File-fallback tokens could be lost by a corrupt machine key

`SecretStore._get_fernet` (`clipforge/auth/secrets.py`) read
`<data_dir>/secrets/.machine.key`; if the file was corrupt it silently
generated a **new** key and overwrote the file. Every token encrypted
under the old key then failed to decrypt, `get()` returned `None`, and
the channel showed **Not connected** — with no error anywhere. (On
Windows the keyring backend is normally used, so this mostly bites
headless / keyring-less setups — but it is a silent, total loss when it
bites.)

### R10 — Env-var accounts look "connected" forever, unverified

`channel_status` treats a legacy env token (`YOUTUBE_REFRESH_TOKEN`,
`META_ACCESS_TOKEN`, …) as `connected` with no expiry, no validation
and `can_refresh: false`. A stale env token looks healthy indefinitely.

## What changed in v0.9.0 (Worker B)

### Proactive refresh before expiry

- `TokenRecord.needs_refresh()` / `seconds_until_expiry()`
  (`clipforge/auth/token_store.py`): a token needs refresh when it
  expires within **15 minutes** (`REFRESH_BEFORE_EXPIRY_S = 900` in
  `clipforge/auth/health.py`) or is already expired.
- `ChannelHealthMonitor` (`clipforge/auth/health.py`): a daemon thread
  in the sidecar (first check 60 s after the Channels/health endpoint
  is first hit, then every 15 minutes) refreshes every due token. A
  refresh is also attempted synchronously by `GET /channels/health`,
  so opening Channels never shows a stale state when a refresh is due.
- `refresh_with_retry(...)`: up to 3 attempts with backoff (1 s, 2 s,
  4 s) on **transient** failures only (`network_error`). A definitive
  failure (`invalid_grant` — refresh token dead, `invalid_client`,
  `no_refresh_token`) is *not* retried: retrying a dead grant is
  pointless and burns rate limit.

### Refresh actually works for every platform

- X refresh now uses HTTP Basic auth
  (`clipforge/auth/oauth.py::refresh_access_token`, `x` branch) — the
  method X requires for confidential clients.
- Meta: the OAuth callback now exchanges the short-lived token for a
  60-day long-lived token immediately after connect; if that exchange
  fails, the short-lived token is kept and the monitor retries the
  exchange before it expires (connect never fails because of it).
- Callback (generic + X) now preserves the previously stored refresh
  token when the provider omits a new one — reconnecting can no longer
  destroy refreshability (R4).
- `POST /channels/{platform}/reconnect` (new): manual Reconnect with
  backoff. It tries a silent refresh first; only when the refresh is
  definitively dead (HTTP 409) does the UI send the user through full
  OAuth. `POST /channels/{platform}/refresh` now uses the same
  retrying refresh and answers 409 (not 502) for definitive failures.
- Test Connection (`SettingsStore._test_platform`) now attempts a
  refresh for an expired-but-refreshable token before reporting
  "token expired".

### Honest health status

`GET /channels` cards now carry, alongside the unchanged `status`
(`connected` / `expired` / `not_connected`):

| field | meaning |
|---|---|
| `health` | `connected` \| `expiring_soon` \| `disconnected` \| `not_connected` |
| `health_reason` | plain-language reason, e.g. "Token expired 2 hours ago" |
| `health_next_step` | what to do: "Press Refresh token…" / "Press Connect again…" |
| `expires_in_s` | seconds until expiry (`null` when unknown) |
| `needs_refresh` | true when expiry is < 15 min away or past |
| `last_error` / `last_checked_at` | last monitor failure / check, persisted in `<data_dir>/channel_health.json` |

`GET /channels/health` returns the same per-platform health plus a
summary (`connected` / `expiring_soon` / `disconnected` counts). The
health assessment is clock-based (no provider call, no quota burn);
live verification stays where it belongs — Test Connection. The X
`/x/status` live check is unchanged.

The Channels page shows the health badge + reason + next step, and the
Dashboard shows a warning strip for any channel that is disconnected
or expiring soon. Env-var accounts now say so plainly: "Connected via
environment variable — ClipForge can't check when this token expires
or refresh it automatically."

### Never silently drop a channel

- The monitor **never deletes a token**. A failed refresh keeps the
  stored token; the channel is reported `disconnected` with the reason
  and the next step.
- A definitive disconnect emits a notification (see below). A
  transient refresh failure on an already-expired token also notifies
  (once per 24 h per channel — deduped); a transient failure while the
  token is still valid just records `last_error` and retries next run.

### Notifications (Worker C's system, reused — see note below)

Channel disconnects go through the shared notifications system:
`clipforge.auth.notifications` is the single store (the
``notifications`` table + ``emit_notification`` with 24 h dedupe),
`clipforge.notifications` is the thin adapter Worker C's code uses,
and the HTTP endpoints live in ``clipforge_api/app.py``
(`POST /api/notifications {level, title, message, action?, job_id?,
platform?, dedupe_key?}`, `GET /api/notifications
?unread_only=&limit=` → `{notifications, unread}`, `POST
/api/notifications/{id}/read`, `read-all`, `clear`, plus the
`GET /notifications` UI shape for the header bell). The monitor calls
``clipforge.auth.health.notify_channel_disconnected``, which posts
through the adapter with ``dedupe_key="channel-disconnected:<platform>"``
— at most one unread disconnect alert per channel per day.

**Note for the coordinator:** the task brief said to implement the
contract if Worker C's API did not exist yet. It *did* exist in the
working tree by the time this work landed (module + adapter + app
endpoints, converging while this work was in progress), so this work
reuses it instead of building a second one. Two reconciliations were
needed and are done here: the ``Notification`` model is unified
(``action`` is a plain path string, plus ``job_id`` / ``platform`` /
``dedupe_key`` columns), and ``clipforge.auth.notifications`` is the
one store both sides write to. One leftover: Worker C's standalone
router file ``apps/api/clipforge_api/notifications.py`` is **not**
registered in ``app.py`` — the endpoints live in ``app.py`` instead,
per the adapter's docstring. The coordinator should delete the
unregistered router file (or register it and remove the app.py
duplicates) — do not keep both.

### Token storage hardened

- `SecretStore` now keeps a backup machine key
  (`.machine.key.bak`). A corrupt or missing primary key is restored
  from the backup instead of silently generating a fresh key and
  orphaning every file-backend token (R9). Existing installs self-heal:
  a valid primary key writes a backup if none exists.
- Known limitation, *not* fixed: if the keyring backend is up on one
  run and down on the next, a token written to the file fallback while
  the keyring was down is shadowed by a stale keyring value when the
  keyring comes back. Fixing that needs dual-write + versioning; out of
  scope for v0.9.0, noted here so it is not rediscovered as a mystery.

## What this does NOT do (honest limits)

- **No live OAuth was verified here.** No real channel is connected,
  and Google verification / Meta App Review / TikTok audit are still
  pending, so the authorize URL, code exchange, Meta long-lived
  exchange, X Basic-auth refresh and profile fetch are verified with
  mocked provider HTTP only (see tests below). A real grant can still
  fail on the platform side (wrong redirect URI, test-user list,
  review gates) — the code reports those failures plainly instead of
  faking success. The Connect wizard is unchanged and still works for
  all 5 platforms (YouTube / Instagram / Facebook / TikTok via the
  wizard, X via its own card with the same reconnect-first flow);
  the wizard's step-5 Test Connection now attempts a refresh for an
  expired-but-refreshable token before reporting "token expired".
- The monitor refreshes on a 15-minute cadence inside the sidecar
  process. If the desktop app (and therefore the sidecar) is closed,
  no refresh happens until it is opened again — a token that expired
  while the app was closed is refreshed on the next
  `GET /channels/health` / monitor tick after launch.
- Live profile verification is deliberately *not* part of the
  background monitor (it would burn YouTube quota and turn network
  blips into false disconnects). A revoked-but-unexpired token still
  shows Connected until Test Connection or a publish attempt proves
  otherwise.
- `StateStore` is still in-memory (R6 mitigated, not fixed): the
  callback now fails with a clear "start Connect again" message, and
  the Connect wizard re-issues state, but a restart mid-OAuth still
  requires starting Connect again. Persisting state server-side is
  follow-up work.

## Tests

- `tests/unit/test_channel_health.py` — health transitions
  (connected → expiring_soon → disconnected), refresh-due boundary,
  retry vs definitive refresh failures, X Basic-auth refresh, machine-
  key backup recovery, token persistence across a simulated restart,
  monitor behaviour (refresh before expiry, definitive failure →
  honest Disconnected + notification + token kept, transient failure →
  token kept + `last_error`).
- `tests/integration/test_channel_health_api.py` — `GET
  /channels/health`, `POST /channels/{platform}/reconnect` (success +
  definitive-failure 409 + notification, network-failure 502),
  notifications endpoints (create / list / read / read-all / clear /
  dedupe), Meta long-lived exchange at connect time, reconnect
  preserving the refresh token.
- `apps/web/lib/api.test.ts` — `normalizeChannelStatus` maps the new
  health fields and distrusts unknown health values.
- Existing auth suites still green:
  `tests/unit/test_auth_*.py tests/integration/test_auth_api.py`.

## How to verify with a real account (when approvals land)

1. Connect YouTube, wait 45–60 min: the card must stay Connected
   (monitor refreshes at < 15 min to expiry); `channel_health.json`
   shows `last_refresh_at` advancing. Before v0.9.0 it flipped to
   Expired after 1 hour.
2. Connect Instagram/Facebook: `expires_at` on the card must be ~60
   days out, not ~1 hour.
3. Connect X, wait 2 h: card stays Connected (Basic-auth refresh).
4. Revoke a token on the platform, then press Test Connection: honest
   "refused — press Connect again", plus a notification, and the token
   is still stored (nothing silently deleted).
5. Turn off Wi-Fi for a few minutes around a refresh: no false
   Disconnected, `last_error` records the network failure, next run
   retries.
