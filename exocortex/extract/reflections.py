"""/log reflections: store immediately, extract structure with a T1 model,
feed the skill graph. Extraction failures stay 'pending' and the nightly
cycle retries them."""

from __future__ import annotations

import json
import logging
import sqlite3

from ..config import Config
from ..router.llm import RouterError, complete_info, parse_json_loose
from ..state import db
from .graph import upsert_edge, upsert_node

log = logging.getLogger("exo.extract")

EXTRACT_PROMPT = """Extract structured fields from this personal reflection log entry.
Reply with ONLY a JSON object, no prose, with exactly these keys:
- "skills": array of 0-5 short skill names demonstrated (e.g. "python", "client negotiation")
- "domain": one short domain label (e.g. "software", "social media", "chemistry"), or null
- "project": short project name if one is clearly referenced, else null
- "insight_type": one of "learned" | "decision" | "idea" | "observation"
- "portfolio_score": 0.0-1.0, how portfolio/CV-worthy this is

Reflection:
{text}"""


def add_reflection(conn: sqlite3.Connection, cfg: Config, text: str,
                   extract_now: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO reflections (ts, device, raw_text) VALUES (?, ?, ?)",
        (db.now_ms(), cfg.device_id, text),
    )
    conn.commit()
    rid = cur.lastrowid
    if extract_now:
        try:
            extract_one(conn, cfg, rid)
        except Exception as e:
            log.warning("extraction deferred for reflection %s: %s", rid, e)
    return rid


def extract_one(conn: sqlite3.Connection, cfg: Config, rid: int) -> bool:
    row = conn.execute("SELECT raw_text FROM reflections WHERE id = ?", (rid,)).fetchone()
    if not row:
        return False
    try:
        reply, used_model = complete_info(
            cfg, conn, "T1_free", "extract_reflection",
            [{"role": "user", "content": EXTRACT_PROMPT.format(text=row["raw_text"])}],
            # thinking models (gemini 2.5, sonnet 5) spend output tokens on
            # reasoning before the JSON — tight budgets truncate mid-object
            max_tokens=1200,
        )
        data = parse_json_loose(reply)
        skills = [str(s).strip().lower() for s in data.get("skills") or [] if str(s).strip()][:5]
        domain = (data.get("domain") or None) and str(data["domain"]).strip().lower()
        project = (data.get("project") or None) and str(data["project"]).strip()
        insight = str(data.get("insight_type") or "observation").lower()
        score = max(0.0, min(1.0, float(data.get("portfolio_score") or 0)))
    except (RouterError, ValueError, KeyError, json.JSONDecodeError) as e:
        conn.execute(
            "UPDATE reflections SET extraction_status = 'failed' WHERE id = ?", (rid,)
        )
        conn.commit()
        log.warning("extraction failed for reflection %s: %s", rid, e)
        return False

    conn.execute(
        "UPDATE reflections SET skills = ?, domain = ?, project = ?, insight_type = ?,"
        " portfolio_score = ?, extraction_status = 'done', extraction_model = ?"
        " WHERE id = ?",
        (json.dumps(skills), domain, project, insight, score, used_model, rid),
    )
    evidence = {"table": "reflections", "id": rid}
    domain_id = upsert_node(conn, domain, "domain") if domain else None
    project_id = upsert_node(conn, project, "project") if project else None
    for skill in skills:
        sid = upsert_node(conn, skill, "skill")
        if domain_id:
            upsert_edge(conn, sid, domain_id, "in_domain", evidence)
        if project_id:
            upsert_edge(conn, sid, project_id, "demonstrated_in", evidence)
    conn.commit()
    return True


def extract_pending(conn: sqlite3.Connection, cfg: Config, limit: int = 25) -> int:
    """Retry pending/failed extractions (called by the nightly cycle)."""
    rows = conn.execute(
        "SELECT id FROM reflections WHERE extraction_status IN ('pending', 'failed')"
        " ORDER BY ts LIMIT ?",
        (limit,),
    ).fetchall()
    done = 0
    for r in rows:
        if extract_one(conn, cfg, r["id"]):
            done += 1
    return done
