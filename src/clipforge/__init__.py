"""ClipForge clipping engine — public package surface.

v0.1.0. Synchronous, single-process engine. No network calls except the
optional LLM / yt-dlp paths, which are guarded by explicit opt-in flags
and environment variables. No secrets are stored or logged here.
"""

from __future__ import annotations

__version__ = "0.9.3"

# Engine re-exports are optional at import time: other ClipForge components
# (API / publishing / scheduler) must stay importable while the engine
# modules are still being written by their owners. Once highlights/pipeline
# land, these re-exports resolve as before.
try:
    from clipforge.highlights import Candidate  # noqa: F401  (re-export)
    from clipforge.pipeline import run_pipeline  # noqa: F401
except ImportError:  # engine modules not yet present
    Candidate = None  # type: ignore[assignment]
    run_pipeline = None  # type: ignore[assignment]

__all__ = ["__version__", "Candidate", "run_pipeline"]
