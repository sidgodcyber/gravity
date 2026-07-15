"""Spaced repetition over inferred concepts, plus the /gaps report.

Flow: the engine writes gaps → the daily brief surfaces at most ONE (status
gap → reviewing, first check-in scheduled) → /review pops due concepts with
self-assessment buttons → "got it" advances through the intervals and
graduates at the end (skill-graph confidence rises); "fuzzy"/"nope" resets
the ladder and drops confidence. /learned graduates directly.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from ..config import Config
from ..extract.graph import set_node_confidence, upsert_node
from ..state import db

GRADUATE_BONUS = 0.25
FUMBLE_PENALTY = -0.10


def _intervals(cfg: Config) -> list[int]:
    return [int(x) for x in cfg.get("curriculum.review_intervals_days", [2, 4, 8, 16])]


# ------------------------------------------------------------ daily surfacing

def surface_daily(cfg: Config, conn: sqlite3.Connection):
    """Pick today's ONE foundation concept. Two gates: a GLOBAL one-per-20h
    gate (three /briefs in an afternoon must not burn three concepts) and a
    per-concept 3-day cooldown. Surfacing flips the concept to reviewing and
    schedules its first check-in."""
    now = db.now_ms()
    last_global = int(db.get_control(conn, "foundation_last_surfaced_ms", "0") or 0)
    if now - last_global < 20 * 3600_000:
        return None
    row = conn.execute(
        "SELECT * FROM concepts WHERE status = 'gap' AND explainer IS NOT NULL"
        " AND (surfaced_ts IS NULL OR surfaced_ts < ?)"
        " ORDER BY gap_score DESC, last_seen DESC LIMIT 1",
        (now - 3 * 86400_000,),
    ).fetchone()
    if row is None:
        return None
    first_interval = _intervals(cfg)[0]
    conn.execute(
        "UPDATE concepts SET status = 'reviewing', surfaced_ts = ?,"
        " next_review_ts = ?, review_stage = 0 WHERE id = ?",
        (now, now + first_interval * 86400_000, row["id"]),
    )
    conn.commit()
    db.set_control(conn, "foundation_last_surfaced_ms", str(now))
    return row


def foundation_section(cfg: Config, conn: sqlite3.Connection,
                       local_conn: sqlite3.Connection | None = None) -> str:
    """Deterministic brief section: one new concept and/or due check-ins.
    The 'why' rationale is trail-derived and lives in local.db."""
    parts = []
    row = surface_daily(cfg, conn)
    if row is not None:
        parts.append(f"📚 Foundation — {row['name']}")
        why = _local_why(local_conn, row["name"])
        if why:
            parts.append(f"Why now: {why}")
        parts.append(row["explainer"])
    due = due_concepts(conn)
    if due:
        names = ", ".join(r["name"] for r in due[:3])
        parts.append(f"🔁 Due for review: {names} — reply /review")
    return "\n".join(parts)


def _local_why(local_conn: sqlite3.Connection | None, name: str) -> str:
    if local_conn is None:
        return ""
    try:
        row = local_conn.execute(
            "SELECT why FROM concept_evidence WHERE concept_name = ?"
            " ORDER BY ts DESC LIMIT 1", (name,),
        ).fetchone()
        return row["why"] if row else ""
    except Exception:
        return ""


# ------------------------------------------------------------ review loop

def due_concepts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM concepts WHERE status = 'reviewing'"
        " AND next_review_ts IS NOT NULL AND next_review_ts <= ?"
        " ORDER BY next_review_ts LIMIT 5",
        (db.now_ms(),),
    ).fetchall()


def grade(cfg: Config, conn: sqlite3.Connection, concept_id: int, result: str) -> str:
    """result: got_it | fuzzy | nope. Returns a short human reply."""
    row = conn.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    if row is None:
        return "that concept vanished — odd."
    intervals = _intervals(cfg)
    now = db.now_ms()
    node_id = upsert_node(conn, row["name"], "concept", bump=False)
    if result == "got_it":
        stage = row["review_stage"] + 1
        if stage >= len(intervals):
            conn.execute(
                "UPDATE concepts SET status = 'graduated', next_review_ts = NULL,"
                " review_stage = ? WHERE id = ?", (stage, concept_id),
            )
            set_node_confidence(conn, node_id, delta=GRADUATE_BONUS)
            conn.commit()
            return f"🎓 {row['name']} graduated — it's yours now."
        nxt = intervals[min(stage, len(intervals) - 1)]
        conn.execute(
            "UPDATE concepts SET review_stage = ?, next_review_ts = ? WHERE id = ?",
            (stage, now + nxt * 86400_000, concept_id),
        )
        set_node_confidence(conn, node_id, delta=0.05)
        conn.commit()
        return f"✓ {row['name']}: next check-in in {nxt} days."
    # fuzzy or nope — back to the start of the ladder
    conn.execute(
        "UPDATE concepts SET review_stage = 0, fumble_count = fumble_count + 1,"
        " next_review_ts = ? WHERE id = ?",
        (now + intervals[0] * 86400_000, concept_id),
    )
    set_node_confidence(conn, node_id, delta=FUMBLE_PENALTY)
    conn.commit()
    return (f"↺ {row['name']} stays in rotation (check-in in {intervals[0]} days)."
            " Re-read the explainer when you get a minute.")


def mark_learned(cfg: Config, conn: sqlite3.Connection, topic: str) -> str:
    topic = topic.strip().casefold()
    row = conn.execute(
        "SELECT id, name FROM concepts WHERE name = ? OR name LIKE ?"
        " ORDER BY length(name) LIMIT 1",
        (topic, f"%{topic}%"),
    ).fetchone()
    if row is None:
        # nothing tracked by that name — record it as known directly
        node_id = upsert_node(conn, topic, "concept", bump=False)
        set_node_confidence(conn, node_id, delta=GRADUATE_BONUS)
        conn.commit()
        return f"noted — '{topic}' recorded as understood (wasn't in the gap list)."
    conn.execute(
        "UPDATE concepts SET status = 'graduated', next_review_ts = NULL WHERE id = ?",
        (row["id"],),
    )
    node_id = upsert_node(conn, row["name"], "concept", bump=False)
    set_node_confidence(conn, node_id, delta=GRADUATE_BONUS)
    conn.commit()
    return f"🎓 {row['name']} graduated."


# ------------------------------------------------------------ weekly outcomes

def store_weekly_outcome(cfg: Config, text: str) -> int:
    """The one deliberate log of the week. Stored as a reflection with
    insight_type='weekly_outcome' (synced — it's my own statement, like /log
    was). Not re-extracted: the marker type must survive. Returns the
    USER-FACING uid (the only number ever shown)."""
    from ..state.uids import uid_for
    conn = db.connect(cfg.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO reflections (ts, device, raw_text, insight_type,"
            " extraction_status) VALUES (?, ?, ?, 'weekly_outcome', 'done')",
            (db.now_ms(), cfg.device_id, text.strip()),
        )
        conn.commit()
        return uid_for(conn, "reflections", cur.lastrowid)
    finally:
        conn.close()


# ------------------------------------------------------------ /gaps report

def gaps_report(cfg: Config) -> str:
    conn = db.connect(cfg.db_path)
    local_conn = db.local_connect(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM concepts WHERE status IN ('gap', 'reviewing')"
            " ORDER BY gap_score DESC, last_seen DESC LIMIT 5"
        ).fetchall()
        if not rows:
            return ("No foundation gaps on file yet. The engine needs a few days"
                    " of trail (searches, AI prompts, commits) to have evidence"
                    " worth acting on — this is an honest empty, not a bug.")
        lines = ["🧱 Foundation gaps (top 5):"]
        for i, r in enumerate(rows, 1):
            state = "" if r["status"] == "gap" else " · in review"
            fumbles = f" · fumbled x{r['fumble_count']}" if r["fumble_count"] else ""
            lines.append(f"\n{i}. {r['name']} — score {r['gap_score']:.1f}"
                         f" · {r['engagement'] or '?'} engagement{state}{fumbles}")
            ev = local_conn.execute(
                "SELECT evidence, why FROM concept_evidence WHERE concept_name = ?"
                " ORDER BY ts DESC LIMIT 1", (r["name"],),
            ).fetchone()
            if ev:
                if ev["why"]:
                    lines.append(f"   why: {ev['why']}")
                for cite in json.loads(ev["evidence"])[:3]:
                    lines.append(f"   · {cite}")
        return "\n".join(lines)
    finally:
        local_conn.close()
        conn.close()
