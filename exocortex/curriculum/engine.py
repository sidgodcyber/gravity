"""The concept-inference engine: decides, from the activity trail, what I'm
learning and where the foundation gaps are.

Design: mechanical SQL aggregation builds a compact per-day trail (searches,
AI prompts, pages, commits, weekly outcomes) with repetition/depth counts;
ONE structured LLM call over that material (T2 free stack) does the actual
judgment; a programmatic validator then cross-checks every evidence citation
against the real trail — a claimed quote that doesn't exist in the material
kills the citation, and a gap with no surviving evidence is dropped and
logged, never shown. Grounding over eloquence: the prompt requires stating
absence rather than inventing urgency, and the validator enforces the
receipts side of that.

Results: `concepts` rows (synced — names, scores, explainers are curriculum)
+ `concept_evidence` rows (local.db — the raw trail excerpts stay on this
machine) + skill-graph writeback (nodes for concepts/domains; repeated
re-lookups push confidence DOWN).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from collections import defaultdict
from urllib.parse import urlparse

from ..config import Config
from ..extract.graph import set_node_confidence, upsert_edge, upsert_node
from ..router.llm import RouterError, complete, parse_json_loose
from ..state import db

log = logging.getLogger("exo.engine")

PER_DAY_CAPS = {"searches": 25, "ai_prompts": 25, "pages": 30, "commits": 15}
MIN_TRAIL_EVENTS = 8   # below this, honestly report "not enough trail"

ANALYSIS_PROMPT = """You are the curriculum engine of my second brain. Below is my real
activity trail for the last {days} days: searches, prompts I sent to AI coding
tools, pages I read, code I shipped, and my own weekly outcome notes.

Your job:
1. RECURRING LOOKUPS: find concepts/APIs/techniques I looked up or asked AI
   about on multiple days. Repeatedly needing the same fundamental means it
   has not stuck.
2. DEPTH VS SURFACE: for each, judge from the trail whether I engaged deeply
   (several sources, follow-up questions) or copy-paste-level ("surface").
   Surface engagement on something recurring = a foundation gap.
3. EMERGING DOMAINS: sustained new-topic activity across several days.

Rules — these are hard constraints:
- Every gap MUST include evidence quotes that are VERBATIM substrings of the
  trail below (short fragments are fine). No quote, no claim.
- If the trail is too thin to conclude something, leave it out. NEVER invent
  facts, deadlines, or urgency that the trail does not show. It is always
  better to return fewer, well-evidenced gaps than to pad the list.
- Concept names: short, canonical, lowercase (e.g. "sqlite wal mode",
  "css grid", "spectral calibration").

Reply with ONLY this JSON:
{{"gaps": [{{"concept": str, "domain": str, "why": str (plain language, 1-2
sentences citing what I did), "evidence": [{{"day": "YYYY-MM-DD", "quote":
str}}], "engagement": "surface"|"deep", "score": 0.0-1.0 (how urgent to fix)}}],
"emerging_domains": [{{"name": str, "note": str, "days_active": int}}]}}

My trail:
{trail}"""

EXPLAINER_PROMPT = """Write short original explainers for these foundation-gap concepts,
teaching each in the context of what I was actually doing (my evidence trail
is quoted). For each: 80-140 words, plain language, teach the core mental
model (not a tutorial), and end with one check-in question that tests whether
I own the idea. If you are not confident what the concept refers to from the
evidence, say exactly that in the explainer instead of guessing.

Reply with ONLY JSON: {{"explainers": [{{"concept": str, "explainer": str,
"check_question": str}}]}}

Concepts and evidence:
{items}"""


# ------------------------------------------------------------ trail gathering

def gather_trail(cfg: Config, conn: sqlite3.Connection,
                 local_conn: sqlite3.Connection) -> dict:
    days = int(cfg.get("curriculum.trail_window_days", 14))
    since = db.now_ms() - days * 86400_000
    by_day: dict[str, dict] = defaultdict(
        lambda: {"searches": [], "ai_prompts": [], "pages": [], "commits": []}
    )

    def day_of(ts_ms: int) -> str:
        return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")

    for r in local_conn.execute(
        "SELECT ts, source, content, extra FROM life_stream"
        " WHERE ts >= ? AND sensitivity = 'normal'"
        " AND source IN ('search', 'ai_prompt', 'browser') ORDER BY ts",
        (since,),
    ):
        bucket = by_day[day_of(r["ts"])]
        if r["source"] == "search":
            bucket["searches"].append(r["content"][:120])
        elif r["source"] == "ai_prompt":
            bucket["ai_prompts"].append(r["content"][:200])
        else:
            try:
                title = json.loads(r["extra"]).get("title", "")
            except json.JSONDecodeError:
                title = ""
            host = urlparse(r["content"]).netloc
            if title or host:
                bucket["pages"].append(f"{title} ({host})"[:140] if title else host)

    for r in conn.execute(
        "SELECT ts, repo, message, files FROM code_activity WHERE ts >= ? ORDER BY ts",
        (since,),
    ):
        try:
            files = ", ".join(json.loads(r["files"])[:5])
        except json.JSONDecodeError:
            files = ""
        by_day[day_of(r["ts"])]["commits"].append(
            f"{r['message'][:100]} [{files}]"[:180]
        )

    outcomes = [
        (day_of(r["ts"]), r["raw_text"][:400])
        for r in conn.execute(
            "SELECT ts, raw_text FROM reflections WHERE ts >= ?"
            " AND insight_type = 'weekly_outcome' ORDER BY ts",
            (since,),
        )
    ]
    event_count = sum(
        len(v) for day in by_day.values() for v in day.values()
    ) + len(outcomes)
    return {"days": days, "by_day": dict(by_day), "outcomes": outcomes,
            "event_count": event_count}


def build_material(trail: dict) -> str:
    lines = []
    for day in sorted(trail["by_day"]):
        bucket = trail["by_day"][day]
        lines.append(f"## {day}")
        for kind, cap in PER_DAY_CAPS.items():
            items = list(dict.fromkeys(bucket.get(kind, [])))[:cap]  # dedupe, cap
            if items:
                lines.append(f"{kind}: " + " | ".join(items))
    if trail["outcomes"]:
        lines.append("## my weekly outcome notes")
        lines.extend(f"[{d}] {t}" for d, t in trail["outcomes"])
    return "\n".join(lines)


# ------------------------------------------------------------ validation

def validate_gaps(gaps: list[dict], material: str) -> tuple[list[dict], int]:
    """Evidence quotes must exist verbatim (case-insensitive) in the material.
    Citations that don't are removed; gaps left with no evidence are dropped.
    This is the anti-confabulation gate — it cannot be prompted away."""
    haystack = material.casefold()
    kept, dropped = [], 0
    for g in gaps:
        good = []
        for ev in g.get("evidence") or []:
            quote = str(ev.get("quote", "")).strip()
            if len(quote) >= 4 and quote.casefold() in haystack:
                good.append({"day": str(ev.get("day", ""))[:10], "quote": quote[:200]})
        if good and g.get("concept"):
            g["evidence"] = good
            kept.append(g)
        else:
            dropped += 1
            log.warning("engine: dropped ungrounded gap %r", g.get("concept"))
    return kept, dropped


# ------------------------------------------------------------ persistence

def _upsert_concept(conn: sqlite3.Connection, g: dict) -> tuple[int, bool]:
    """Returns (concept_id, is_relapse). A graduated concept that shows up
    again as a gap regresses — I clearly didn't own it after all."""
    now = db.now_ms()
    name = g["concept"].strip().casefold()[:80]
    score = max(0.0, min(1.0, float(g.get("score") or 0.5)))
    row = conn.execute("SELECT id, status FROM concepts WHERE name = ?", (name,)).fetchone()
    # NOTE: `why` is trail-derived prose (it quotes what I did) — it goes to
    # concept_evidence in LOCAL.DB, never into the synced concepts row.
    if row is None:
        cur = conn.execute(
            "INSERT INTO concepts (name, domain, status, gap_score, engagement,"
            " first_seen, last_seen) VALUES (?, ?, 'gap', ?, ?, ?, ?)",
            (name, (g.get("domain") or "").casefold()[:60] or None, score,
             g.get("engagement") or "", now, now),
        )
        return cur.lastrowid, False
    relapse = row["status"] == "graduated"
    conn.execute(
        "UPDATE concepts SET last_seen = ?, gap_score = MAX(gap_score, ?),"
        " engagement = ?,"
        " status = CASE WHEN status = 'graduated' THEN 'gap' ELSE status END,"
        " fumble_count = fumble_count + CASE WHEN status = 'graduated' THEN 1 ELSE 0 END"
        " WHERE id = ?",
        (now, score, g.get("engagement") or "", row["id"]),
    )
    return row["id"], relapse


def _write_results(cfg: Config, conn: sqlite3.Connection,
                   local_conn: sqlite3.Connection, gaps: list[dict],
                   domains: list[dict]) -> None:
    now = db.now_ms()
    for g in gaps:
        cid, relapse = _upsert_concept(conn, g)
        name = g["concept"].strip().casefold()[:80]
        local_conn.execute(
            "INSERT INTO concept_evidence (concept_name, ts, evidence, why)"
            " VALUES (?, ?, ?, ?)",
            (name, now, json.dumps([
                f"[{e['day']}] {e['quote']}" for e in g["evidence"]
            ]), (g.get("why") or "")[:500]),
        )
        # bump=False: the curriculum manages confidence itself — recurring
        # lookups mean I don't own it, so confidence goes DOWN
        node_id = upsert_node(conn, name, "concept", bump=False)
        set_node_confidence(conn, node_id, delta=-0.15 if relapse else -0.05)
        if g.get("domain"):
            dom_id = upsert_node(conn, str(g["domain"]).casefold()[:60], "domain")
            upsert_edge(conn, node_id, dom_id, "in_domain")
    for d in domains or []:
        if d.get("name"):
            upsert_node(conn, str(d["name"]).casefold()[:60], "domain")
    conn.commit()
    local_conn.commit()


def _generate_explainers(cfg: Config, conn: sqlite3.Connection,
                         local_conn: sqlite3.Connection, top_n: int = 3) -> int:
    rows = conn.execute(
        "SELECT id, name FROM concepts WHERE status IN ('gap', 'reviewing')"
        " AND explainer IS NULL ORDER BY gap_score DESC LIMIT ?", (top_n,),
    ).fetchall()
    if not rows:
        return 0
    items = []
    for r in rows:
        ev = local_conn.execute(
            "SELECT evidence FROM concept_evidence WHERE concept_name = ?"
            " ORDER BY ts DESC LIMIT 1", (r["name"],),
        ).fetchone()
        items.append({"concept": r["name"],
                      "evidence": json.loads(ev["evidence"]) if ev else []})
    try:
        reply = complete(
            cfg, conn, "T2_workhorse", "concept_explainers",
            [{"role": "user", "content": EXPLAINER_PROMPT.format(
                items=json.dumps(items, indent=1))}],
            max_tokens=4000,
        )
        data = parse_json_loose(reply)
    except (RouterError, ValueError, json.JSONDecodeError) as e:
        log.warning("explainer generation failed: %s", e)
        return 0
    n = 0
    by_name = {r["name"]: r["id"] for r in rows}
    for item in data.get("explainers") or []:
        cid = by_name.get(str(item.get("concept", "")).casefold())
        if cid and item.get("explainer"):
            conn.execute(
                "UPDATE concepts SET explainer = ?, check_question = ? WHERE id = ?",
                (item["explainer"][:1500], (item.get("check_question") or "")[:400], cid),
            )
            n += 1
    conn.commit()
    return n


# ------------------------------------------------------------ entry point

def run_engine(cfg: Config, conn: sqlite3.Connection,
               local_conn: sqlite3.Connection) -> str:
    trail = gather_trail(cfg, conn, local_conn)
    if trail["event_count"] < MIN_TRAIL_EVENTS:
        # honest absence beats confident noise
        return (f"not enough trail ({trail['event_count']} events in"
                f" {trail['days']}d) — no gap analysis run")
    material = build_material(trail)
    try:
        reply = complete(
            cfg, conn, "T2_workhorse", "concept_inference",
            [{"role": "user", "content": ANALYSIS_PROMPT.format(
                days=trail["days"], trail=material)}],
            max_tokens=6000,
        )
        data = parse_json_loose(reply)
    except (RouterError, ValueError, json.JSONDecodeError) as e:
        return f"engine failed: {e}"
    gaps, dropped = validate_gaps(list(data.get("gaps") or []), material)
    _write_results(cfg, conn, local_conn, gaps, list(data.get("emerging_domains") or []))
    explained = _generate_explainers(cfg, conn, local_conn)
    return (f"{len(gaps)} gaps written ({dropped} dropped as ungrounded),"
            f" {explained} explainers generated,"
            f" {len(data.get('emerging_domains') or [])} emerging domains")
