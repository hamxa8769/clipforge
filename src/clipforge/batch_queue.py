"""Batch queue for ClipForge Pro (v0.8.0, Worker A).

An in-memory queue with JSON persistence in the app data dir
(``batches.json``), processed by a background worker thread so
``create_batch`` never blocks the caller.

Per-item states: ``queued | processing | done | failed | paused``,
with per-item progress 0-100. A batch can be paused, resumed and
cancelled. The maximum number of items processed at once comes from
the active performance profile (``clipforge.perf``) — on a ``low``
machine that is 1, so items run strictly one at a time and a slow
machine is never overloaded. The default item processor only
simulates the pipeline stages (transcribe -> score -> clip -> SEO)
with staged progress; the real pipeline is wired in by passing a
``processor`` — the queue itself never downloads or re-uploads
anything, so the own/licensed-videos-only guardrail is unaffected.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipforge.perf import get_active_profile

BATCHES_FILENAME = "batches.json"

ITEM_STATES = ("queued", "processing", "done", "failed", "paused")

# Default processor stages (name, progress after the stage).
_DEFAULT_STAGES = (
    ("transcribe", 25),
    ("score", 50),
    ("clip", 75),
    ("seo", 100),
)


def _default_processor(item: dict, set_progress, is_paused) -> None:
    """Simulated pipeline: staged progress, honours pause between stages."""
    for _stage, pct in _DEFAULT_STAGES:
        if is_paused():
            return
        time.sleep(0.05)
        if is_paused():
            return
        set_progress(pct)


@dataclass
class BatchItem:
    """One source inside a batch."""

    id: str
    source: str
    state: str = "queued"
    progress: int = 0
    error: str = ""
    result: dict = field(default_factory=dict)
    own_content: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Batch:
    """One batch: a list of sources processed in order."""

    id: str
    sources: list[str]
    items: list[BatchItem] = field(default_factory=list)
    paused: bool = False
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def status(self) -> str:
        states = [item.state for item in self.items]
        if any(s == "processing" for s in states):
            return "processing"
        if any(s == "paused" for s in states):
            return "paused"
        if any(s == "queued" for s in states):
            return "queued"
        if states and all(s == "done" for s in states):
            return "done"
        if states and all(s in ("done", "failed") for s in states):
            return "partial"
        return "queued"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status
        data["total"] = len(self.items)
        data["done"] = sum(1 for i in self.items if i.state == "done")
        data["failed"] = sum(1 for i in self.items if i.state == "failed")
        return data


class BatchQueue:
    """Thread-safe batch queue with a background dispatcher thread.

    ``processor`` is called as ``processor(item_dict, set_progress,
    is_paused)`` for each item: ``set_progress(pct)`` reports 0-100,
    ``is_paused()`` tells the processor to stop early (the item is
    then marked ``paused``). Any exception from the processor marks
    the item ``failed`` with the exception message — never a
    traceback. ``data_dir`` defaults to the app data dir.
    """

    def __init__(self, data_dir: str | Path | None = None, processor=None):
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            import os

            from clipforge.config import settings

            self.data_dir = Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.data_dir / BATCHES_FILENAME
        self._processor = processor or _default_processor
        self._lock = threading.Lock()
        self._batches: dict[str, Batch] = {}
        self._active = 0
        self._stop = threading.Event()
        self._load()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="clipforge-batch-dispatcher", daemon=True
        )
        self._dispatcher.start()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw in (data.get("batches") or {}).values():
            try:
                items = [BatchItem(**item) for item in raw.get("items", [])]
                # Anything left mid-flight when the app stopped goes
                # back to queued — it never silently stays "processing".
                for item in items:
                    if item.state == "processing":
                        item.state = "queued"
                        item.progress = 0
                batch = Batch(
                    id=str(raw["id"]),
                    sources=list(raw.get("sources", [])),
                    items=items,
                    paused=bool(raw.get("paused", False)),
                    cancelled=bool(raw.get("cancelled", False)),
                    created_at=float(raw.get("created_at", time.time())),
                )
                self._batches[batch.id] = batch
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        try:
            payload = {"batches": {bid: b.to_dict() for bid, b in self._batches.items()}}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            # Persistence is best-effort: a failed save must never
            # break queueing itself.
            pass

    # ------------------------------------------------------------ public API
    def create_batch(self, sources: list[str], own_content: bool = False) -> str:
        """Queue a batch; returns the batch id immediately (non-blocking)."""
        clean = [str(s).strip() for s in (sources or []) if str(s).strip()]
        if not clean:
            raise ValueError("Give at least one source to queue.")
        batch_id = uuid.uuid4().hex[:12]
        batch = Batch(
            id=batch_id,
            sources=clean,
            items=[
                BatchItem(id=uuid.uuid4().hex[:8], source=s, own_content=own_content) for s in clean
            ],
        )
        with self._lock:
            self._batches[batch_id] = batch
            self._save()
        return batch_id

    def get_batch(self, batch_id: str) -> dict | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            return batch.to_dict() if batch else None

    def list_batches(self) -> list[dict]:
        with self._lock:
            return [
                b.to_dict()
                for b in sorted(self._batches.values(), key=lambda b: b.created_at, reverse=True)
            ]

    def pause_batch(self, batch_id: str) -> dict | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            batch.paused = True
            self._save()
            return batch.to_dict()

    def resume_batch(self, batch_id: str) -> dict | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            batch.paused = False
            for item in batch.items:
                if item.state == "paused":
                    item.state = "queued"
            self._save()
            return batch.to_dict()

    def cancel_batch(self, batch_id: str) -> dict | None:
        """Cancel: queued items fail at once; in-flight items fail
        when they next check in (marked "failed", error "cancelled")."""
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            batch.cancelled = True
            for item in batch.items:
                if item.state in ("queued", "paused"):
                    item.state = "failed"
                    item.error = "cancelled"
            self._save()
            return batch.to_dict()

    def max_concurrent(self) -> int:
        """Concurrency cap from the active perf profile (>= 1)."""
        try:
            settings = get_active_profile(data_dir=self.data_dir)["settings"]
            return max(1, int(settings.get("max_concurrent_jobs", 1)))
        except Exception:
            return 1

    def wait_until_done(self, batch_id: str, timeout: float = 30.0) -> dict | None:
        """Block (tests / scripts only) until no item is queued,
        processing or paused, or the timeout elapses."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            batch = self.get_batch(batch_id)
            if batch is None:
                return None
            if batch["status"] in ("done", "partial"):
                return batch
            time.sleep(0.05)
        return self.get_batch(batch_id)

    def shutdown(self) -> None:
        """Stop the dispatcher (tests / clean shutdown). Idempotent."""
        self._stop.set()

    # ------------------------------------------------------------ internals
    def _item_is_paused(self, batch: Batch) -> bool:
        return batch.paused or batch.cancelled

    def _run_item(self, batch: Batch, item: BatchItem) -> None:
        def set_progress(pct: int) -> None:
            with self._lock:
                item.progress = max(0, min(100, int(pct)))
                self._save()

        def is_paused() -> bool:
            with self._lock:
                return self._item_is_paused(batch)

        try:
            result = self._processor(item.to_dict(), set_progress, is_paused)
        except Exception as exc:  # processor errors never kill the queue
            with self._lock:
                item.state = "failed"
                item.error = str(exc)[:300] or exc.__class__.__name__
                self._save()
            return
        with self._lock:
            if isinstance(result, dict) and result:
                item.result = result
            if batch.cancelled:
                item.state = "failed"
                item.error = "cancelled"
            elif self._item_is_paused(batch):
                item.state = "paused"
            else:
                item.state = "done"
                item.progress = 100
            self._save()

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch_once()
            except Exception:
                pass  # the dispatcher must never die on a bad batch
            time.sleep(0.1)

    def _dispatch_once(self) -> None:
        with self._lock:
            if self._active >= self.max_concurrent():
                return
            target: tuple[Batch, BatchItem] | None = None
            for batch in sorted(self._batches.values(), key=lambda b: b.created_at):
                if batch.paused or batch.cancelled:
                    continue
                for item in batch.items:
                    if item.state == "queued":
                        target = (batch, item)
                        break
                if target:
                    break
            if target is None:
                return
            batch, item = target
            item.state = "processing"
            self._active += 1
            self._save()

        def _runner() -> None:
            try:
                self._run_item(batch, item)
            finally:
                with self._lock:
                    self._active -= 1

        threading.Thread(target=_runner, name=f"clipforge-batch-{item.id}", daemon=True).start()


# One shared queue per process (the API routers use this).
_default_queue: BatchQueue | None = None
_default_lock = threading.Lock()


def get_queue(data_dir: str | Path | None = None, processor=None) -> BatchQueue:
    """The shared process-wide queue (created on first use)."""
    global _default_queue
    with _default_lock:
        if _default_queue is None:
            _default_queue = BatchQueue(data_dir=data_dir, processor=processor)
        return _default_queue


__all__ = [
    "BATCHES_FILENAME",
    "ITEM_STATES",
    "Batch",
    "BatchItem",
    "BatchQueue",
    "get_queue",
]
