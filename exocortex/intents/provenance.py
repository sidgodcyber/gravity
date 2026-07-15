"""Provenance: where every stored record came from, in plain words.

Three origins, always stated explicitly wherever a record is displayed:
- "you said"  — the user typed it (Telegram/CLI) or spoke it (voice note)
- "observed"  — the daemon captured it (browser, window, search, AI prompt)
- "inferred"  — the system derived it (concept engine, extraction)
Inferred items must never read as the user's own statements.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from ..config import Config
from ..state import db
from ..state.uids import resolve

_SAID = {
    "telegram": "you said (typed on Telegram)",
    "voice": "you said (voice note, locally transcribed)",
    "cli": "you typed (CLI)",
    "edit": "you said (after your /edit)",
    "test": "system test message (not you)",
}


def short_label(conn: sqlite3.Connection, tbl: str, row_id: int) -> str:
    """One-line origin label for record listings."""
    if tbl == "inbox":
        r = conn.execute("SELECT source, intent FROM inbox WHERE id = ?", (row_id,)).fetchone()
        if r is None:
            return "unknown"
        base = _SAID.get(r["source"], f"you said ({r['source']})")
        if r["intent"] == "unclassified":
            base += " · saved unclassified"
        return base
    if tbl == "tasks":
        r = conn.execute("SELECT inbox_id FROM tasks WHERE id = ?", (row_id,)).fetchone()
        if r and r["inbox_id"]:
            src = conn.execute("SELECT source FROM inbox WHERE id = ?", (r["inbox_id"],)).fetchone()
            if src:
                return "task from something " + _SAID.get(src["source"], "you said")
        return "task (manually created)"
    if tbl == "reflections":
        r = conn.execute("SELECT inbox_id, insight_type, device FROM reflections"
                         " WHERE id = ?", (row_id,)).fetchone()
        if r is None:
            return "unknown"
        if r["inbox_id"]:
            src = conn.execute("SELECT source FROM inbox WHERE id = ?", (r["inbox_id"],)).fetchone()
            if src:
                return _SAID.get(src["source"], "you said")
        # direct logs carry the machine they were typed on ("on desktop")
        dev = f", on {r['device']}" if r["device"] else ""
        return f"you said (direct log{dev})"
    if tbl == "crm_notes":
        r = conn.execute("SELECT inbox_id FROM crm_notes WHERE id = ?", (row_id,)).fetchone()
        if r and r["inbox_id"]:
            src = conn.execute("SELECT source FROM inbox WHERE id = ?", (r["inbox_id"],)).fetchone()
            if src:
                return "client note from something " + _SAID.get(src["source"], "you said")
        return "client note (direct entry)"
    return "unknown"


def describe(cfg: Config, conn: sqlite3.Connection, uid: int) -> str:
    """Full /source answer: origin, timestamp, and source pointer."""
    ref = resolve(conn, uid)
    if ref is None:
        return f"No record #{uid}."
    tbl, row_id = ref
    ts = None
    text = ""
    if tbl == "inbox":
        r = conn.execute("SELECT ts, raw_text, source, intent, extracted FROM inbox"
                         " WHERE id = ?", (row_id,)).fetchone()
        if r is None:
            return f"#{uid} was deleted."
        ts, text = r["ts"], r["raw_text"]
        origin = short_label(conn, tbl, row_id)
        extra = ""
        if r["intent"] not in ("unclassified",):
            extra = f"\nClassified as: {r['intent']}"
    elif tbl == "tasks":
        r = conn.execute("SELECT ts, raw_text FROM inbox WHERE id ="
                         " (SELECT inbox_id FROM tasks WHERE id = ?)", (row_id,)).fetchone()
        t = conn.execute("SELECT text, created_ts FROM tasks WHERE id = ?", (row_id,)).fetchone()
        if t is None:
            return f"#{uid} was deleted."
        ts, text = t["created_ts"], t["text"]
        origin = short_label(conn, tbl, row_id)
        extra = f"\nCreated from your message: “{r['raw_text'][:120]}”" if r else ""
    elif tbl == "reflections":
        r = conn.execute("SELECT ts, raw_text, device FROM reflections WHERE id = ?",
                         (row_id,)).fetchone()
        if r is None:
            return f"#{uid} was deleted."
        ts, text = r["ts"], r["raw_text"]
        origin = short_label(conn, tbl, row_id)
        extra = f"\nDevice: {r['device']}" if r["device"] else ""
    elif tbl == "crm_notes":
        r = conn.execute("SELECT ts, note FROM crm_notes WHERE id = ?", (row_id,)).fetchone()
        if r is None:
            return f"#{uid} was deleted."
        ts, text = r["ts"], r["note"]
        origin = short_label(conn, tbl, row_id)
        extra = ""
    else:
        return f"#{uid}: unknown record type."
    when = dt.datetime.fromtimestamp(ts / 1000).strftime("%A %b %d, %H:%M")
    return (f"#{uid} “{text[:100]}”\nOrigin: {origin}\nWhen: {when}{extra}")


def trail_source(local_conn: sqlite3.Connection, life_stream_id: int) -> str:
    """Provenance line for a Plane B trail row (observed data)."""
    r = local_conn.execute(
        "SELECT ts, source, kind, content, extra, device FROM life_stream WHERE id = ?",
        (life_stream_id,)).fetchone()
    if r is None:
        return "unknown trail row"
    when = dt.datetime.fromtimestamp(r["ts"] / 1000).strftime("%b %d %H:%M")
    if r["device"]:
        when += f" on {r['device']}"
    try:
        extra = json.loads(r["extra"] or "{}")
    except json.JSONDecodeError:
        extra = {}
    pointer = extra.get("url") or extra.get("title") or extra.get("project") or ""
    kinds = {"browser": "your browser history", "search": "a search you ran",
             "ai_prompt": "a prompt you sent to Claude Code",
             "window": "an active window title", "clipboard": "your clipboard"}
    return (f"observed — {kinds.get(r['source'], r['source'])}"
            f" ({r['kind']}) on {when}" + (f" · {pointer[:80]}" if pointer else ""))
