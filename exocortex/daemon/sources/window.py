"""Active-window title capture. Windows via ctypes; Linux X11 via xdotool.

Wayland exposes no portable foreground-window API — there the source logs a
one-line warning and stays idle (clipboard/file capture still work).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading

import psutil

from ..filter import Event
from ..pipeline import Pipeline

log = logging.getLogger("exo.window")

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

    def read_foreground() -> tuple[str, str]:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = ""
        if pid.value:
            try:
                app = psutil.Process(pid.value).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                app = ""
        return buf.value, app

else:
    def read_foreground() -> tuple[str, str]:
        if not shutil.which("xdotool"):
            return "", ""
        try:
            wid = subprocess.run(
                ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if not wid:
                return "", ""
            title = subprocess.run(
                ["xdotool", "getwindowname", wid], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            pid = subprocess.run(
                ["xdotool", "getwindowpid", wid], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            app = ""
            if pid.isdigit():
                try:
                    app = psutil.Process(int(pid)).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    app = ""
            return title, app
        except (subprocess.SubprocessError, OSError):
            return "", ""


class WindowSource(threading.Thread):
    """Polls the foreground window; submits an event when it changes."""

    def __init__(self, pipeline: Pipeline, poll_s: float, stop: threading.Event):
        super().__init__(name="window-source", daemon=True)
        self._pipeline = pipeline
        self._poll_s = poll_s
        self._stop = stop
        self._last: tuple[str, str] | None = None
        if sys.platform.startswith("linux") and not shutil.which("xdotool"):
            log.warning("xdotool not found (or Wayland) — window titles disabled")

    def run(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                title, app = read_foreground()
            except Exception:
                log.exception("window read failed")
                continue
            if not title or (title, app) == self._last:
                continue
            self._last = (title, app)
            self._pipeline.submit(
                Event(source="window", content=title, app=app, window_title=title)
            )
