"""Skill-graph upserts (plain tables, no graph db)."""

from __future__ import annotations

import json
import sqlite3

from ..state.db import now_ms

CONFIDENCE_STEP = 0.1


def upsert_node(conn: sqlite3.Connection, name: str, node_type: str,
                bump: bool = True) -> int:
    """bump=True (extraction flow): touching a node is evidence of use, so
    confidence rises a step. bump=False (curriculum flow): the caller manages
    confidence explicitly via set_node_confidence — an automatic bump would
    cancel the fumble/re-lookup penalties."""
    name = name.strip()
    ts = now_ms()
    conn.execute(
        "INSERT INTO skill_nodes (name, type, created_ts, updated_ts, confidence)"
        " VALUES (?, ?, ?, ?, 0.5)"
        " ON CONFLICT(name, type) DO UPDATE SET"
        "   updated_ts = excluded.updated_ts,"
        "   confidence = MIN(1.0, confidence + ?)",
        (name, node_type, ts, ts, CONFIDENCE_STEP if bump else 0.0),
    )
    return conn.execute(
        "SELECT id FROM skill_nodes WHERE name = ? AND type = ?", (name, node_type)
    ).fetchone()["id"]


def set_node_confidence(conn: sqlite3.Connection, node_id: int,
                        delta: float = 0.0, floor: float = 0.05) -> None:
    """Nudge a node's confidence up (mastery demonstrated) or down (still
    re-looking it up). Clamped to [floor, 1.0]."""
    conn.execute(
        "UPDATE skill_nodes SET confidence = MIN(1.0, MAX(?, confidence + ?)),"
        " updated_ts = ? WHERE id = ?",
        (floor, delta, now_ms(), node_id),
    )


def upsert_edge(conn: sqlite3.Connection, src: int, dst: int, rel: str,
                evidence: dict | None = None) -> None:
    ts = now_ms()
    conn.execute(
        "INSERT INTO skill_edges (src, dst, rel, weight, created_ts, updated_ts, evidence)"
        " VALUES (?, ?, ?, 0.5, ?, ?, ?)"
        " ON CONFLICT(src, dst, rel) DO UPDATE SET"
        "   weight = MIN(1.0, weight + ?), updated_ts = excluded.updated_ts",
        (src, dst, rel, ts, ts, json.dumps([evidence] if evidence else []), CONFIDENCE_STEP),
    )
    if evidence:
        row = conn.execute(
            "SELECT id, evidence FROM skill_edges WHERE src = ? AND dst = ? AND rel = ?",
            (src, dst, rel),
        ).fetchone()
        try:
            ev = json.loads(row["evidence"] or "[]")
        except json.JSONDecodeError:
            ev = []
        if evidence not in ev:
            ev.append(evidence)
            conn.execute(
                "UPDATE skill_edges SET evidence = ? WHERE id = ?",
                (json.dumps(ev[-20:]), row["id"]),
            )
