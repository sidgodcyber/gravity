"""Resolve a natural-language command ("delete that last one", "everything
except #1", "the museum director one") to concrete record uids.

Structural anti-confabulation: the model only ever sees real uids from the
current context (the last listing shown, open tasks, recent records, and
text-search candidates), and any target it returns that is NOT in that set
is discarded. It can name records; it cannot invent them.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from ..config import Config
from ..router.llm import RouterError, complete, parse_json_loose
from ..state.uids import uid_for
from . import memory_edit

log = logging.getLogger("exo.resolve")

RESOLVE_PROMPT = """You resolve a user's command about their stored records to concrete
record ids. Use ONLY ids that appear in the context lists below — never any
other number, even if the user's text mentions one that isn't listed.

Action: {action}
The user said: "{text}"

LAST LISTING the user saw (this is what "that one", "the last one",
"everything except #N" refer to):
{last_shown}

Open tasks:
{tasks}

Recent records:
{recent}

Text-search candidates matching their words:
{candidates}

Rules:
- Ordinals and demonstratives ("that last one", "the first one") refer to the
  LAST LISTING order.
- Set expressions ("all of them", "everything except #1") expand against the
  LAST LISTING; list every included id explicitly.
- "the <word> one" matches by content against all lists.
- If you cannot resolve confidently to specific ids, return an empty targets
  list and ONE specific question naming what's ambiguous (e.g. "Did you mean
  #12 'salon research' or #15 'salon note'?") — never a generic 'not found'.

Reply ONLY JSON: {{"targets": [ids], "question": null or str,
"note": short phrase describing what the targets are}}"""


def _fmt(rows: list[dict]) -> str:
    return "\n".join(f"- #{r['uid']}: {r['text']}" for r in rows) or "- (none)"


def gather_context(conn: sqlite3.Connection, target_text: str) -> dict:
    shown = memory_edit.last_shown(conn)
    tasks = []
    for r in conn.execute(
        "SELECT id, text, entity, status FROM tasks WHERE status = 'open'"
        " ORDER BY updated_ts DESC LIMIT 25"):
        tasks.append({"uid": uid_for(conn, "tasks", r["id"]),
                      "text": r["text"] + (f" [{r['entity']}]" if r["entity"] else "")})
    recent = []
    for r in conn.execute("SELECT id, raw_text FROM inbox ORDER BY id DESC LIMIT 15"):
        recent.append({"uid": uid_for(conn, "inbox", r["id"]),
                       "text": r["raw_text"][:80]})
    candidates = []
    if target_text and len(target_text) >= 3:
        for m in memory_edit.resolve_correction(conn, {"find": target_text}):
            candidates.append({"uid": uid_for(conn, m["table"], m["id"]),
                               "text": m["text"][:80]})
    return {"last_shown": shown, "tasks": tasks, "recent": recent,
            "candidates": candidates}


def resolve_targets(cfg: Config, conn: sqlite3.Connection, action: str,
                    text: str, target_text: str = "") -> dict:
    """Returns {"targets": [uid...], "question": str|None, "note": str}.
    Targets are guaranteed to exist in the provided context."""
    ctx = gather_context(conn, target_text or text)
    allowed = {r["uid"] for group in ctx.values() for r in group}
    if not allowed:
        return {"targets": [], "note": "",
                "question": "I don't have any stored records yet to match that against."}
    prompt = RESOLVE_PROMPT.format(
        action=action, text=text[:400],
        last_shown=_fmt(ctx["last_shown"]), tasks=_fmt(ctx["tasks"]),
        recent=_fmt(ctx["recent"]), candidates=_fmt(ctx["candidates"]),
    )
    try:
        reply = complete(cfg, conn, "T2_workhorse", "resolve_command",
                         [{"role": "user", "content": prompt}],
                         max_tokens=800, suppress_reasoning=True)
        data = parse_json_loose(reply)
    except (RouterError, ValueError, json.JSONDecodeError) as e:
        log.warning("resolution failed: %s", e)
        return {"targets": [], "note": "",
                "question": "I couldn't run resolution just now — try /recent"
                            " and give me the # directly."}
    targets = []
    for t in data.get("targets") or []:
        try:
            uid = int(t)
        except (TypeError, ValueError):
            continue
        if uid in allowed and uid not in targets:
            targets.append(uid)
    return {"targets": targets[:30],
            "question": (str(data["question"])[:300] if data.get("question") else None),
            "note": str(data.get("note") or "")[:150]}
