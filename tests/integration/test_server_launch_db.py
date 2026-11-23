"""Regression tests for the v0.9.1 server-launch DB split.

Root cause (found by the v0.9.1 E2E run): ``clipforge_api/__init__.py``
eagerly imported ``clipforge_api.app``, whose module-level
``_engine = init_db()`` bound the SQLite file from ``DATABASE_URL`` as
it was at import time. The launcher (``__main__._prepare_environment``)
pins ``DATABASE_URL`` to ``<data_dir>/clipforge.db`` *after* the
package import but *before* importing the app — so the API engine was
bound to the default ``./clipforge.db`` (CWD-relative) while the job
queue / dispatcher, which build fresh sessions later, used the
data-dir DB. Two databases, no shared jobs: a job created through the
API sat ``pending`` forever (its auto-enqueue failed silently against
the empty data-dir DB) and ``/worker/queue`` 500d with "no such table:
jobs".

Both tests run in a fresh interpreter so the import order is real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
API_DIR = ROOT / "apps" / "api"


def _child_env(data_dir: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    # A real launch has no DATABASE_URL: the launcher pins it. The
    # test process itself sets one (see tests/conftest.py) — scrub it
    # or the child would inherit the test DB and prove nothing.
    env.pop("DATABASE_URL", None)
    env.pop("CLIPFORGE_DATABASE_URL", None)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), str(API_DIR)])
    if data_dir is not None:
        env["CLIPFORGE_DATA_DIR"] = str(data_dir)
    return env


def test_package_import_does_not_import_app(tmp_path: Path) -> None:
    """``import clipforge_api`` must not import the app (no DB engine)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import clipforge_api, sys; "
            "assert 'clipforge_api.app' not in sys.modules, "
            "'eager app import binds the DB engine at package import'",
        ],
        env=_child_env(None),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    # No database file may appear as a side effect of the import.
    assert not (tmp_path / "clipforge.db").exists()
    assert not list(tmp_path.glob("*.db"))


def test_launcher_pins_database_before_app_import(tmp_path: Path) -> None:
    """After ``_prepare_environment``, the app engine is the data-dir DB."""
    data_dir = tmp_path / "data"
    script = tmp_path / "check_launch_db.py"
    script.write_text(
        textwrap.dedent(
            f"""\
            import os
            from pathlib import Path
            from clipforge_api.__main__ import _prepare_environment
            data_dir = _prepare_environment(Path({str(tmp_path)!r}))
            assert os.environ["DATABASE_URL"] == (
                "sqlite:///" + (data_dir / "clipforge.db").as_posix()
            ), os.environ["DATABASE_URL"]
            from clipforge_api import app as app_mod
            from clipforge.db import database_url
            assert database_url() == os.environ["DATABASE_URL"]
            assert str(app_mod._engine.url) == database_url(), app_mod._engine.url
            from sqlalchemy import inspect
            assert "jobs" in inspect(app_mod._engine).get_table_names()
            print("single DB:", database_url())
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        env=_child_env(data_dir),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "single DB: sqlite:///" in proc.stdout
    assert (data_dir / "clipforge.db").is_file()
