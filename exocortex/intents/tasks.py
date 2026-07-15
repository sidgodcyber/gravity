"""Task store + the /tasks /due /done /drop views."""

from __future__ import annotations

import datetime as dt
import sqlite3

from ..state.db import now_ms
from ..state.uids import resolve, uid_for


def create_task(conn: sqlite3.Connection, text: str, *, entity: str | None = None,
                due_ts: int | None = None, priority: int = 0,
                parent_id: int | None = None, inbox_id: int | None = None) -> int:
    ts = now_ms()
    cur = conn.execute(
        "INSERT INTO tasks (text, parent_id, entity, status, due_ts, priority,"
        " inbox_id, created_ts, updated_ts) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)",
        (text.strip()[:300], parent_id, entity, due_ts, priority, inbox_id, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def set_status(conn: sqlite3.Connection, task_id: int, status: str) -> str | None:
    row = conn.execute("SELECT text FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE tasks SET status = ?, updated_ts = ? WHERE id = ?",
                 (status, now_ms(), task_id))
    # closing a parent closes its still-open subtasks
    if status in ("done", "dropped"):
        conn.execute(
            "UPDATE tasks SET status = ?, updated_ts = ? WHERE parent_id = ?"
            " AND status = 'open'", (status, now_ms(), task_id))
    conn.commit()
    return row["text"]


def set_status_by_uid(conn: sqlite3.Connection, uid: int, status: str) -> str | None:
    """Close a task by its USER-FACING id. Returns None if the uid doesn't
    exist or isn't a task (never touches a different table's row)."""
    ref = resolve(conn, uid)
    if ref is None or ref[0] != "tasks":
        return None
    return set_status(conn, ref[1], status)


def _fmt_due(due_ts: int | None, now: dt.datetime) -> str:
    if not due_ts:
        return ""
    d = dt.datetime.fromtimestamp(due_ts / 1000)
    days = (d.date() - now.date()).days
    when = ("overdue" if days < 0 else "today" if days == 0 else
            "tomorrow" if days == 1 else d.strftime("%a %b %d"))
    return f" ⚠️{when}" if days < 0 else f" (due {when})"


def open_tasks(conn: sqlite3.Connection) -> str:
    now = dt.datetime.now()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'open' ORDER BY entity IS NULL, entity,"
        " due_ts IS NULL, due_ts, priority DESC, id"
    ).fetchall()
    if not rows:
        return "No open tasks. 🎉"
    by_entity: dict[str, list] = {}
    for r in rows:
        by_entity.setdefault(r["entity"] or "(no client/project)", []).append(r)
    lines = ["📋 Open tasks:"]
    shown = []
    for entity, items in by_entity.items():
        lines.append(f"\n{entity}:")
        parents = [r for r in items if r["parent_id"] is None]
        for p in parents:
            star = " ‼️" if p["priority"] == 2 else ""
            uid = uid_for(conn, 'tasks', p['id'])
            shown.append({"uid": uid, "text": p["text"][:80]})
            lines.append(f"  #{uid} {p['text']}{star}{_fmt_due(p['due_ts'], now)}")
            for c in items:
                if c["parent_id"] == p["id"]:
                    cuid = uid_for(conn, 'tasks', c['id'])
                    shown.append({"uid": cuid, "text": c["text"][:80]})
                    lines.append(f"     └ #{cuid} {c['text']}{_fmt_due(c['due_ts'], now)}")
        # orphaned subtasks whose parent is in another group are rare; show flat
        for r in items:
            if r["parent_id"] is not None and not any(p["id"] == r["parent_id"] for p in parents):
                uid = uid_for(conn, 'tasks', r['id'])
                shown.append({"uid": uid, "text": r["text"][:80]})
                lines.append(f"  #{uid} {r['text']}{_fmt_due(r['due_ts'], now)}")
    # "everything except #N" / "that last one" must refer to THIS listing
    from .memory_edit import _remember_shown
    _remember_shown(conn, shown)
    return "\n".join(lines)


def due_soon(conn: sqlite3.Connection, within_days: int = 3) -> str:
    now = dt.datetime.now()
    horizon = int((now + dt.timedelta(days=within_days)).timestamp() * 1000)
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'open' AND due_ts IS NOT NULL"
        " AND due_ts <= ? ORDER BY due_ts", (horizon,),
    ).fetchall()
    if not rows:
        return "Nothing due in the next few days."
    lines = ["⏰ Due soon:"]
    shown = []
    overdue = [r for r in rows if r["due_ts"] < now_ms()]
    upcoming = [r for r in rows if r["due_ts"] >= now_ms()]
    for r in overdue + upcoming:
        ent = f" [{r['entity']}]" if r["entity"] else ""
        uid = uid_for(conn, 'tasks', r['id'])
        shown.append({"uid": uid, "text": r["text"][:80]})
        lines.append(f"  #{uid} {r['text']}{ent}{_fmt_due(r['due_ts'], now)}")
    from .memory_edit import _remember_shown
    _remember_shown(conn, shown)
    return "\n".join(lines)


def brief_tasks(conn: sqlite3.Connection) -> str:
    """Compact open/overdue block for the morning brief."""
    now = dt.datetime.now()
    overdue = conn.execute(
        "SELECT count(*) FROM tasks WHERE status='open' AND due_ts IS NOT NULL"
        " AND due_ts < ?", (now_ms(),)).fetchone()[0]
    soon = conn.execute(
        "SELECT * FROM tasks WHERE status='open' AND due_ts IS NOT NULL"
        " AND due_ts <= ? ORDER BY due_ts LIMIT 5",
        (int((now + dt.timedelta(days=3)).timestamp() * 1000),)).fetchall()
    if not soon and not overdue:
        return ""
    lines = ["✅ Tasks:"]
    for r in soon:
        ent = f" [{r['entity']}]" if r["entity"] else ""
        lines.append(f"  #{uid_for(conn, 'tasks', r['id'])} {r['text']}{ent}{_fmt_due(r['due_ts'], now)}")
    if overdue > len(soon):
        lines.append(f"  ({overdue} overdue in total — /due for all)")
    return "\n".join(lines)
