"""``clipforge-server`` launcher — entry point for the packaged executable.

This is what PyInstaller bundles into the one-file ``clipforge-server``
executable (see ``clipforge-server.spec``). It also works as a plain
``python -m clipforge_api`` run from a source checkout.

Behaviour:

* **Base directory** — when frozen, the folder holding the executable;
  from source, the repo root. All defaults below are resolved against it,
  so the exe works no matter which folder it is launched from.
* **``.env``** — a ``.env`` file next to the executable (or in the current
  folder) is loaded first. It is a minimal parser: ``KEY=VALUE`` lines,
  ``#`` comments, optional quotes. Values already present in the
  environment always win. No secrets are ever printed.
* **Data directory** — unless ``CLIPFORGE_DATA_DIR`` / ``DATABASE_URL`` are
  set, the SQLite database lives at ``<base>/data/clipforge.db``. This
  matters for the frozen exe, whose default relative ``clipforge.db``
  would otherwise land in whatever the user's current folder is.
* **Web UI** — the Next.js app is a server-rendered app, so it cannot be
  served from inside this Python executable. If a *static export* of the
  web UI is present at ``<base>/web/out`` (``index.html`` exists), it is
  served at ``/``. ``CLIPFORGE_WEB_OUT`` overrides that location: the
  Tauri desktop shell sets it, because the Linux bundles install the
  sidecar to ``/usr/bin`` while the export lives under the app resources
  dir (``<resources>/binaries/web/out``), so "next to the exe" does not
  hold there. Otherwise the server runs API-only and ``/`` returns
  a small status page explaining how to start the web UI (bundled
  ``start-web`` script, needs Node.js 20+). Nothing is faked: the page
  says plainly which mode is active.

Run:  clipforge-server [--host 127.0.0.1] [--port 8000]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    """Folder the executable / repo lives in (frozen exe or source tree)."""
    if getattr(sys, "frozen", False):  # PyInstaller one-file build
        return Path(sys.executable).resolve().parent
    # Source checkout: <repo>/apps/api/clipforge_api/__main__.py
    return Path(__file__).resolve().parents[3]


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines into os.environ (never overriding it)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _prepare_environment(base: Path) -> Path:
    """Load .env, pin data/database defaults next to the executable."""
    for candidate in (base / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            _load_dotenv(candidate)
            break
    data_dir = Path(os.environ.get("CLIPFORGE_DATA_DIR") or (base / "data"))
    os.environ.setdefault("CLIPFORGE_DATA_DIR", str(data_dir))
    if not os.environ.get("DATABASE_URL") and not os.environ.get("CLIPFORGE_DATABASE_URL"):
        # Absolute sqlite path: sqlite:///<abs> == sqlite:////abs/...
        os.environ["DATABASE_URL"] = f"sqlite:///{(data_dir / 'clipforge.db').as_posix()}"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


_STATUS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ClipForge API</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;
padding:0 1rem;line-height:1.5}}code{{background:#eee;padding:.1em .3em}}</style>
</head><body>
<h1>ClipForge API {version}</h1>
<p>The API server is running (dry-run: <b>{dry_run}</b>).</p>
<ul>
<li>API docs: <a href="/docs">/docs</a></li>
<li>Health: <a href="/health">/health</a></li>
<li>Jobs: <a href="/jobs">/jobs</a></li>
</ul>
<h2>Web review UI</h2>
<p>This build runs <b>API-only</b>: no static web export was found next to
the executable (<code>web/out/index.html</code>). Start the bundled web UI
with the <code>start-web</code> script in this folder (needs Node.js 20+),
or run the web app from source with <code>npm run dev</code> in
<code>apps/web</code>. The web UI talks to this API on port {port}.</p>
<p>ClipForge publishes only after a human approves a post, and dry-run is
the default. See README.txt in this folder.</p>
</body></html>
"""


def _build_app(base: Path):
    """Import the FastAPI app and attach the web UI / status page."""
    # Env vars must be set before this import: settings are read at import.
    from clipforge import __version__  # noqa: F401  (re-exported below)
    from clipforge.auth.providers import PLATFORMS as _PLATFORMS
    from clipforge.config import settings
    from clipforge_api.app import app

    web_out_env = os.environ.get("CLIPFORGE_WEB_OUT", "").strip()
    if web_out_env:
        web_out = Path(web_out_env)
    else:
        # Frozen exe: <exe-dir>/web/out. Source checkout: the export
        # lives at <repo>/apps/web/out, NOT <repo>/web/out — the old
        # code only tried the frozen layout, so `python -m
        # clipforge_api` from a checkout silently ran API-only and
        # every browser navigation to a UI route answered raw JSON
        # (v0.6.0: found by the coordinator's live audit).
        candidates = [base / "web" / "out", base / "apps" / "web" / "out"]
        web_out = next(
            (c for c in candidates if (c / "index.html").is_file()),
            candidates[0],
        )
    if (web_out / "index.html").is_file():
        # Serve the static export BEFORE API routing — but only for
        # paths that look like files. A plain ``app.mount("/",
        # StaticFiles(...))`` loses to the API routes: GET
        # /channels/{platform} would claim the Next.js RSC payload
        # /channels/index.txt and 404 it as an "unknown platform", so
        # every Channels visit logged console 404s (the page still
        # loaded via Next's full-page fallback, but noisily). This
        # middleware serves a path only when its last segment has a
        # file extension (index.txt, index.html, favicon.ico, _next
        # assets) and the file really exists under web_out. It ALSO
        # serves the exported page for a real browser navigation
        # (Accept: text/html) to an extensionless UI route such as
        # /channels/connect — otherwise that full-page load hits
        # GET /channels/{platform} and the user sees raw JSON
        # ({"detail": "unknown platform: connect"}). API fetches
        # (Accept: */*) are unaffected, so GET /channels still
        # returns the API's JSON list and /channels/ still reaches
        # the mount below. The mount stays: it serves directory
        # indexes (/channels/ → index.html) and the UI root.
        from urllib.parse import unquote

        from starlette.responses import FileResponse

        web_root = web_out.resolve()

        class _StaticFirstMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    raw_path = unquote(scope.get("path", "/"))
                    last = raw_path.rstrip("/").rsplit("/", 1)[-1]
                    if "." in last:
                        candidate = (web_root / raw_path.lstrip("/")).resolve()
                        try:
                            candidate.relative_to(web_root)
                        except ValueError:
                            candidate = None  # path traversal: let the API answer
                        if candidate is not None and candidate.is_file():
                            await FileResponse(candidate)(scope, receive, send)
                            return
                    elif scope.get("method", "GET") == "GET":
                        # Browser navigation to a UI route (v0.4.1): a
                        # full-page load of /channels, /channels/connect,
                        # /settings, /jobs, /monetization, /jobs/view, ...
                        # used to fall through to the API routes, because
                        # FastAPI matches /channels/{platform} (and
                        # /channels, /settings, /jobs, /monetization,
                        # /jobs/{job_id}) before the StaticFiles mount
                        # below. The user then saw raw JSON — e.g.
                        # {"detail": "unknown platform: connect"} — for
                        # clicking "Connect a channel" (a plain <a>, so a
                        # full navigation, not a client-side one). Serve
                        # the exported page instead, but ONLY for real
                        # browser navigations: those send
                        # Accept: text/html, while the web UI's own
                        # fetch() calls (and curl / API clients) send
                        # Accept: */* and must keep getting JSON. A path
                        # with no exported directory (e.g.
                        # /channels/bogus-platform) still falls through
                        # to the API, which 404s it as before. UI-shaped
                        # paths with no exported page at all (/jobs,
                        # /channels/<platform>, /posts,
                        # /auth/<platform>/start, /jobs/<id>/...) redirect
                        # (303) to the real page that answers them instead
                        # (v0.6.0, below).
                        headers = dict(scope.get("headers", []))
                        accept = headers.get(b"accept", b"").decode("latin-1")
                        if "text/html" in accept:
                            candidate = (web_root / raw_path.lstrip("/")).resolve()
                            try:
                                candidate.relative_to(web_root)
                            except ValueError:
                                candidate = None  # path traversal: let the API answer
                            if candidate is not None and candidate.is_dir():
                                index = candidate / "index.html"
                                if index.is_file():
                                    await FileResponse(index)(scope, receive, send)
                                    return
                            # v0.6.0: UI-shaped paths with no exported
                            # page. The exported page above wins whenever
                            # one exists; what is left here used to fall
                            # through to the API and show raw JSON on a
                            # full-page load (refresh / pasted URL /
                            # back-forward). Each case redirects the
                            # browser navigation to the real page that
                            # answers it. API fetches (Accept: */*) never
                            # reach this branch and still get JSON, and an
                            # unknown platform still falls through to the
                            # API, which 404s it as before.
                            from urllib.parse import quote

                            from starlette.responses import RedirectResponse

                            parts = [s for s in raw_path.split("/") if s]

                            async def _redirect(url: str):
                                await RedirectResponse(url=url, status_code=303)(
                                    scope, receive, send
                                )

                            if parts == ["jobs"]:
                                # No /jobs list page exists (no
                                # jobs/page.tsx), yet the Pipeline page
                                # links "Go to Jobs" here — a direct load
                                # answered GET /jobs, raw JSON ([]).
                                # Automation is the jobs list (runner,
                                # rules & history).
                                await _redirect("/automation")
                                return
                            if parts and parts[0] == "channels" and len(parts) >= 2:
                                if parts[1] == "connect" and len(parts) == 3:
                                    # /channels/connect/<platform>
                                    target = "/channels/connect"
                                    if parts[2] in _PLATFORMS:
                                        target += f"?platform={parts[2]}"
                                    await _redirect(target)
                                    return
                                if parts[1] in _PLATFORMS:
                                    # /channels/<platform> has no exported
                                    # page; it answered the channel-status
                                    # JSON. The Connect wizard (preselected
                                    # via ?platform=) is the page for it.
                                    await _redirect(f"/channels/connect?platform={parts[1]}")
                                    return
                            if len(parts) >= 2 and parts[0] == "jobs":
                                if parts[1] == "view":
                                    # /jobs/view/<anything> — the review
                                    # page itself; deeper paths have no page.
                                    await _redirect("/jobs/view/")
                                    return
                                # Live job ids have no exported directory
                                # (only demo ids are generated at build
                                # time), so a direct load / refresh of
                                # /jobs/<live-id> would otherwise fall
                                # through to the API and show raw JSON.
                                # Redirect to the review page that reads
                                # the id from the query string. Deeper
                                # paths (/jobs/<id>/candidates, ...) are
                                # API shapes, but a browser landing on one
                                # gets the same review page rather than
                                # raw JSON.
                                await _redirect(f"/jobs/view/?id={quote(parts[1], safe='')}")
                                return
                            if parts and parts[0] == "posts":
                                # No /posts page; the approval queue lists
                                # posts. A direct load answered GET /posts,
                                # raw JSON.
                                await _redirect("/queue")
                                return
                            if len(parts) >= 2 and parts[0] == "auth" and parts[1] in _PLATFORMS:
                                # /auth/<platform>/start is a fetch-only API
                                # endpoint; a browser landing on it saw raw
                                # JSON (or a 409). The Connect wizard is the
                                # page that starts it. /auth/callback/* is
                                # untouched: it already answers HTML.
                                await _redirect(f"/channels/connect?platform={parts[1]}")
                                return
                await self.app(scope, receive, send)

        # Starlette refuses add_middleware once the app has served a
        # request (the middleware stack is cached), so clear the cache
        # first — this only matters when _build_app runs after first
        # use, e.g. in tests; at real startup the stack is empty.
        app.middleware_stack = None
        app.add_middleware(_StaticFirstMiddleware)

        from fastapi.staticfiles import StaticFiles

        # Mounted last, so /health, /docs, /jobs, ... keep working.
        app.mount("/", StaticFiles(directory=str(web_out), html=True), name="web")
        web_mode = "web UI: static export (web/out)"
    else:
        from fastapi.responses import HTMLResponse

        @app.get("/", include_in_schema=False)
        def _root():
            return HTMLResponse(
                _STATUS_PAGE.format(
                    version=__version__,
                    dry_run=settings.dry_run,
                    port=settings.api_port,
                )
            )

        web_mode = "web UI: not bundled — API only (see start-web script)"
    return app, settings, __version__, web_mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clipforge-server",
        description="ClipForge API server (FastAPI). Serves the web UI too "
        "when a static export is present at web/out next to the executable.",
    )
    parser.add_argument("--host", default=None, help="bind host (default: env or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: env or 8000)")
    args = parser.parse_args(argv)

    base = _base_dir()
    data_dir = _prepare_environment(base)

    # Source runs: make src/ and apps/api importable from anywhere.
    for candidate in (base / "src", base / "apps" / "api"):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))

    app, settings, version, web_mode = _build_app(base)
    host = args.host or settings.api_host
    port = args.port or settings.api_port

    print(f"ClipForge {version} — API server", flush=True)
    print(f"  API:      http://{host}:{port}  (docs: http://{host}:{port}/docs)", flush=True)
    print(f"  {web_mode}", flush=True)
    print(f"  Data dir: {data_dir}", flush=True)
    print(f"  Dry run:  {settings.dry_run}  (nothing publishes live while true)", flush=True)
    print("  Press Ctrl+C to stop.", flush=True)

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
