"""Route one inbound message. Order of operations is the trust contract:

1. PENDING GATE (db-persisted, TTL'd) — if a confirmation is waiting, the
   reply resolves it BEFORE any classification. Confirmation traffic is never
   stored as content; a reply that is neither yes nor no cancels the pending
   and is processed as a fresh message.
2. Classification (raw text persisted BEFORE any fragile step).
3. Dispatch / command resolution / answer / guide.

Pending state lives in the synced db (control key), so it survives process
restarts and is shared by every instance — the live Bug-2 failure was
in-process state lost across restarts/duplicate pollers.
"""

from __future__ import annotations

import json
import sqlite3

from ..config import Config
from ..router.llm import RouterError
from ..state import db
from ..state.db import now_ms
from ..state.uids import resolve as uid_resolve
from ..state.uids import uid_for
from . import memory_edit
from .classify import CONFIDENCE_GATE, classify
from .dispatch import dispatch, record_raw

PENDING_TTL_MS = 10 * 60 * 1000
PENDING_KEY = "pending_confirm"
# Destructive confirms require a DELIBERATE word. "ok"/"okay" are everyday
# acknowledgements (people reply "ok" to a brief) — they confirm ONLY the
# harmless voice-transcript flow, never a delete/replace/create.
AFFIRM_STRICT = {"yes", "y", "yep", "yeah", "confirm", "do it"}
AFFIRM_CASUAL = AFFIRM_STRICT | {"ok", "okay"}
NEGATE = {"no", "n", "nope", "cancel", "nevermind", "never mind", "stop", "don't"}


# ------------------------------------------------------------ pending (db)

def get_pending(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_control(conn, PENDING_KEY, "")
    if not raw:
        return None
    try:
        p = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if now_ms() - p.get("ts", 0) > PENDING_TTL_MS:
        return None
    return p


def take_pending(conn: sqlite3.Connection) -> dict | None:
    """Atomically consume the pending confirmation: only ONE caller can get
    it, so two entry points (bot + CLI) can't both execute a confirm."""
    row = conn.execute(
        "DELETE FROM control WHERE key = ? RETURNING value", (PENDING_KEY,)
    ).fetchone()
    conn.commit()
    if row is None or not row["value"]:
        return None
    try:
        p = json.loads(row["value"])
    except json.JSONDecodeError:
        return None
    if now_ms() - p.get("ts", 0) > PENDING_TTL_MS:
        return None
    return p


def set_pending(conn: sqlite3.Connection, p: dict | None) -> None:
    db.set_control(conn, PENDING_KEY, json.dumps(p) if p else "")


def clear_pending(cfg: Config) -> None:
    """Slash commands call this: the user moved on, no stale confirm may fire."""
    conn = db.connect(cfg.db_path)
    try:
        set_pending(conn, None)
    finally:
        conn.close()


# ------------------------------------------------------------ entry point

def handle_incoming(cfg: Config, text: str, source: str = "telegram") -> dict:
    """THE entry for every free-text message. Checks the pending gate first."""
    conn = db.connect(cfg.db_path)
    try:
        pending = take_pending(conn)   # atomic: single consumer
    finally:
        conn.close()
    if pending is not None:
        return _resolve_pending(cfg, pending, text, source)
    return route(cfg, text, source)


def route(cfg: Config, text: str, source: str = "telegram") -> dict:
    text = text.strip()
    if not text:
        return {"reply": "Say something and I'll sort it.", "pending": None}
    conn = db.connect(cfg.db_path)
    try:
        try:
            c = classify(cfg, conn, text)
        except (RouterError, ValueError) as e:
            rid = record_raw(conn, text, "unclassified", source)
            return {"reply": f"⚠️ I saved your message (#{uid_for(conn, 'inbox', rid)})"
                             f" but couldn't classify it right now"
                             f" ({type(e).__name__}). It's in /recent.",
                    "pending": None}

        if c["intent"] == "meta":
            from ..guide import answer_meta
            return {"reply": answer_meta(text), "pending": None}

        if c["clarify"] and c["confidence"] < CONFIDENCE_GATE:
            rid = record_raw(conn, text, "unclassified", source, extracted=c)
            out = {"reply": f"❓ {c['clarify']}",
                   "pending": {"type": "clarify", "text": text, "inbox_id": rid,
                               "ts": now_ms()}}
            set_pending(conn, out["pending"])
            return out

        if c["intent"] == "question":
            from ..retrieval.ask import answer
            return {"reply": answer(cfg, text), "pending": None}

        if c["intent"] == "correction":
            out = _handle_correction(cfg, conn, c)
            set_pending(conn, out.get("pending"))
            return out

        if c["intent"] == "command" and c["command"]:
            out = _handle_command(cfg, conn, c)
            set_pending(conn, out.get("pending"))
            return out

        result = dispatch(cfg, conn, c, source=source)
        if result.get("pending"):
            set_pending(conn, result["pending"])
        return {"reply": result["reply"], "followup": result.get("followup"),
                "pending": result.get("pending")}
    finally:
        conn.close()


# ------------------------------------------------------------ corrections

def _handle_correction(cfg: Config, conn: sqlite3.Connection, c: dict) -> dict:
    from .resolve import resolve_targets
    corr = c["correction"] or {"action": "delete", "target": "", "find": None,
                               "replace_with": None}
    target_text = corr.get("find") or corr.get("target") or ""
    res = resolve_targets(cfg, conn, corr["action"], c["raw_text"], target_text)
    if res["question"]:
        # keep the thread alive: the user's answer merges with the original
        # command and re-resolves, instead of dead-ending as stored junk
        return {"reply": f"❓ {res['question']}",
                "pending": {"type": "clarify", "text": c["raw_text"],
                            "inbox_id": None, "ts": now_ms()}}
    if not res["targets"]:
        return {"reply": "I couldn't match that to any stored record."
                         " Nothing changed. /recent shows what's there.",
                "pending": None}
    refs, listing = _refs_and_listing(conn, res["targets"])
    if corr["action"] == "replace":
        if not corr.get("replace_with"):
            # a fix request with no replacement must NEVER degrade to a delete
            return {"reply": f"I found what you mean:\n{listing}\n\n"
                             "What should it say instead? (e.g. \"it's Roven"
                             " not Urban\" tells me both halves)", "pending": None}
        return {"reply": f"Replace in these {len(refs)} record(s)?\n{listing}\n\n"
                         f"'{corr['find']}' → '{corr['replace_with']}'. Reply yes to apply.",
                "pending": {"type": "replace", "refs": refs, "find": corr["find"],
                            "replace_with": corr["replace_with"], "ts": now_ms()}}
    note = f" ({res['note']})" if res["note"] else ""
    return {"reply": f"This deletes {len(refs)} record(s){note}:\n{listing}\n\n"
                     "Reply *yes* to confirm, or /delete <id> for just one.",
            "pending": {"type": "delete", "refs": refs, "ts": now_ms()}}


def _refs_and_listing(conn: sqlite3.Connection, uids: list[int]):
    refs, lines = [], []
    for uid in uids:
        ref = uid_resolve(conn, uid)
        if ref is None:
            continue
        refs.append({"table": ref[0], "id": ref[1], "uid": uid})
        text = _peek(conn, ref[0], ref[1])
        lines.append(f"#{uid} {text[:80]}")
    return refs, "\n".join(lines)


def _peek(conn: sqlite3.Connection, tbl: str, row_id: int) -> str:
    col = {"inbox": "raw_text", "tasks": "text", "reflections": "raw_text",
           "crm_notes": "note"}.get(tbl, "rowid")
    r = conn.execute(f"SELECT {col} AS v FROM {tbl} WHERE id = ?", (row_id,)).fetchone()
    return r["v"] if r else "(gone)"


# ------------------------------------------------------------ commands

def _handle_command(cfg: Config, conn: sqlite3.Connection, c: dict) -> dict:
    from .resolve import resolve_targets
    action = c["command"]["action"]
    target = c["command"]["target"]

    if action == "show":
        from .tasks import due_soon, open_tasks
        t = target.lower()
        if "due" in t or "overdue" in t:
            return {"reply": due_soon(conn), "pending": None}
        if "recent" in t or "record" in t or "note" in t:
            return {"reply": memory_edit.recent(conn, 10), "pending": None}
        return {"reply": open_tasks(conn), "pending": None}

    if action == "reassign":
        return _handle_reassign(cfg, conn, c)

    res = resolve_targets(cfg, conn, action, c["raw_text"], target)
    if res["question"]:
        return {"reply": f"❓ {res['question']}",
                "pending": {"type": "clarify", "text": c["raw_text"],
                            "inbox_id": None, "ts": now_ms()}}
    if not res["targets"]:
        return {"reply": "I couldn't match that to a record — /recent or /tasks"
                         " show ids you can use directly.", "pending": None}

    if action == "complete":
        from .tasks import set_status_by_uid
        out = []
        for uid in res["targets"]:
            txt = set_status_by_uid(conn, uid, "done")
            out.append(f"✅ done: #{uid} {txt}" if txt
                       else f"#{uid} isn't an open task.")
        return {"reply": "\n".join(out), "pending": None}

    if action == "source":
        from .provenance import describe
        return {"reply": "\n\n".join(describe(cfg, conn, uid)
                                     for uid in res["targets"][:3]), "pending": None}

    return {"reply": "I understood a command but not which action — try /guide.",
            "pending": None}


def _handle_reassign(cfg: Config, conn: sqlite3.Connection, c: dict) -> dict:
    """'Not my task — you do it.' Attempt what the bot can actually do;
    decline honestly otherwise. NEVER file it as the user's to-do."""
    ask = (c["command"]["target"] or c["raw_text"]).lower()
    if any(w in ask for w in ("browser", "history", "search")):
        from ..retrieval.ask import answer
        return {"reply": "On it — searching your trail:\n"
                         + answer(cfg, c["raw_text"]), "pending": None}
    if "brief" in ask:
        from ..cycles.brief import brief_command
        return {"reply": brief_command(cfg, send=False), "pending": None}
    if "gap" in ask:
        from ..curriculum.spaced import gaps_report
        return {"reply": gaps_report(cfg), "pending": None}
    return {"reply": ("Understood — that's for me, not a task for you. But I"
                      " can't do that myself yet: I can search your trail"
                      " (/ask), show tasks/gaps/briefs, and manage records."
                      " Not filing it as your task."), "pending": None}


# ------------------------------------------------------------ pending resolution

def _resolve_pending(cfg: Config, pending: dict, reply_text: str,
                     source: str = "telegram") -> dict:
    # TTL guard here too (not just at the db gate): a stale confirmation must
    # never fire, no matter which path delivered it
    if now_ms() - pending.get("ts", 0) > PENDING_TTL_MS:
        return route(cfg, reply_text, source=source)
    raw = reply_text.strip()
    ans = raw.lower().strip(".!")

    if pending["type"] == "voice_confirm":
        final = pending["text"] if ans in AFFIRM_CASUAL else raw
        return route(cfg, final, source="voice")

    if pending["type"] == "clarify":
        merged = pending["text"] + " — " + raw
        conn = db.connect(cfg.db_path)
        try:
            if pending.get("inbox_id"):
                memory_edit.delete_record(cfg, conn, pending["inbox_id"])
        finally:
            conn.close()
        return route(cfg, merged, source=source)

    if pending["type"] == "create_task":
        if ans in AFFIRM_STRICT:
            conn = db.connect(cfg.db_path)
            try:
                from .dispatch import create_grounded_task
                return {"reply": create_grounded_task(cfg, conn, pending),
                        "pending": None}
            finally:
                conn.close()
        if ans in NEGATE:
            return {"reply": "Okay — not creating it.", "pending": None}
        out = route(cfg, reply_text, source=source)
        out["reply"] = "(dropped the unconfirmed task)\n" + out["reply"]
        return out

    # delete / replace — STRICT vocabulary only ("ok" never destroys data)
    if ans in NEGATE:
        return {"reply": "Okay, left everything as-is.", "pending": None}
    if ans not in AFFIRM_STRICT:
        out = route(cfg, reply_text, source=source)
        out["reply"] = "(cancelled the pending confirmation)\n" + out["reply"]
        return out

    conn = db.connect(cfg.db_path)
    try:
        if pending["type"] == "delete":
            msgs = [memory_edit.delete_any(cfg, conn, r["table"], r["id"])[1]
                    for r in pending["refs"]]
            return {"reply": "\n".join(msgs), "pending": None}
        if pending["type"] == "replace":
            out = [memory_edit.replace_in(cfg, conn, r["table"], r["id"],
                                          pending["find"], pending["replace_with"])[1]
                   for r in pending["refs"]]
            return {"reply": "\n".join(out) or "Nothing to replace.", "pending": None}
    finally:
        conn.close()
    return {"reply": "Done.", "pending": None}


# backward-compat shim for older callers/tests
def resolve_pending(cfg: Config, pending: dict, reply_text: str,
                    source: str = "telegram") -> dict:
    return _resolve_pending(cfg, pending, reply_text, source)
