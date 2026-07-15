"""Clipboard text capture (poll + hash dedupe). Text only, ever.

Each clipboard event carries the foreground window/app at copy time so the
firewall can drop copies made inside blocklisted apps or windows.
"""

from __future__ import annotations

import hashlib
import logging
import threading

import pyperclip

from ..filter import Event
from ..pipeline import Pipeline
from .window import read_foreground

log = logging.getLogger("exo.clipboard")


class ClipboardSource(threading.Thread):
    def __init__(self, pipeline: Pipeline, poll_s: float, max_chars: int,
                 stop: threading.Event):
        super().__init__(name="clipboard-source", daemon=True)
        self._pipeline = pipeline
        self._poll_s = poll_s
        self._max_chars = max_chars
        self._stop = stop
        self._last_hash = self._current_hash()  # prime: never re-capture stale clip
        self._prev_fg: tuple[str, str] = ("", "")  # previous tick's (title, app)

    def _current_hash(self) -> str:
        try:
            return hashlib.sha1(pyperclip.paste().encode("utf-8", "replace")).hexdigest()
        except Exception:
            return ""

    def run(self) -> None:
        while not self._stop.wait(self._poll_s):
            # sample the foreground window every tick, so when a change is
            # detected we know both the current window AND the one focused a
            # tick ago — the copy happened in one of them (alt-tab-and-paste
            # would otherwise dodge the app/title blocklist)
            title, app = "", ""
            try:
                title, app = read_foreground()
            except Exception:
                pass
            prev_title, prev_app = self._prev_fg
            self._prev_fg = (title, app)

            try:
                text = pyperclip.paste()
            except Exception:
                continue  # clipboard busy/non-text; try next tick
            if not text or not text.strip():
                continue
            h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
            if h == self._last_hash:
                continue
            self._last_hash = h
            self._pipeline.submit(
                Event(
                    source="clipboard",
                    content=text[: self._max_chars],
                    app=app,
                    window_title=title,
                    alt_app=prev_app,
                    alt_window_title=prev_title,
                )
            )
