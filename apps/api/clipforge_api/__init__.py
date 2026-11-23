"""ClipForge API package.

NOTE: this package deliberately does NOT import ``clipforge_api.app``.
Importing the app creates the database engine at module level
(``_engine = init_db()`` in app.py), which binds the SQLite file from
``DATABASE_URL`` as it is at import time. The server launcher
(``__main__._prepare_environment``) pins ``DATABASE_URL`` to
``<data_dir>/clipforge.db`` *after* the package is imported but
*before* it imports the app — so an eager import here bound the engine
to the default ``./clipforge.db`` (CWD-relative) while the job queue /
dispatcher, which build fresh sessions later, used the data-dir DB.
Two databases, no shared jobs: jobs created through the API sat
``pending`` forever, ``/worker/queue`` 500d with "no such table:
jobs", and no clip ever started (found by the v0.9.1 E2E run).
Import the app explicitly: ``from clipforge_api.app import app``.
"""
