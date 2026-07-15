"""Editable memory: /recent, /edit, /delete, and natural-language corrections.

The load-bearing guarantees (tested adversarially):
- Nothing is deleted without the exact records being SHOWN first and the user
  confirming. A natural-language "delete X" resolves to a concrete list and
  returns it for confirmation — it never deletes on its own.
- Deleting an inbox message HEALS downstream: every artifact it spawned
  (tasks, reflections, crm_notes, and any NEW skill-graph node created solely
  from it) is removed too, and affected inference (today's cached brief) is
  invalidated. Corrections fix the source AND its descendants, never just the
  surface row.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from ..config import Config
from ..state import db
from ..state.db import now_ms
from ..state.uids import release, resolve, uid_for

INTENT_ICON = {"task": "✅", "weekly_update": "📦", "plan": "💡",
               "client_note": "📇", "reflection": "💭", "question": "❓",
               "correction": "✏️"}


def recent(conn: sqlite3.Connection, n: int = 10) -> str:
    """Last records across everything user-visible, with uid + provenance."""
    from .provenance import short_label
    items: list[tuple[int, str, int, str]] = []   # (ts, tbl, row_id, text)
    for r in conn.execute(
        "SELECT id, ts, raw_text FROM inbox ORDER BY id DESC LIMIT ?",
        (max(1, min(n, 30)),)):
        items.append((r["ts"], "inbox", r["id"], r["raw_text"]))
    for r in conn.execute(
        "SELECT id, ts, raw_text FROM reflections WHERE inbox_id IS NULL"
        " ORDER BY id DESC LIMIT ?", (max(1, min(n, 30)),)):
        items.append((r["ts"], "reflections", r["id"], r["raw_text"]))
    items.sort(reverse=True)
    items = items[: max(1, min(n, 30))]
    if not items:
        return "Nothing stored yet."
    lines = [f"🗂 Last {len(items)} records (newest first):"]
    shown = []
    for ts, tbl, row_id, text in items:
        uid = uid_for(conn, tbl, row_id)
        d = dt.datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")
        label = short_label(conn, tbl, row_id)
        lines.append(f"#{uid} [{d}] {text[:80]}\n    — {label}")
        shown.append({"uid": uid, "text": text[:80]})
    lines.append("\n/edit <id> <text> · /delete <id> · /source <id> for origin.")
    _remember_shown(conn, shown)
    return "\n".join(lines)


def _remember_shown(conn: sqlite3.Connection, shown: list[dict]) -> None:
    """Keep the last listing the user saw — the context for 'that last one' /
    'everything except #N' natural-language references."""
    db.set_control(conn, "last_shown", json.dumps(shown[:30]))


def last_shown(conn: sqlite3.Connection) -> list[dict]:
    try:
        return json.loads(db.get_control(conn, "last_shown", "[]"))
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- healing

def _delete_artifacts(cfg: Config, conn: sqlite3.Connection, artifacts: list[dict]) -> list[str]:
    """Remove everything an inbox message spawned. A NEW skill-graph node is
    removed only if this was its sole evidence; a node with other evidence
    keeps living but loses this citation."""
    removed = []
    reflection_ids = [a["id"] for a in artifacts if a["table"] == "reflections"]
    # process auto-created clients LAST — only after their notes/tasks are gone
    # can we tell whether the client is now orphaned and safe to remove
    artifacts = sorted(artifacts, key=lambda a: a["table"] == "crm")
    for a in artifacts:
        t, i = a["table"], a["id"]
        if t == "tasks":
            subs = [r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE parent_id = ?", (i,))]
            conn.execute("DELETE FROM tasks WHERE id = ? OR parent_id = ?", (i, i))
            for tid in [i, *subs]:
                release(conn, "tasks", tid)
            removed.append("a task" + (f" (+{len(subs)} sub-tasks)" if subs else ""))
        elif t == "reflections":
            conn.execute("DELETE FROM reflections WHERE id = ?", (i,))
            release(conn, "reflections", i)
            removed.append("a reflection")
        elif t == "crm_notes":
            conn.execute("DELETE FROM crm_notes WHERE id = ?", (i,))
            release(conn, "crm_notes", i)
            removed.append("a client note")
        elif t == "crm" and a.get("created"):
            # only remove an auto-created client if it now has no notes/tasks
            has_notes = conn.execute("SELECT 1 FROM crm_notes WHERE crm_id = ? LIMIT 1", (i,)).fetchone()
            name = conn.execute("SELECT name FROM crm WHERE id = ?", (i,)).fetchone()
            if name:
                has_tasks = conn.execute(
                    "SELECT 1 FROM tasks WHERE entity = ? LIMIT 1", (name["name"],)).fetchone()
                if not has_notes and not has_tasks:
                    conn.execute("DELETE FROM crm WHERE id = ?", (i,))
                    removed.append(f"client '{name['name']}'")
    conn.commit()
    # heal the skill graph: reflections that were extracted may have spawned
    # nodes/edges whose only evidence was this reflection
    if reflection_ids:
        removed += _prune_graph_evidence(conn, reflection_ids)
    _invalidate_today_brief(conn)
    return removed


def _prune_graph_evidence(conn: sqlite3.Connection, reflection_ids: list[int]) -> list[str]:
    removed = []
    ids = set(reflection_ids)
    edges = conn.execute("SELECT id, src, dst, evidence FROM skill_edges").fetchall()
    touched_nodes: set[int] = set()
    for e in edges:
        try:
            ev = json.loads(e["evidence"] or "[]")
        except json.JSONDecodeError:
            continue
        kept = [x for x in ev
                if not (isinstance(x, dict) and x.get("table") == "reflections"
                        and x.get("id") in ids)]
        if len(kept) == len(ev):
            continue
        touched_nodes.update([e["src"], e["dst"]])
        if kept:
            conn.execute("UPDATE skill_edges SET evidence = ? WHERE id = ?",
                         (json.dumps(kept), e["id"]))
        else:
            # name the link, never its raw table id — #N is uid-only territory
            nm = conn.execute(
                "SELECT (SELECT name FROM skill_nodes WHERE id = ?) AS a,"
                " (SELECT name FROM skill_nodes WHERE id = ?) AS b",
                (e["src"], e["dst"])).fetchone()
            conn.execute("DELETE FROM skill_edges WHERE id = ?", (e["id"],))
            removed.append(f"graph link {nm['a']}–{nm['b']}"
                           if nm["a"] and nm["b"] else "a graph link")
    # drop now-orphaned nodes (no edges either direction) that were concept/
    # skill nodes — leaves domains, which are cheap to keep
    for nid in touched_nodes:
        still = conn.execute(
            "SELECT 1 FROM skill_edges WHERE src = ? OR dst = ? LIMIT 1", (nid, nid)
        ).fetchone()
        if not still:
            row = conn.execute("SELECT name, type FROM skill_nodes WHERE id = ?", (nid,)).fetchone()
            if row and row["type"] in ("skill", "concept"):
                conn.execute("DELETE FROM skill_nodes WHERE id = ?", (nid,))
                removed.append(f"graph node '{row['name']}'")
    conn.commit()
    return removed


def _invalidate_today_brief(conn: sqlite3.Connection) -> None:
    midnight = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    conn.execute("DELETE FROM briefs WHERE ts >= ? AND delivered = 0",
                 (int(midnight.timestamp() * 1000),))
    conn.commit()


def delete_record(cfg: Config, conn: sqlite3.Connection, inbox_id: int) -> tuple[bool, str]:
    row = conn.execute("SELECT raw_text, artifacts FROM inbox WHERE id = ?",
                       (inbox_id,)).fetchone()
    if row is None:
        return False, f"No record #{inbox_id}."
    try:
        artifacts = json.loads(row["artifacts"] or "[]")
    except json.JSONDecodeError:
        artifacts = []
    uid = uid_for(conn, "inbox", inbox_id)
    removed = _delete_artifacts(cfg, conn, artifacts)
    conn.execute("DELETE FROM inbox WHERE id = ?", (inbox_id,))
    release(conn, "inbox", inbox_id)
    conn.commit()
    tail = (" (also removed: " + ", ".join(removed) + ")") if removed else ""
    return True, f"🗑 Deleted #{uid}: {row['raw_text'][:60]}{tail}"


def delete_any(cfg: Config, conn: sqlite3.Connection, table: str, rec_id: int) -> tuple[bool, str]:
    """Delete a record identified by (table, id), healing downstream. Handles
    both Phase-2.5 inbox records (via their artifacts) and legacy records
    (reflections/crm_notes that predate the inbox layer)."""
    if table == "inbox":
        return delete_record(cfg, conn, rec_id)
    if table == "reflections":
        row = conn.execute("SELECT raw_text FROM reflections WHERE id = ?", (rec_id,)).fetchone()
        if row is None:
            return False, f"No such reflection."
        uid = uid_for(conn, "reflections", rec_id)
        conn.execute("DELETE FROM reflections WHERE id = ?", (rec_id,))
        release(conn, "reflections", rec_id)
        conn.commit()
        healed = _prune_graph_evidence(conn, [rec_id])
        _invalidate_today_brief(conn)
        tail = (" (also removed: " + ", ".join(healed) + ")") if healed else ""
        return True, f"🗑 Deleted reflection #{uid}: {row['raw_text'][:60]}{tail}"
    if table == "crm_notes":
        crm_id = conn.execute("SELECT crm_id FROM crm_notes WHERE id = ?", (rec_id,)).fetchone()
        if crm_id is None:
            return False, "No such client note."
        note_uid = uid_for(conn, "crm_notes", rec_id)
        conn.execute("DELETE FROM crm_notes WHERE id = ?", (rec_id,))
        release(conn, "crm_notes", rec_id)
        conn.commit()
        # heal an orphaned client (no remaining notes AND no tasks) for parity
        # with the inbox path
        if crm_id:
            cid = crm_id["crm_id"]
            name = conn.execute("SELECT name FROM crm WHERE id = ?", (cid,)).fetchone()
            has_notes = conn.execute("SELECT 1 FROM crm_notes WHERE crm_id = ? LIMIT 1", (cid,)).fetchone()
            has_tasks = name and conn.execute(
                "SELECT 1 FROM tasks WHERE entity = ? LIMIT 1", (name["name"],)).fetchone()
            if name and not has_notes and not has_tasks:
                conn.execute("DELETE FROM crm WHERE id = ?", (cid,))
                conn.commit()
                return True, f"🗑 Deleted client note #{note_uid} (and orphaned client '{name['name']}')."
        return True, f"🗑 Deleted client note #{note_uid}."
    if table == "tasks":
        uid = uid_for(conn, "tasks", rec_id)
        row = conn.execute("SELECT text FROM tasks WHERE id = ?", (rec_id,)).fetchone()
        if row is None:
            return False, "No such task."
        subs = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE parent_id = ?", (rec_id,))]
        conn.execute("DELETE FROM tasks WHERE id = ? OR parent_id = ?", (rec_id, rec_id))
        for tid in [rec_id, *subs]:
            release(conn, "tasks", tid)
        conn.commit()
        sub = f" (+{len(subs)} sub-tasks)" if subs else ""
        return True, f"🗑 Deleted task #{uid}: {row['text'][:60]}{sub}"
    return False, f"Don't know how to delete from {table}."


def delete_by_uid(cfg: Config, conn: sqlite3.Connection, uid: int) -> tuple[bool, str]:
    """THE user-facing delete: the uid retrieves exactly the row it was shown
    for. Fixes the #9 bug where a displayed task id resolved a different
    table's row."""
    ref = resolve(conn, uid)
    if ref is None:
        return False, f"No record #{uid} (it may already be deleted)."
    return delete_any(cfg, conn, ref[0], ref[1])


def replace_in(cfg: Config, conn: sqlite3.Connection, table: str, rec_id: int,
               find: str, replace_with: str) -> tuple[bool, str]:
    """Case-insensitive text substitution on any record. inbox records
    re-classify (edit_record); legacy rows get an in-place rewrite (and
    reflections re-extract)."""
    import re
    pat = re.compile(re.escape(find), re.IGNORECASE)
    uid = uid_for(conn, table, rec_id)
    if table == "inbox":
        row = conn.execute("SELECT raw_text FROM inbox WHERE id = ?", (rec_id,)).fetchone()
        if row is None:
            return False, f"No record #{rec_id}."
        new = pat.sub(replace_with, row["raw_text"])
        if new == row["raw_text"]:
            return False, f"#{uid}: '{find}' not found (nothing changed)."
        return edit_record(cfg, conn, rec_id, new)
    col = {"reflections": "raw_text", "crm_notes": "note", "tasks": "text"}.get(table)
    if col is None:
        return False, f"Can't replace in {table}."
    row = conn.execute(f"SELECT {col} AS v FROM {table} WHERE id = ?", (rec_id,)).fetchone()
    if row is None:
        return False, f"No such record (#{uid})."
    new = pat.sub(replace_with, row["v"])
    if new == row["v"]:
        return False, f"#{uid}: '{find}' not found (nothing changed)."
    conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (new, rec_id))
    if table == "reflections":
        # content changed → old extraction is stale; heal graph and re-extract
        _prune_graph_evidence(conn, [rec_id])
        conn.execute("UPDATE reflections SET extraction_status = 'pending' WHERE id = ?", (rec_id,))
    conn.commit()
    _invalidate_today_brief(conn)
    return True, f"✏️ Updated #{uid}: '{find}' → '{replace_with}'."


def edit_record(cfg: Config, conn: sqlite3.Connection, inbox_id: int,
                new_text: str) -> tuple[bool, str]:
    """Replace content: heal the old artifacts, then re-run the classifier and
    re-dispatch the new text under the same inbox id lineage."""
    row = conn.execute("SELECT artifacts FROM inbox WHERE id = ?", (inbox_id,)).fetchone()
    if row is None:
        return False, f"No record #{inbox_id}."
    try:
        artifacts = json.loads(row["artifacts"] or "[]")
    except json.JSONDecodeError:
        artifacts = []
    _delete_artifacts(cfg, conn, artifacts)
    conn.execute("DELETE FROM inbox WHERE id = ?", (inbox_id,))
    # CRITICAL: base tables reuse rowids (no AUTOINCREMENT), so a uid left
    # pointing at a deleted row WILL rebind to a future record unless released
    release(conn, "inbox", inbox_id)
    conn.commit()
    from .classify import classify
    from .dispatch import dispatch
    c = classify(cfg, conn, new_text)
    result = dispatch(cfg, conn, c, source="edit")
    return True, "✏️ Updated. " + result["reply"]


# ---------------------------------------------------------------- NL correction

def resolve_correction(conn: sqlite3.Connection, correction: dict) -> list[dict]:
    """Find records a natural-language correction refers to, across the inbox
    AND legacy tables (reflections, crm_notes) so pre-Phase-2.5 rows are
    reachable too. Returns dicts {table, id, ts, text} — the caller SHOWS them
    and only deletes on confirmation. Never deletes here."""
    term = (correction.get("find") or correction.get("target") or "").strip()
    if not term or len(term) < 2:
        return []
    like = f"%{term}%"
    out: list[dict] = []
    for r in conn.execute(
        "SELECT id, ts, raw_text AS t FROM inbox WHERE raw_text LIKE ? COLLATE NOCASE"
        " ORDER BY id DESC LIMIT 10", (like,)):
        out.append({"table": "inbox", "id": r["id"], "ts": r["ts"], "text": r["t"]})
    # legacy reflections that never went through the inbox
    for r in conn.execute(
        "SELECT id, ts, raw_text AS t FROM reflections WHERE raw_text LIKE ? COLLATE NOCASE"
        " AND inbox_id IS NULL ORDER BY id DESC LIMIT 10", (like,)):
        out.append({"table": "reflections", "id": r["id"], "ts": r["ts"], "text": r["t"]})
    for r in conn.execute(
        "SELECT id, ts, note AS t FROM crm_notes WHERE note LIKE ? COLLATE NOCASE"
        " AND inbox_id IS NULL ORDER BY id DESC LIMIT 10", (like,)):
        out.append({"table": "crm_notes", "id": r["id"], "ts": r["ts"], "text": r["t"]})
    return out[:12]


def format_matches(conn: sqlite3.Connection, rows: list[dict]) -> str:
    lines = []
    for r in rows:
        uid = uid_for(conn, r["table"], r["id"])
        d = dt.datetime.fromtimestamp(r["ts"] / 1000).strftime("%m-%d %H:%M")
        label = short_or_blank(conn, r["table"], r["id"])
        lines.append(f"#{uid} [{d}] {r['text'][:80]}{label}")
    return "\n".join(lines)


def short_or_blank(conn: sqlite3.Connection, tbl: str, row_id: int) -> str:
    from .provenance import short_label
    try:
        return " — " + short_label(conn, tbl, row_id)
    except Exception:
        return ""
