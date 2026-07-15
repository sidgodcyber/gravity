"""Gravity's rulebook — ONE source of truth. `/guide` reads these topics,
natural-language questions about Gravity route here (never stored as logs),
and `exo guide --write` renders USER_GUIDE.md from the same dict so the doc
can't drift from the bot."""

from __future__ import annotations

TOPICS: dict[str, str] = {
    "talking": """HOW TO TALK TO GRAVITY
Just type (or send a voice note). Every message is classified as one of:
task · weekly update · idea/plan · client note · reflection · question ·
correction · command. Gravity echoes back what it stored ("✅ Task #12 ...")
— that echo is your tripwire: if it read you wrong, say so or /edit.
Examples that work:
- "research Roven's salon idea by Friday" → task with due date
- "finished the shoot, still owe them captions by Wed" → update + task
- "delete that last note" / "delete everything except #1" → shows the exact
  list, asks you to confirm
- "mark the roven one done" · "where did that note come from?"
- "what did I search about mediapipe?" → answered, not stored""",

    "ids": """RECORD IDS
Every number Gravity shows (#12) is from ONE shared namespace — tasks, logs,
notes all included — and always retrieves exactly the record it was shown
for. Deleted ids are never reused. Use them with /delete /edit /done /drop
/source.""",

    "tasks": """TASKS
/tasks — open tasks grouped by client/project
/due — due & overdue (overdue first)
/task <text> — force-create (still extracts due date/client)
/done <id> · /drop <id> — close/abandon (sub-tasks close with the parent)
Natural language works: "task 12 is finished", "drop the calendar one".
Open/overdue tasks lead the morning brief. If a task presupposes an event
Gravity has no record of ("follow up on the call with X"), it asks before
creating instead of inventing the premise.""",

    "delete": """FIXING & DELETING (editable memory)
/recent [n] — last records with ids, provenance, and timestamps
/edit <id> <new text> — replace & re-classify
/delete <id> — always asks you to confirm first
Natural language: "delete the urban straps note", "it's Roven not Urban",
"delete everything except #1" — Gravity shows the EXACT records first and
touches nothing until you reply yes. Deletes heal downstream: spawned tasks,
notes, and skill-graph entries whose only source was that record are removed
too, so ghosts can't haunt future briefs.
Confirmations for deletes/replaces need a deliberate *yes/yep/confirm* —
"ok" never destroys data (it only accepts voice transcripts). no/cancel
cancels; anything else cancels AND is processed as a new message. A
confirmation expires after 10 minutes. On the CLI, `exo delete <id>` previews
and requires --yes.""",

    "provenance": """PROVENANCE (where data came from)
Every displayed record carries its origin in plain words:
- "you said" — you typed it (Telegram/CLI) or spoke it (voice note)
- "system test message (not you)" — verification traffic, never your words
- "inferred" — Gravity derived it (concept engine, extraction)
Browsing/search/AI-prompt data lives in the local trail and is labeled
"observed" wherever it surfaces (e.g. in /ask answers and /gaps evidence) —
it never appears in /recent as if you typed it.
/source <id> (or "did I say this or did you?") gives origin + timestamp +
pointer.""",

    "voice": """VOICE NOTES
Transcribed locally on this laptop (small Whisper model) — rough by design,
correctable by design: Gravity shows what it heard; reply "ok" to file it, or
retype the correct text and that's used instead. Weekly check-in (Sunday
19:00) accepts a 30-second voice note or /week <text>.""",

    "brief": """BRIEF & LEARNING
/brief — composed fresh on demand (the 07:00 delivery uses the 03:30 cycle).
Sections: yesterday, priorities (your own words first), tasks due, foundation
concept (one/day), due reviews. "Connection you missed" only appears when two
projects share a concrete artifact in the skill graph — never word overlap.
/gaps — evidence-cited foundation gaps · /review — spaced check-ins ·
/learned <topic> — graduate a concept.""",

    "schedules": """SCHEDULES
Registered by `exo install --apply` (verify: `exo install --status`):
- nightly cycle 03:30 (trail import, concept engine, brief compose)
- brief delivery 07:00 · weekly check-in Sunday 19:00
- daemon + bot at logon (Windows Startup folder; systemd on Linux)
A job that was never registered fails silently — run --status after setup.""",

    "capture": """CAPTURE & THE EMPLOYER-CODE EXCLUSION
Two planes. Plane A (code): git activity is read ONLY from repos listed in
capture_code_from — an allowlist, so employer code is structurally
unreachable. Plane B (research trail: browser, searches, window titles,
clipboard, AI prompts) is captured generally into a separate local-only
database that is never included in sync bundles. /work on suspends capture
(manual logs still work).""",

    "devices": """MULTI-DEVICE (hub & spoke)
One machine is the hub — it runs this bot, the cycles, and owns the brain.
Other machines are spokes: capture only. A spoke stages your `exo log`
reflections and allowlisted git commits, then pushes them as encrypted
batches; the hub merges them during the nightly cycle (or `exo sync merge`).
Merges are insert-only and idempotent — the same batch can never import
twice. /devices shows every machine and its last batch, so a dead spoke
daemon is visible from your phone. Each machine has its OWN code allowlist
(capture_code_from keyed by device name) and its own local-only research
trail — Plane B never leaves any machine, so /gaps and /ask see only the
hub's trail. Records name their device: "you said (direct log, on desktop)".
A spoke refuses to run the bot or cycles — one bot, ever (the hub's).""",

    "vault": """GRAPH VAULT (Obsidian)
`exo graph export` writes your skill graph as markdown notes (default
d:\\gravity-vault, set vault.path in config); the nightly cycle re-exports
automatically. Open that folder as a vault in Obsidian and use the graph
view (Ctrl+G) — folders = node types (Skills/Domains/Projects/Concepts/
Clients), so "group by folder" colors the graph by type. Each note carries
confidence + dates, its connections as [[links]], and the evidence behind
each link with a provenance label and record id you can /source.
Notes marked `gravity: exporter-owned` are overwritten or removed on every
export to mirror the database — put your own thoughts in separate files (any
name not used by the exporter) and they are never touched. The vault is
derived data: it is NOT synced, and deleting it loses nothing.""",

    "limits": """KNOWN LIMITS (honest)
- Voice transcription is imperfect on this hardware — hence the confirm step.
- Memory starts at capture-start (early July 2026) plus ~90 days of
  backfilled browser history; nothing before that exists.
- Free-tier models can be slow (seconds) and occasionally misclassify — the
  echo + /edit + /delete are the safety net.
- A plausible-looking due date can slip through; every created task shows its
  date so you can catch it.""",
}

_ALIASES = {
    "deleting": "delete", "edit": "delete", "editing": "delete", "fix": "delete",
    "corrections": "delete", "memory": "delete", "id": "ids", "source": "provenance",
    "obsidian": "vault", "graph": "vault", "visualization": "vault",
    "multi-device": "devices", "multidevice": "devices", "spoke": "devices",
    "hub": "devices", "desktop": "devices", "device": "devices",
    "origin": "provenance", "task": "tasks", "todo": "tasks", "schedule": "schedules",
    "cron": "schedules", "install": "schedules", "voice notes": "voice",
    "transcription": "voice", "privacy": "capture", "work": "capture",
    "planes": "capture", "limitations": "limits", "commands": "talking",
    "help": "talking", "rules": "talking", "gaps": "brief", "review": "brief",
}

CARD = """🧭 Gravity — command card
Talk normally; I classify, store, and echo back what I did.
Tasks: /tasks /due /task /done /drop
Fix me: /recent /edit /delete /source (or just say it)
Learn:  /gaps /review /learned /week (voice notes fine)
Daily:  /brief /budget /ask /work on|off /devices
Meta:   /guide <topic> — topics: """ + ", ".join(sorted(TOPICS))


def guide_text(topic: str = "") -> str:
    t = topic.strip().lower()
    if not t:
        return CARD
    t = _ALIASES.get(t, t)
    if t in TOPICS:
        return TOPICS[t]
    hits = [k for k in TOPICS if t in k or t in TOPICS[k].lower()]
    if len(hits) == 1:
        return TOPICS[hits[0]]
    if hits:
        return "Closest topics: " + ", ".join(hits) + "\n\n" + TOPICS[hits[0]]
    return CARD


def answer_meta(question: str) -> str:
    """Natural-language questions about Gravity itself — keyword-routed to
    topics, never stored as user content."""
    q = question.lower()
    scores = {}
    keywords = {
        "talking": ["what can you", "how do i use", "commands", "help"],
        "delete": ["delete", "fix", "wrong", "edit", "correct", "remove"],
        "provenance": ["where did", "did i say", "source", "origin", "came from"],
        "tasks": ["task", "todo", "due", "remind"],
        "voice": ["voice", "transcri", "audio", "mishear"],
        "schedules": ["schedule", "when do", "nightly", "weekly", "cron"],
        "capture": ["capture", "privacy", "employer", "work mode", "browser"],
        "brief": ["brief", "gaps", "review", "learn", "connection"],
        "vault": ["obsidian", "vault", "graph view", "visuali", "map"],
        "devices": ["device", "desktop", "spoke", "hub", "machine",
                    "other computer", "second computer"],
        "limits": ["limit", "can't", "cannot", "rulebook", "honest"],
        "ids": ["id", "number"],
    }
    for topic, kws in keywords.items():
        scores[topic] = sum(1 for k in kws if k in q)
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return CARD
    return TOPICS[best] + "\n\n(more: /guide)"


def write_user_guide(path: str = "USER_GUIDE.md") -> str:
    from pathlib import Path

    from .config import REPO_ROOT
    out = Path(REPO_ROOT) / path
    parts = [
        "# Gravity — User Guide",
        "",
        "*Generated from the live rulebook (`exocortex/guide.py`) — the same*",
        "*content `/guide` serves, so this file cannot drift from the bot.*",
        "*Regenerate with `exo guide --write`.*",
        "",
    ]
    order = ["talking", "ids", "tasks", "delete", "provenance", "voice",
             "brief", "schedules", "capture", "devices", "vault", "limits"]
    for key in order:
        body = TOPICS[key]
        title, _, rest = body.partition("\n")
        parts.append(f"## {title.title()}")
        parts.append(rest.strip())
        parts.append("")
    out.write_text("\n".join(parts), encoding="utf-8", newline="\n")
    return str(out)
