"""Intent classification: decide WHAT KIND of thing a message is before it
touches storage. Runs on every inbound message, so it's one tight T2 call
with reasoning suppressed and output capped.

Anti-confabulation is structural, not just prompted: the known-entity list is
supplied to the model, anything outside it comes back flagged `known: false`,
and dates are post-validated — a due date that isn't a real ISO date within a
sane window is dropped, never guessed."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3

from ..config import Config
from ..router.llm import RouterError, complete, parse_json_loose

log = logging.getLogger("exo.intents")

INTENTS = {"task", "weekly_update", "plan", "client_note", "reflection",
           "question", "correction", "command", "meta"}
CONFIDENCE_GATE = 0.55

PROMPT = """You classify one inbound message to a personal assistant. Decide what kind
of thing it is and extract the actionable structure. Current time:
{now} ({weekday}).

Known entities (clients/projects/domains I already track):
{entities}

Primary intents (choose exactly ONE as "intent"):
- task: something I need to DO. Extract every distinct action; if one action
  has several distinct pieces, make it a parent with "subtasks". Extract a
  due date ONLY if stated or clearly implied ("by Friday", "this week" ->
  resolve against current time, ISO YYYY-MM-DD). Never invent a date.
- weekly_update: reporting what I did/shipped/worked on.
- plan: an idea or something I'm considering, NOT committed to doing yet
  ("thinking about", "maybe", "could pitch").
- client_note: information/context about a client (preference, decision,
  fact) that is not a to-do.
- reflection: a learning, insight, or observation about my own skills/work.
- question: I'm asking the assistant about MY LIFE/data ("what did I search
  about X", "when did I last talk to Roven").
- correction: I'm fixing/removing something previously stored ("delete the
  X note", "delete task 9", "nevermind that last one", "it's Roven not
  Urban", "delete everything except #1").
- command: I'm operating the assistant itself — completing/closing a task
  ("mark the roven one done", "task 5 is finished"), showing lists ("show my
  tasks", "what's due"), asking a record's origin ("where did that note come
  from", "did I say this or did you?"), or REASSIGNING something to the
  assistant ("that's not my task, it's for you", "you check the browser
  history"). Fill "command".
- meta: I'm asking how the assistant itself works ("what can you do", "how
  do I fix a wrong note", "what do the labels mean", "what's your rulebook").

A message can carry several kinds at once: pick the dominant one as "intent",
list the others in "tags", AND fill their extraction fields too (e.g. an
update that mentions an undone item with a deadline also fills "tasks").

Entity rules (hard): match "entity" against the known list above
(case-insensitive, minor spelling drift is a match). If it matches, use the
EXACT known spelling with "known": true. If it's genuinely not in the list,
keep the message's spelling with "known": false. NEVER substitute or invent
a name. If you can't tell which entity is meant, use null.

Reply with ONLY this JSON (no prose):
{{"intent": "...", "tags": ["..."], "confidence": 0.0-1.0,
 "entity": {{"name": "...", "known": true/false}} or null,
 "tasks": [{{"text": "...", "due": "YYYY-MM-DD" or null, "priority": 0-2,
            "entity": "..." or null, "subtasks": ["..."],
            "presupposes": "..." or null}}],
 "ideas": ["..."], "note": "..." or null, "reflection": "..." or null,
 "weekly": "..." or null,
 "correction": {{"action": "delete" or "replace", "target": "...",
                "find": "..." or null, "replace_with": "..." or null}} or null,
 "command": {{"action": "complete" or "show" or "source" or "reassign",
             "target": "..."}} or null,
 "clarify": "..." or null}}

"presupposes": if a task's text assumes a PRIOR event/conversation happened
(e.g. "follow up on the call with X" assumes a call with X occurred), put a
short phrase naming that assumed event; else null.

Set "clarify" to ONE short question ONLY when the message is too ambiguous to
classify at all. priority: 2 only for explicit urgency ("urgent", "asap"),
1 for "important", else 0.

Message:
{text}"""


def known_entities(conn: sqlite3.Connection, limit: int = 40) -> list[str]:
    names: list[str] = []
    for r in conn.execute("SELECT name FROM crm ORDER BY updated_ts DESC LIMIT 15"):
        names.append(r["name"])
    for r in conn.execute(
        "SELECT DISTINCT entity FROM tasks WHERE entity IS NOT NULL"
        " ORDER BY updated_ts DESC LIMIT 15"
    ):
        names.append(r["entity"])
    for r in conn.execute(
        "SELECT name FROM skill_nodes WHERE type IN ('project', 'client', 'domain')"
        " ORDER BY updated_ts DESC LIMIT 25"
    ):
        names.append(r["name"])
    seen: dict[str, str] = {}
    for n in names:
        seen.setdefault(n.casefold(), n)
    return list(seen.values())[:limit]


def _valid_due(due, now: dt.datetime) -> int | None:
    """ISO date -> epoch ms (end of day 18:00 local). Anything unparseable or
    absurdly far (>1y) is dropped — never guessed."""
    if not due:
        return None
    try:
        d = dt.date.fromisoformat(str(due)[:10])
    except ValueError:
        return None
    if not (now.date() - dt.timedelta(days=1)
            <= d <= now.date() + dt.timedelta(days=366)):
        return None
    return int(dt.datetime.combine(d, dt.time(18, 0)).timestamp() * 1000)


def classify(cfg: Config, conn: sqlite3.Connection, text: str,
             forced_intent: str | None = None) -> dict:
    """Returns a normalized classification dict; raises RouterError upward.
    forced_intent: user already told us what it is — still run extraction,
    but pin the intent."""
    now = dt.datetime.now().astimezone()
    entities = known_entities(conn)
    prompt = PROMPT.format(
        now=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"),
        entities="\n".join(f"- {e}" for e in entities) or "- (none yet)",
        text=text[:3000],
    )
    if forced_intent:
        prompt += f"\n\nThe user has stated this is a '{forced_intent}' — use that as the intent."
    reply = complete(
        cfg, conn, "T2_workhorse", "intent_classify",
        [{"role": "user", "content": prompt}],
        max_tokens=1500, suppress_reasoning=True,
    )
    data = parse_json_loose(reply)
    return normalize(data, text, now, entities, forced_intent)


def normalize(data: dict, text: str, now: dt.datetime, entities: list[str],
              forced_intent: str | None = None) -> dict:
    intent = forced_intent or str(data.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = "reflection"  # safest storage default; raw text is kept anyway
    tags = [t for t in (data.get("tags") or []) if t in INTENTS and t != intent][:3]
    conf = data.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.5
    if forced_intent:
        conf = 1.0

    known_fold = {e.casefold(): e for e in entities}

    def _entity(raw) -> dict | None:
        if not raw:
            return None
        name = raw.get("name") if isinstance(raw, dict) else str(raw)
        if not name or not str(name).strip():
            return None
        name = str(name).strip()
        hit = known_fold.get(name.casefold())
        # trust the actual list over the model's "known" claim
        return {"name": hit or name, "known": hit is not None}

    tasks = []
    for t in (data.get("tasks") or [])[:6]:
        if not isinstance(t, dict) or not str(t.get("text") or "").strip():
            continue
        try:
            prio = max(0, min(2, int(t.get("priority") or 0)))
        except (TypeError, ValueError):
            prio = 0
        tasks.append({
            "text": str(t["text"]).strip()[:300],
            "due_ts": _valid_due(t.get("due"), now),
            "priority": prio,
            "entity": _entity(t.get("entity")),
            "subtasks": [str(s).strip()[:300] for s in (t.get("subtasks") or [])[:8]
                         if str(s).strip()],
            "presupposes": (str(t["presupposes"]).strip()[:150]
                            if t.get("presupposes") else None),
        })

    cmd = data.get("command") if isinstance(data.get("command"), dict) else None
    if cmd:
        cmd = {"action": str(cmd.get("action") or "")[:20],
               "target": str(cmd.get("target") or "")[:300]}
        if cmd["action"] not in ("complete", "show", "source", "reassign"):
            cmd = None

    corr = data.get("correction") if isinstance(data.get("correction"), dict) else None
    if corr:
        corr = {
            "action": "replace" if corr.get("action") == "replace" else "delete",
            "target": str(corr.get("target") or "")[:200],
            "find": (str(corr["find"])[:200] if corr.get("find") else None),
            "replace_with": (str(corr["replace_with"])[:200]
                             if corr.get("replace_with") else None),
        }

    return {
        "intent": intent, "tags": tags, "confidence": conf,
        "entity": _entity(data.get("entity")),
        "tasks": tasks,
        "ideas": [str(i).strip()[:300] for i in (data.get("ideas") or [])[:5]
                  if str(i).strip()],
        "note": (str(data["note"]).strip()[:500] if data.get("note") else None),
        "reflection": (str(data["reflection"]).strip()[:1000]
                       if data.get("reflection") else None),
        "weekly": (str(data["weekly"]).strip()[:1000] if data.get("weekly") else None),
        "correction": corr,
        "command": cmd,
        "clarify": (str(data["clarify"]).strip()[:200] if data.get("clarify") else None),
        "raw_text": text,
    }
