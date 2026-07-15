"""/ask — retrieval-augmented answers over the state store.

Retrieval is FTS5 (no model load, so the bot stays light); the answer is
composed by T2 with T1 fallback when the budget governor says no.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from ..config import Config
from ..router.llm import BudgetExceeded, RouterError, complete
from ..state import db
from .embeddings import fts_terms

SNIPPET_LIMIT = 12


def _fmt_ts(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def gather_context(conn: sqlite3.Connection, question: str,
                   local_conn: sqlite3.Connection | None = None) -> list[str]:
    """conn = synced db (reflections, concepts); local_conn = Plane B trail
    (life_stream). The trail lives in local.db as of Phase 2."""
    terms = fts_terms(question)
    chunks: list[str] = []
    seen_reflections: set[int] = set()
    trail = local_conn if local_conn is not None else conn
    if terms:
        for r in trail.execute(
            "SELECT ls.ts, ls.source, ls.content FROM life_stream_fts f"
            " JOIN life_stream ls ON ls.id = f.rowid"
            " WHERE life_stream_fts MATCH ? AND ls.sensitivity = 'normal'"
            " ORDER BY rank LIMIT ?",
            (terms, SNIPPET_LIMIT),
        ):
            chunks.append(f"[{_fmt_ts(r['ts'])} {r['source']}] {r['content'][:300]}")
        for r in conn.execute(
            "SELECT rf.id, rf.ts, rf.raw_text, rf.skills, rf.domain, rf.project"
            " FROM reflections_fts f"
            " JOIN reflections rf ON rf.id = f.rowid"
            " WHERE reflections_fts MATCH ? ORDER BY rank LIMIT 6",
            (terms,),
        ):
            seen_reflections.add(r["id"])
            chunks.append(_reflection_line(r))
    # always include the most recent reflections — questions are usually about
    # recent life even when keywords don't match
    for r in conn.execute(
        "SELECT id, ts, raw_text, skills, domain, project FROM reflections"
        " ORDER BY ts DESC LIMIT 3"
    ):
        if r["id"] not in seen_reflections:
            seen_reflections.add(r["id"])
            chunks.append(_reflection_line(r))
    return chunks


def _reflection_line(r) -> str:
    tags = " ".join(
        f"{k}={r[k]}" for k in ("domain", "project") if r[k]
    )
    skills = f" skills={r['skills']}" if r["skills"] else ""
    return (f"[{_fmt_ts(r['ts'])} my own /log entry] {r['raw_text'][:400]}"
            + (f" ({tags}{skills})" if tags or skills else ""))


def answer(cfg: Config, question: str) -> str:
    conn = db.connect(cfg.db_path)
    local_conn = db.local_connect(cfg)
    try:
        chunks = gather_context(conn, question, local_conn)
        if not chunks:
            return ("I found nothing relevant in your state store yet. "
                    "The daemon may need more capture time, or try other words.")
        context = "\n".join(chunks)
        messages = [
            {"role": "system", "content": (
                "You are Exocortex, the user's second brain. Answer their"
                " question by SYNTHESIZING the context — never quote or"
                " parrot context lines back. Rephrase in your own words,"
                " combine related items, and answer the actual question"
                " (who/what/when/why), adding useful structure like a"
                " short list when several things are involved. Cite dates"
                " inline like (2026-07-04)."
                " GROUNDING (hard rule): use ONLY facts present in the"
                " context. If the context does not answer the question, say"
                " plainly 'I don't have anything on that' and stop. Never"
                " infer a deadline, urgency, commitment, or date that is not"
                " explicitly in the context — no fabricated 'due dates' or"
                " 'X days left'. Absence stated plainly beats a confident"
                " guess. Keep it under 150 words unless the question demands"
                " more."
            )},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
        ]
        try:
            return complete(cfg, conn, "T2_workhorse", "ask", messages, max_tokens=1600)
        except BudgetExceeded:
            note = "(budget cap: answered on free tier)\n"
        except RouterError:
            note = "(workhorse tier unavailable: answered on free tier)\n"
        try:
            return note + complete(cfg, conn, "T1_free", "ask", messages, max_tokens=1600)
        except RouterError as e:
            return f"Could not reach any model: {e}"
    finally:
        local_conn.close()
        conn.close()
