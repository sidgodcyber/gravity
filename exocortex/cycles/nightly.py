"""The sleep cycle: nightly consolidation of the day's captures.

Steps: summarize the day's life stream (T1_free), retry pending reflection
extractions, refresh style-corpus embeddings, detect emerging interests,
refresh CRM staleness, surface opportunities, then synthesize the morning
brief (T2, degrading to T1, degrading to a mechanical brief) and deliver it
via Telegram. Every step is best-effort: one failure never kills the cycle.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from collections import Counter
from urllib.parse import urlparse

from ..config import Config
from ..extract.reflections import extract_pending
from ..router.llm import BudgetExceeded, RouterError, complete
from ..state import db
from .telegram_send import send_message

log = logging.getLogger("exo.nightly")

LANES = "museum-dev internship / freelance social media / science competitions"


# ------------------------------------------------------------------ materials

def gather_day_activity(conn: sqlite3.Connection, lookback_hours: float,
                        local_conn: sqlite3.Connection | None = None) -> dict:
    """conn = synced db (reflections); local_conn = Plane B trail. Passing
    only one connection still works for tests that keep both in one db."""
    trail = local_conn if local_conn is not None else conn
    since = db.now_ms() - int(lookback_hours * 3600 * 1000)
    rows = trail.execute(
        "SELECT source, kind, content, app, extra FROM life_stream"
        " WHERE ts >= ? AND sensitivity = 'normal'", (since,),
    ).fetchall()
    windows: Counter = Counter()
    apps: Counter = Counter()
    domains: Counter = Counter()
    files: Counter = Counter()
    clip_count = 0
    for r in rows:
        if r["source"] == "window":
            windows[r["content"][:120]] += 1
            if r["app"]:
                apps[r["app"]] += 1
        elif r["source"] == "browser":
            netloc = urlparse(r["content"]).netloc
            if netloc:
                domains[netloc] += 1
        elif r["source"] == "file":
            files[r["content"]] += 1
        elif r["source"] == "clipboard":
            clip_count += 1  # clipboard text stays out of LLM prompts
    reflections = conn.execute(
        "SELECT ts, raw_text FROM reflections WHERE ts >= ? ORDER BY ts", (since,)
    ).fetchall()
    return {
        "windows": windows.most_common(30),
        "apps": apps.most_common(8),
        "domains": domains.most_common(15),
        "files": [p for p, _ in files.most_common(12)],
        "clip_count": clip_count,
        "reflections": [(r["ts"], r["raw_text"]) for r in reflections],
        "event_count": len(rows),
    }


def summarize_day(cfg: Config, conn: sqlite3.Connection, activity: dict) -> str:
    if not activity["event_count"] and not activity["reflections"]:
        return "No activity captured."
    material = []
    material.append("Window titles (count): " + ("; ".join(
        f"{t} x{c}" for t, c in activity["windows"][:20]) or "none"))
    material.append("Apps: " + (", ".join(f"{a} x{c}" for a, c in activity["apps"]) or "none"))
    material.append("Web domains: " + (", ".join(f"{d} x{c}" for d, c in activity["domains"]) or "none"))
    material.append("Files touched: " + (", ".join(activity["files"]) or "none"))
    material.append("Reflections: " + (" | ".join(t for _, t in activity["reflections"]) or "none"))
    prompt = (
        "Summarize this day of my digital activity in exactly 3 sentences,"
        " naming the main projects/topics. Material:\n" + "\n".join(material)
    )
    try:
        return complete(cfg, conn, "T1_free", "nightly_summary",
                        [{"role": "user", "content": prompt}], max_tokens=1000)
    except RouterError as e:
        log.warning("day summary degraded to mechanical (%s)", e)
        top = ", ".join(t for t, _ in activity["windows"][:5])
        return f"Captured {activity['event_count']} events; mostly: {top}."


# ------------------------------------------------------------------ analysis

def detect_emerging_interests(conn: sqlite3.Connection) -> list[str]:
    """Sustained recent activity in a domain that has little history."""
    now = db.now_ms()
    recent = now - 14 * 86400_000
    old_window = now - 74 * 86400_000
    out = []
    for r in conn.execute(
        "SELECT domain, COUNT(*) AS n FROM reflections"
        " WHERE ts >= ? AND domain IS NOT NULL GROUP BY domain HAVING n >= 2",
        (recent,),
    ):
        prior = conn.execute(
            "SELECT COUNT(*) FROM reflections WHERE domain = ? AND ts >= ? AND ts < ?",
            (r["domain"], old_window, recent),
        ).fetchone()[0]
        if prior == 0:
            out.append(f"{r['domain']} ({r['n']} recent reflections, none before)")
    return out[:3]


def crm_alerts(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    stale_ms = db.now_ms() - int(cfg.get("crm.stale_after_days", 14)) * 86400_000
    alerts = []
    for r in conn.execute("SELECT id, name, kind, next_action, next_action_due FROM crm"):
        last = conn.execute(
            "SELECT MAX(ts) FROM crm_touchpoints WHERE crm_id = ?", (r["id"],)
        ).fetchone()[0]
        due = r["next_action_due"]
        today = dt.date.today().isoformat()
        if due and due <= today:
            alerts.append(f"{r['name']}: due {due} — {r['next_action'] or 'follow up'}")
        elif last is None or last < stale_ms:
            days = "never contacted" if last is None else f"{(db.now_ms() - last) // 86400_000}d silent"
            alerts.append(f"{r['name']} ({r['kind']}): {days} — going stale")
    return alerts[:5]


def top_opportunities(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT id, title, fit_score, payoff_estimate, deadline FROM opportunities"
        " WHERE status IN ('new', 'surfaced')"
        " ORDER BY fit_score IS NULL, fit_score DESC, deadline IS NULL, deadline LIMIT 3"
    ).fetchall()
    out = []
    for r in rows:
        bits = [r["title"]]
        if r["payoff_estimate"]:
            bits.append(str(r["payoff_estimate"]))
        if r["deadline"]:
            bits.append(f"due {r['deadline']}")
        out.append(" — ".join(bits))
        conn.execute("UPDATE opportunities SET status = 'surfaced' WHERE id = ?", (r["id"],))
    conn.commit()
    return out


# A real cross-project connection must rest on a concrete shared artifact a
# node you actually USE that both projects touch — not a shared category label.
# Domains/clients/people are excluded as the shared node: two projects both
# tagged "space science" share vocabulary, not an artifact.
_ARTIFACT_TYPES = ("tool", "skill", "concept", "technique", "dataset", "file")
_ENDPOINT_TYPES = ("project", "domain")


def missed_connection(conn: sqlite3.Connection) -> str:
    """State a cross-project connection ONLY when two recently-active,
    otherwise-unlinked projects share a concrete artifact node (a
    tool/technique/dataset/file/skill both connect to via edges). Returns ""
    (so the brief omits the line) when no such artifact exists — two projects
    that share only surface jargon must produce nothing, never an invented
    link. The returned string names the shared artifact so the brief can cite
    it rather than free-associate."""
    cutoff = db.now_ms() - 21 * 86400_000
    arts = ",".join("?" * len(_ARTIFACT_TYPES))
    ends = ",".join("?" * len(_ENDPOINT_TYPES))
    row = conn.execute(
        f"""
        SELECT a.name AS an, b.name AS bn, c.name AS cn, c.type AS ct,
               (a.confidence + b.confidence) AS score
        FROM skill_nodes c
        JOIN skill_edges ea
          ON (ea.src = c.id OR ea.dst = c.id)
        JOIN skill_nodes a
          ON a.id = CASE WHEN ea.src = c.id THEN ea.dst ELSE ea.src END
        JOIN skill_edges eb
          ON (eb.src = c.id OR eb.dst = c.id)
        JOIN skill_nodes b
          ON b.id = CASE WHEN eb.src = c.id THEN eb.dst ELSE eb.src END
        WHERE c.type IN ({arts})
          AND a.id < b.id
          AND a.type IN ({ends}) AND b.type IN ({ends})
          AND a.updated_ts >= ? AND b.updated_ts >= ?
          AND NOT EXISTS (
            SELECT 1 FROM skill_edges e
            WHERE (e.src = a.id AND e.dst = b.id) OR (e.src = b.id AND e.dst = a.id))
        ORDER BY score DESC, c.confidence DESC
        LIMIT 1
        """,
        (*_ARTIFACT_TYPES, *_ENDPOINT_TYPES, *_ENDPOINT_TYPES, cutoff, cutoff),
    ).fetchone()
    if not row:
        return ""
    return f"{row['an']} and {row['bn']} both use {row['cn']} ({row['ct']})"


# ------------------------------------------------------------------ brief

def compose_brief(cfg: Config, conn: sqlite3.Connection,
                  local_conn: sqlite3.Connection | None = None) -> str:
    activity = gather_day_activity(
        conn, float(cfg.get("cycle.lookback_hours", 36)), local_conn
    )
    day_summary = summarize_day(cfg, conn, activity)
    interests = detect_emerging_interests(conn)
    alerts = crm_alerts(conn, cfg)
    opps = top_opportunities(conn)
    connection = missed_connection(conn)

    material = {
        # ORDER MATTERS: reflections are what I deliberately chose to record —
        # they outrank everything the daemon captured ambiently
        "my_own_log_entries_PRIMARY_SIGNAL": [t for _, t in activity["reflections"]][-8:],
        "ambient_activity_summary_secondary": day_summary,
        "emerging_interests_flag_for_confirmation": interests,
        "opportunities": opps,
        "connection_via_shared_artifact": connection,
        "crm_followups": alerts,
    }
    prompt = (
        f"Write my morning brief from this material. My three lanes: {LANES}.\n"
        "PRIORITY RULE: my own /log entries are the primary signal — they hold"
        " my commitments, worries and decisions. Ambient activity (window"
        " titles, browsing) is background color only; never let it displace"
        " what I explicitly logged. If a log entry mentions clients, deadlines"
        " or falling behind, that LEADS the brief.\n"
        "Format, plain text, under 220 words total:\n"
        "🌅 Brief — <weekday, date>\n"
        "Yesterday: exactly three sentences, log entries first.\n"
        "Today: up to 3 priorities across my lanes — derive them from my log"
        " entries first (commitments and deadlines beat routine activity).\n"
        "Opportunities: at most 3 one-liners (omit section if none).\n"
        "Connection you missed: ONLY if"
        " 'connection_via_shared_artifact' below is non-empty, write ONE"
        " sentence noting the two projects both use that shared artifact and"
        " suggesting they could inform each other — you MUST name the shared"
        " artifact given. If it is empty, OMIT this line entirely. Do NOT"
        " invent a connection from topic or word overlap.\n"
        "Follow-ups: CRM alerts as given (omit if none).\n"
        "If emerging interests are flagged, end with: 'New interest detected:"
        " <x> — confirm?'\n\n"
        f"Material:\n{json.dumps(material, indent=1)}\n"
        f"Today's date: {dt.datetime.now():%A, %B %d, %Y}"
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        text = complete(cfg, conn, "T2_workhorse", "brief_synthesis", messages, max_tokens=4000)
    except (BudgetExceeded, RouterError):
        # budget cap or workhorse outage: free tier first, mechanical last
        try:
            text = complete(cfg, conn, "T1_free", "brief_synthesis", messages, max_tokens=4000)
        except RouterError:
            text = _mechanical_brief(day_summary, opps, alerts, connection)

    # Deterministic sections appended after synthesis — no model rewrites them.
    try:
        from ..intents.tasks import brief_tasks
        tsec = brief_tasks(conn)
        if tsec:
            text = text.rstrip() + "\n\n" + tsec
    except Exception:
        log.exception("task section failed; brief goes out without it")
    try:
        from ..curriculum.spaced import foundation_section
        section = foundation_section(cfg, conn, local_conn)
        if section:
            text = text.rstrip() + "\n\n" + section
    except Exception:
        log.exception("foundation section failed; brief goes out without it")

    conn.execute("INSERT INTO briefs (ts, content) VALUES (?, ?)", (db.now_ms(), text))
    conn.commit()
    return text


def _mechanical_brief(day_summary: str, opps: list[str], alerts: list[str],
                      connection: str) -> str:
    parts = [f"🌅 Brief — {dt.datetime.now():%A, %b %d} (offline fallback)"]
    parts.append(f"Yesterday: {day_summary}")
    if opps:
        parts.append("Opportunities: " + "; ".join(opps))
    if connection:
        parts.append(f"Unlinked pair worth connecting: {connection}")
    if alerts:
        parts.append("Follow-ups: " + "; ".join(alerts))
    return "\n".join(parts)


# ------------------------------------------------------------------ cycle

def run_nightly(cfg: Config) -> str:
    logging.basicConfig(level=logging.INFO)
    db.split_plane_b(cfg)
    conn = db.connect(cfg.db_path)
    local_conn = db.local_connect(cfg)
    try:
        # merge spoke capture FIRST so tonight's extraction/graph/brief see it
        from ..sync.merge import merge_batches, merge_summary
        log.info("nightly: spoke merge: %s", merge_summary(merge_batches(cfg)))
    except Exception:
        log.exception("nightly: spoke merge failed")
    try:
        n = extract_pending(conn, cfg)
        log.info("nightly: %d reflections extracted", n)
    except Exception:
        log.exception("nightly: extraction step failed")
    try:
        from ..daemon.sources.git_activity import scan_repos
        log.info("nightly: %d commits imported (Plane A)", scan_repos(cfg, conn))
    except Exception:
        log.exception("nightly: git scan failed")
    try:
        from ..state.status import daemon_alive
        work_on = db.get_control(conn, "work_mode", "off") == "on"
        if daemon_alive(cfg):
            pass  # the daemon's hourly import owns this; avoid checkpoint races
        elif work_on:
            pass  # importing now would drop everything AND advance checkpoints
        else:
            from ..daemon.sources import ai_trail
            from ..daemon.filter import Firewall
            from ..daemon.pipeline import Pipeline
            p = Pipeline(local_conn, Firewall(cfg.data["firewall"]), cfg.device_id,
                         inline_flush=True)
            ai_trail.import_prompts(cfg, local_conn, p)
            p.flush()
    except Exception:
        log.exception("nightly: ai trail import failed")
    try:
        from ..curriculum.engine import run_engine
        log.info("nightly: concept engine: %s", run_engine(cfg, conn, local_conn))
    except Exception:
        log.exception("nightly: concept engine failed")
    try:
        if cfg.get("embeddings.enabled", True):
            from ..retrieval.embeddings import embed_pending
            log.info("nightly: %d rows embedded", embed_pending(cfg))
    except Exception:
        log.exception("nightly: embedding step failed")
    try:
        if cfg.get("vault.enabled", True):
            from ..vault import export_vault
            log.info("nightly: vault export: %s", export_vault(cfg))
    except Exception:
        log.exception("nightly: vault export failed")
    try:
        text = compose_brief(cfg, conn, local_conn)
    except Exception:
        log.exception("nightly: brief composition failed hard")
        text = f"🌅 Brief — {dt.datetime.now():%A}: cycle failed, check logs."
        # store the failure brief too, so the delivered-flag update below
        # can't mark yesterday's brief as today's
        conn.execute("INSERT INTO briefs (ts, content) VALUES (?, ?)", (db.now_ms(), text))
        conn.commit()
    delivered = send_message(cfg, text)
    if delivered:
        conn.execute(
            "UPDATE briefs SET delivered = 1 WHERE id = (SELECT MAX(id) FROM briefs)"
        )
        conn.commit()
    local_conn.close()
    conn.close()
    return text


def run_weekly(cfg: Config) -> bool:
    """The Sunday-evening ritual: one question, answerable by voice or /week."""
    logging.basicConfig(level=logging.INFO)
    return send_message(
        cfg,
        "📦 Weekly check-in: what did you ship or work on this week?\n"
        "Reply with a voice note (30 seconds is plenty) or `/week <text>`.",
    )
