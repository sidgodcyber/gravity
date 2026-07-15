"""Periodic browser-history import (Chrome / Edge / Firefox).

Reads each profile's history database (copied to a temp file first — the
originals are locked while the browser runs), takes only entries newer than
the stored checkpoint, and submits every URL through the firewall pipeline.
Never runs while work mode is on; the daemon guards that, and the pipeline
would drop the rows anyway.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ...state import db
from ..filter import Event
from ..pipeline import Pipeline

log = logging.getLogger("exo.browser")

WEBKIT_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000  # 1601 -> 1970

# History events must be attributable to the browser process so the app
# blocklist applies to them like it does to live window/clipboard capture.
BROWSER_APPS = {"chrome": "chrome", "edge": "msedge", "chromium": "chromium",
                "firefox": "firefox"}

# search engines whose query strings become first-class `search` events
SEARCH_PARAMS = {
    "google.": "q", "bing.com": "q", "duckduckgo.com": "q",
    "search.brave.com": "q", "youtube.com": "search_query",
    "scholar.google.": "q", "startpage.com": "query",
}


def extract_search_query(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.casefold()
        for frag, param in SEARCH_PARAMS.items():
            if frag in host:
                q = parse_qs(parsed.query).get(param, [""])[0].strip()
                return q[:500]
    except (ValueError, KeyError):
        pass
    return ""


def _chromium_profiles() -> list[tuple[str, Path]]:
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        roots = [
            ("chrome", local / "Google" / "Chrome" / "User Data"),
            ("edge", local / "Microsoft" / "Edge" / "User Data"),
        ]
    else:
        home = Path.home()
        roots = [
            ("chrome", home / ".config" / "google-chrome"),
            ("chromium", home / ".config" / "chromium"),
        ]
    out = []
    for name, root in roots:
        if not root.is_dir():
            continue
        for profile in root.iterdir():
            history = profile / "History"
            if history.is_file():
                out.append((name, history))
    return out


def _firefox_profiles() -> list[tuple[str, Path]]:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"
    else:
        root = Path.home() / ".mozilla" / "firefox"
    if not root.is_dir():
        return []
    return [
        ("firefox", places)
        for profile in root.iterdir()
        if (places := profile / "places.sqlite").is_file()
    ]


def _read_new_entries(history_file: Path, browser: str, since_unix_us: int):
    """Yield (unix_us, url, title) newer than the checkpoint."""
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "history.db"
        shutil.copy2(history_file, snap)
        conn = sqlite3.connect(snap)
        try:
            if browser == "firefox":
                rows = conn.execute(
                    "SELECT last_visit_date, url, title FROM moz_places"
                    " WHERE last_visit_date IS NOT NULL AND last_visit_date > ?"
                    " ORDER BY last_visit_date LIMIT 2000",
                    (since_unix_us,),
                ).fetchall()
                yield from ((r[0], r[1], r[2] or "") for r in rows)
            else:
                rows = conn.execute(
                    "SELECT last_visit_time, url, title FROM urls"
                    " WHERE last_visit_time > ? ORDER BY last_visit_time LIMIT 2000",
                    (since_unix_us + WEBKIT_EPOCH_OFFSET_US,),
                ).fetchall()
                yield from ((r[0] - WEBKIT_EPOCH_OFFSET_US, r[1], r[2] or "") for r in rows)
        finally:
            conn.close()


def import_history(conn: sqlite3.Connection, pipeline: Pipeline,
                   floor_us: int = 0) -> int:
    """Import new history entries from every detected profile into the local
    trail db. `floor_us` comes from the synced db's work-mode control: visits
    older than it happened during a work-mode period and are gone forever,
    not deferred. Returns events submitted (pre-firewall)."""
    submitted = 0
    floor = floor_us
    # First-run backfill horizon: years-old history has no curriculum value
    # and would eat the per-profile row limit; 30 days covers 2x the engine's
    # trail window.
    backfill_floor = (db.now_ms() - 30 * 86400_000) * 1000
    for browser, history_file in _chromium_profiles() + _firefox_profiles():
        key = "browser_ckpt_" + hashlib.sha1(str(history_file).encode()).hexdigest()[:16]
        ckpt = int(db.get_control(conn, key, "0") or 0)
        since = max(ckpt, floor, backfill_floor if ckpt == 0 else 0)
        newest = since
        try:
            for unix_us, url, title in _read_new_entries(history_file, browser, since):
                app = BROWSER_APPS.get(browser, browser)
                visit_ms = unix_us // 1000
                pipeline.submit(
                    Event(
                        source="browser",
                        content=url,
                        kind=browser,
                        app=app,
                        ts=visit_ms,
                        extra={"title": title},
                    )
                )
                submitted += 1
                query = extract_search_query(url)
                if query:
                    pipeline.submit(
                        Event(
                            source="search",
                            content=query,
                            kind=browser,
                            app=app,
                            ts=visit_ms,
                            extra={"url": url[:300]},
                        )
                    )
                    submitted += 1
                newest = max(newest, unix_us)
        except (sqlite3.Error, OSError) as e:
            log.warning("history import failed for %s: %s", history_file, e)
            continue
        if newest > ckpt:
            db.set_control(conn, key, str(newest))
    return submitted
