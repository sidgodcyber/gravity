"""Turn a classification into stored records, recording every artifact it
spawns so edits/deletes can heal downstream. Returns a human confirmation
line (the echo that lets the user catch a misclassification instantly)."""

from __future__ import annotations

import json
import sqlite3

from ..config import Config
from ..state.db import now_ms
from ..state.uids import uid_for
from . import tasks as taskstore


def _record_inbox(conn: sqlite3.Connection, c: dict, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO inbox (ts, raw_text, source, intent, tags, extracted, confidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_ms(), c["raw_text"], source, c["intent"], json.dumps(c["tags"]),
         json.dumps({k: c[k] for k in ("entity", "tasks", "ideas", "note",
                                       "reflection", "weekly")}),
         c["confidence"]),
    )
    conn.commit()
    return cur.lastrowid


def record_raw(conn: sqlite3.Connection, text: str, intent: str, source: str,
               extracted: dict | None = None) -> int:
    """Persist just the raw text — the 'nothing is lost' floor for when
    classification failed or is deferred. No artifacts spawned."""
    cur = conn.execute(
        "INSERT INTO inbox (ts, raw_text, source, intent, tags, extracted, confidence)"
        " VALUES (?, ?, ?, ?, '[]', ?, ?)",
        (now_ms(), text, source, intent,
         json.dumps(extracted or {}), (extracted or {}).get("confidence")),
    )
    conn.commit()
    return cur.lastrowid


def _set_artifacts(conn: sqlite3.Connection, inbox_id: int, artifacts: list[dict]) -> None:
    conn.execute("UPDATE inbox SET artifacts = ? WHERE id = ?",
                 (json.dumps(artifacts), inbox_id))
    conn.commit()


def _entity_name(ent) -> str | None:
    return ent["name"] if ent and ent.get("name") else None


def _ensure_crm(conn: sqlite3.Connection, name: str, artifacts: list[dict]) -> int:
    row = conn.execute("SELECT id FROM crm WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    ts = now_ms()
    cur = conn.execute(
        "INSERT INTO crm (name, kind, created_ts, updated_ts) VALUES (?, 'prospect', ?, ?)",
        (name, ts, ts))
    conn.commit()
    artifacts.append({"table": "crm", "id": cur.lastrowid, "created": True})
    return cur.lastrowid


def dispatch(cfg: Config, conn: sqlite3.Connection, c: dict,
             source: str = "telegram") -> dict:
    """Store per the classification. Returns
    {"reply": str, "inbox_id": int, "followup": str|None}."""
    inbox_id = _record_inbox(conn, c, source)
    artifacts: list[dict] = []
    reply_parts: list[str] = []
    followup = None
    # a brand-new entity is one the classifier couldn't match — flag it whether
    # it appears at message level OR on any task (confabulated task clients too)
    new_names = []
    if c["entity"] and not c["entity"].get("known"):
        new_names.append(c["entity"]["name"])
    for t in c["tasks"]:
        if t["entity"] and not t["entity"].get("known"):
            new_names.append(t["entity"]["name"])
    new_names = list(dict.fromkeys(new_names))

    # tasks can appear under any intent (multi-intent), so handle them first.
    # GROUNDING: a task presupposing a prior event we have no trace of is HELD
    # behind a question, never silently minted (the museum-director bug).
    made_task_ids = []
    held_tasks = []
    for t in c["tasks"]:
        ent = _entity_name(t["entity"]) or _entity_name(c["entity"])
        if t.get("presupposes") and not _presupposition_evidence(
                cfg, conn, t["presupposes"], exclude_inbox_id=inbox_id):
            held_tasks.append({**t, "resolved_entity": ent})
            continue
        pid = taskstore.create_task(
            conn, t["text"], entity=ent, due_ts=t["due_ts"],
            priority=t["priority"], inbox_id=inbox_id)
        artifacts.append({"table": "tasks", "id": pid, "created": True})
        made_task_ids.append(pid)
        for sub in t["subtasks"]:
            sid = taskstore.create_task(conn, sub, entity=ent, parent_id=pid,
                                        inbox_id=inbox_id)
            artifacts.append({"table": "tasks", "id": sid, "created": True})

    if (c["reflection"] or c["intent"] == "reflection") and c["intent"] != "weekly_update":
        from ..extract.reflections import add_reflection
        rid = add_reflection(conn, cfg, c["reflection"] or c["raw_text"],
                             extract_now=False)
        conn.execute("UPDATE reflections SET inbox_id = ? WHERE id = ?", (inbox_id, rid))
        conn.commit()
        artifacts.append({"table": "reflections", "id": rid, "created": True,
                          "extract": True})

    if c["weekly"] or c["intent"] == "weekly_update":
        from ..extract.reflections import add_reflection
        rid = add_reflection(conn, cfg, c["weekly"] or c["raw_text"],
                             extract_now=False)
        conn.execute("UPDATE reflections SET insight_type = 'weekly_outcome',"
                     " extraction_status = 'done', inbox_id = ? WHERE id = ?",
                     (inbox_id, rid))
        conn.commit()
        artifacts.append({"table": "reflections", "id": rid, "created": True})

    client_note_saved = False
    if c["intent"] == "client_note" or c["note"]:
        ent = _entity_name(c["entity"])
        if ent:
            crm_id = _ensure_crm(conn, ent, artifacts)
            cur = conn.execute(
                "INSERT INTO crm_notes (crm_id, ts, note, inbox_id) VALUES (?, ?, ?, ?)",
                (crm_id, now_ms(), c["note"] or c["raw_text"], inbox_id))
            conn.commit()
            artifacts.append({"table": "crm_notes", "id": cur.lastrowid, "created": True})
            client_note_saved = True

    if c["ideas"] or c["intent"] == "plan":
        # ideas are held, not actioned — stored as inbox extraction only, so
        # they show in /recent and can be promoted later. No separate row.
        pass

    _set_artifacts(conn, inbox_id, artifacts)

    # ---- build the echo (the misclassification tripwire) ----
    reply_parts.append(_echo(c, made_task_ids, conn, client_note_saved))
    inbox_uid = uid_for(conn, "inbox", inbox_id)
    for name in new_names:
        reply_parts.append(f"⚠️ '{name}' is new to me — I recorded it as-is."
                           f" Reply /delete {inbox_uid} if that's a mistranscription.")
    if c["ideas"]:
        followup = (f"Also noted {len(c['ideas'])} idea(s): "
                    + "; ".join(c["ideas"][:3])
                    + ". Want any as tasks? Reply /task <text>.")

    pending = None
    if held_tasks:
        events = "; ".join(dict.fromkeys(
            t["presupposes"] for t in held_tasks if t.get("presupposes")))
        reply_parts.append(
            f"⛔ I have no record of {events} — nothing in your logs, notes,"
            " or trail mentions it. Create the task anyway? (yes/no)")
        pending = {"type": "create_task", "tasks": held_tasks,
                   "inbox_id": inbox_id, "ts": now_ms()}

    return {"reply": "\n".join(p for p in reply_parts if p),
            "inbox_id": inbox_id, "followup": followup, "pending": pending}


def _presupposition_evidence(cfg: Config, conn: sqlite3.Connection, phrase: str,
                             exclude_inbox_id: int | None = None) -> bool:
    """Is there ANY trace of the presupposed event in the store? AND-joined
    distinctive terms so 'museum' alone (everywhere in this user's life)
    can't ground 'a call with the museum director'. The triggering message
    itself is excluded — a claim can't be its own evidence."""
    import re
    from ..retrieval.embeddings import STOPWORDS
    words = [w for w in re.findall(r"[A-Za-z0-9_]{3,}", phrase.lower())
             if w not in STOPWORDS][:4]
    if not words:
        return True  # nothing checkable — don't block
    fts = " AND ".join(f'"{w}"' for w in words)
    if conn.execute("SELECT 1 FROM reflections_fts WHERE reflections_fts MATCH ?"
                    " LIMIT 1", (fts,)).fetchone():
        return True
    # exclude the triggering message AND any verbatim repeat of it — sending
    # the same claim twice must not let copy #1 ground copy #2
    own_text = None
    if exclude_inbox_id:
        r = conn.execute("SELECT raw_text FROM inbox WHERE id = ?",
                         (exclude_inbox_id,)).fetchone()
        own_text = r["raw_text"] if r else None
    like_clauses = " AND ".join("raw_text LIKE ? COLLATE NOCASE" for _ in words)
    if conn.execute(
        f"SELECT 1 FROM inbox WHERE {like_clauses} AND id != ?"
        f" AND raw_text != COALESCE(?, '') LIMIT 1",
        (*(f"%{w}%" for w in words), exclude_inbox_id or -1, own_text),
    ).fetchone():
        return True
    try:
        from ..state import db as _db
        local = _db.local_connect(cfg)
        try:
            if local.execute("SELECT 1 FROM life_stream_fts WHERE life_stream_fts"
                             " MATCH ? LIMIT 1", (fts,)).fetchone():
                return True
        finally:
            local.close()
    except Exception:
        pass  # trail unavailable → judge on the synced store alone
    return False


def create_grounded_task(cfg: Config, conn: sqlite3.Connection, pending: dict) -> str:
    """User confirmed a held task: create it now, under the original message's
    provenance, and record the artifacts for healing."""
    inbox_id = pending["inbox_id"]
    made = []
    artifacts = []
    row = conn.execute("SELECT artifacts FROM inbox WHERE id = ?", (inbox_id,)).fetchone()
    if row is None:
        # source message deleted while the confirm was pending — creating an
        # unhealable orphan task would betray the provenance guarantees
        return ("The original message was deleted in the meantime — not"
                " creating the task. Use /task <text> if you still want it.")
    try:
        artifacts = json.loads(row["artifacts"] or "[]")
    except json.JSONDecodeError:
        artifacts = []
    for t in pending["tasks"]:
        pid = taskstore.create_task(
            conn, t["text"], entity=t.get("resolved_entity"),
            due_ts=t.get("due_ts"), priority=t.get("priority", 0),
            inbox_id=inbox_id)
        artifacts.append({"table": "tasks", "id": pid, "created": True})
        made.append(f"✅ Task #{uid_for(conn, 'tasks', pid)}: {t['text']}")
        for sub in t.get("subtasks") or []:
            sid = taskstore.create_task(conn, sub, entity=t.get("resolved_entity"),
                                        parent_id=pid, inbox_id=inbox_id)
            artifacts.append({"table": "tasks", "id": sid, "created": True})
    if row:
        _set_artifacts(conn, inbox_id, artifacts)
    return "\n".join(made) + "\n(noted: created without a prior record of the event)"


def _echo(c: dict, task_ids: list[int], conn: sqlite3.Connection,
         client_note_saved: bool = False) -> str:
    import datetime as dt
    intent = c["intent"]
    if task_ids:
        # show EVERY created parent task with its own due date — a fabricated
        # date on the 2nd+ task must be visible, not hidden behind task #1
        rows = conn.execute(
            f"SELECT id, text, due_ts, entity FROM tasks WHERE id IN"
            f" ({','.join('?' * len(task_ids))}) ORDER BY id", task_ids).fetchall()
        lines = []
        for r in rows:
            ent = ""
            if r["entity"] and r["entity"].casefold() not in r["text"].casefold():
                ent = f" for {r['entity']}"
            due = (" — due " + dt.datetime.fromtimestamp(r["due_ts"] / 1000).strftime("%a %b %d")
                   if r["due_ts"] else "")
            n_sub = conn.execute(
                "SELECT count(*) FROM tasks WHERE parent_id = ?", (r["id"],)).fetchone()[0]
            extra = f" (+{n_sub} sub-tasks)" if n_sub else ""
            lines.append(f"✅ Task #{uid_for(conn, 'tasks', r['id'])}: {r['text']}{ent}{due}{extra}")
        return "\n".join(lines)
    if intent == "client_note" and not client_note_saved:
        return ("📇 Noted, but I couldn't tell which client — it's in /recent."
                " Reply with the client name and I'll file it to their dossier.")
    labels = {"weekly_update": "📦 Weekly update logged",
              "client_note": "📇 Client note saved",
              "reflection": "💭 Reflection logged",
              "plan": "💡 Idea noted (not a task yet)",
              "question": "❓ Treating as a question"}
    return labels.get(intent, "📝 Logged") + (
        f" · {int(c['confidence'] * 100)}% sure" if c["confidence"] < 0.75 else "")
