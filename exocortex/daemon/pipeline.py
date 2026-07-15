"""Single choke point between capture sources and the database.

Sources call ``submit`` (from any thread); only the daemon's writer thread
calls ``flush``. Work mode is enforced three times: at submit, at toggle
(``purge_non_manual``), and again at flush — so an event captured around a
work-mode toggle can never land on disk.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from ..state.db import now_ms
from .filter import MANUAL, Decision, Event, Firewall


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, firewall: Firewall, device_id: str,
                 max_queue: int = 5000, inline_flush: bool = False):
        self._conn = conn                    # owned by the flush/writer thread
        self._firewall = firewall
        self._device = device_id
        self._lock = threading.Lock()
        self._queue: list[tuple] = []
        self._max_queue = max_queue
        # inline_flush: for SINGLE-THREADED bulk imports (nightly, one-shot
        # scripts) — submit() flushes as the queue fills so the memory cap
        # never sheds events. The daemon keeps this False: there, submit()
        # runs on source threads and only the main loop may touch the conn.
        self._inline_flush = inline_flush
        self.work_mode = False               # set by the daemon's control poller
        self.submitted = 0
        self.dropped = 0
        self.written = 0

    def submit(self, event: Event) -> bool:
        with self._lock:
            self.submitted += 1
        decision: Decision = self._firewall.decide(event, self.work_mode)
        if not decision.allow:
            with self._lock:
                self.dropped += 1
            return False
        row = (
            event.ts or now_ms(), self._device, event.source, event.kind,
            decision.content, event.app, decision.sensitivity,
            json.dumps(event.extra) if event.extra else "{}",
        )
        with self._lock:
            if len(self._queue) >= self._max_queue:
                self._queue.pop(0)           # shed oldest; capture must never OOM
            self._queue.append(row)
            pending = len(self._queue)
        if self._inline_flush and pending >= 200:
            self.flush()
        return True

    def set_work_mode(self, on: bool) -> None:
        turning_on = on and not self.work_mode
        self.work_mode = on
        if turning_on:
            self.purge_non_manual()

    def purge_non_manual(self) -> int:
        """Drop queued-but-unwritten events, keeping only manual reflections."""
        with self._lock:
            before = len(self._queue)
            self._queue = [r for r in self._queue if r[2] == MANUAL]
            purged = before - len(self._queue)
            self.dropped += purged
        return purged

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def flush(self) -> int:
        with self._lock:
            rows, self._queue = self._queue, []
        if (self.work_mode or self._firewall.forced_work_mode) and rows:
            kept = [r for r in rows if r[2] == MANUAL]
            self.dropped += len(rows) - len(kept)
            rows = kept
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT INTO life_stream (ts, device, source, kind, content, app, sensitivity, extra)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        self.written += len(rows)
        return len(rows)
