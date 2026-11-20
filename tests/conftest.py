"""Shared fixtures for the ClipForge test suite.

Test isolation: the API app creates its database at import time from
``DATABASE_URL`` / ``CLIPFORGE_DATABASE_URL`` (default
``sqlite:///./clipforge.db``). This module forces both variables to a
throwaway SQLite file in the system temp dir BEFORE any ``clipforge``
module is imported, so tests never touch the repo's real database.

``src/`` and ``apps/api/`` are put on ``sys.path`` so tests can import
both the engine (``clipforge.*``) and the API app (``clipforge_api.app``).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
API_DIR = ROOT / "apps" / "api"
for _p in (str(SRC), str(API_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_TEST_DB = Path(tempfile.gettempdir()) / f"clipforge-test-{os.getpid()}.db"
for _var in ("DATABASE_URL", "CLIPFORGE_DATABASE_URL"):
    os.environ[_var] = f"sqlite:///{_TEST_DB}"

import pytest  # noqa: E402


def load_or_skip(module_name: str):
    """Import a module, or skip the test if it has not landed yet."""
    try:
        __import__(module_name)
    except ImportError:
        pytest.skip(
            f"{module_name} not implemented yet — contract test, see ARCHITECTURE.md §7",
            allow_module_level=False,
        )
    return sys.modules[module_name]
