"""One-time full browser-history backfill (Plane B, local.db only).

The regular import starts 30 days back and pages 2000 rows per run; this
walks the entire available history database (~90 days for Chrome/Edge) in
batches, deduping against rows already imported. The work-mode floor is
deliberately NOT applied here: the floor exists to keep work-period browsing
out, but it also blocks everything BEFORE the last toggle — for a backfill
explicitly meant to recover old personal history that's wrong. The caller
reports the imported window so the user can audit.
"""

from __future__ import annotations

import logging
import sqlite3

from ...config import Config
from ...state import db
from ..filter import Firewall
from ..pipeline import Pipeline
from .browser import (
    BROWSER_APPS,
    _chromium_profiles,
    _firefox_profiles,
    _read_new_entries,
    extract_search_query,
)
from ..filter import Event

log = logging.getLogger("exo.backfill")

BATCH = 2000


def backfill_history(cfg: Config) -> str:
    conn = db.connect(cfg.db_path)
    local = db.local_connect(cfg)
    try:
        firewall = Firewall(cfg.data["firewall"])
        pipeline = Pipeline(local, firewall, cfg.device_id, inline_flush=True)
        pipeline.set_work_mode(db.get_control(conn, "work_mode", "off") == "on")
        # dedupe against everything already in the trail
        existing = {
            (r["ts"], r["content"]) for r in local.execute(
                "SELECT ts, content FROM life_stream WHERE source IN ('browser','search')")
        }
        imported, oldest, newest = 0, None, None
        for browser, history_file in _chromium_profiles() + _firefox_profiles():
            app = BROWSER_APPS.get(browser, browser)
            since_us = 0
            while True:
                rows = list(_read_new_entries(history_file, browser, since_us))
                if not rows:
                    break
                for unix_us, url, title in rows:
                    since_us = max(since_us, unix_us)
                    visit_ms = unix_us // 1000
                    oldest = min(oldest or visit_ms, visit_ms)
                    newest = max(newest or visit_ms, visit_ms)
                    if (visit_ms, url) in existing:
                        continue
                    existing.add((visit_ms, url))
                    pipeline.submit(Event(source="browser", content=url, kind=browser,
                                          app=app, ts=visit_ms, extra={"title": title}))
                    imported += 1
                    q = extract_search_query(url)
                    if q and (visit_ms, q) not in existing:
                        existing.add((visit_ms, q))
                        pipeline.submit(Event(source="search", content=q, kind=browser,
                                              app=app, ts=visit_ms,
                                              extra={"url": url[:300]}))
                        imported += 1
                if len(rows) < BATCH:
                    break
        pipeline.flush()
        import datetime as dt
        span = ""
        if oldest and newest:
            span = (f" spanning {dt.datetime.fromtimestamp(oldest/1000):%b %d}"
                    f" → {dt.datetime.fromtimestamp(newest/1000):%b %d}")
        return (f"backfill: {pipeline.written} new events written"
                f" ({pipeline.dropped} dropped by firewall){span}")
    finally:
        local.close()
        conn.close()
