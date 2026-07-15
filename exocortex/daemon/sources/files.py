"""File-touch capture over configured watched folders (watchdog)."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..filter import Event
from ..pipeline import Pipeline

log = logging.getLogger("exo.files")

# Directory names that are pure noise; events under them are skipped.
NOISE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".cache",
    ".uv-cache", ".pytest_cache", "dist", "build", "$recycle.bin",
    "appdata", ".tmp",
}
DEBOUNCE_S = 5.0


class _Handler(FileSystemEventHandler):
    def __init__(self, pipeline: Pipeline):
        self._pipeline = pipeline
        self._recent: dict[str, float] = {}

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type == "closed":
            return
        path = getattr(event, "dest_path", "") or event.src_path
        parts = {p.casefold() for p in Path(path).parts}
        if parts & NOISE_DIRS:
            return
        now = time.monotonic()
        last = self._recent.get(path, 0)
        if now - last < DEBOUNCE_S:
            return
        self._recent[path] = now
        if len(self._recent) > 2000:  # keep the debounce map bounded
            cutoff = now - DEBOUNCE_S
            self._recent = {p: t for p, t in self._recent.items() if t > cutoff}
        self._pipeline.submit(
            Event(source="file", content=str(path), kind=event.event_type)
        )


def start_file_watcher(pipeline: Pipeline, folders: list[str]) -> Observer | None:
    handler = _Handler(pipeline)
    observer = Observer()
    scheduled = 0
    for raw in folders or []:
        folder = Path(os.path.expanduser(raw))
        if not folder.is_dir():
            log.warning("watched folder missing, skipping: %s", folder)
            continue
        observer.schedule(handler, str(folder), recursive=True)
        scheduled += 1
    if not scheduled:
        return None
    observer.daemon = True
    observer.start()
    return observer
