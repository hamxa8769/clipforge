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
  served at ``/``. Otherwise the server runs API-only and ``/`` returns
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
    from clipforge.config import settings
    from clipforge_api.app import app

    web_out = base / "web" / "out"
    if (web_out / "index.html").is_file():
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
