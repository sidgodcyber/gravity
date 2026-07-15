"""The one user-facing ID namespace.

Rule: any number shown to the user is a uid from this registry, and a uid
resolves to exactly the row it was minted for, forever. Internal table ids
(tasks.id, inbox.id, ...) never appear in user-facing text.
"""

from __future__ import annotations

import sqlite3

USER_TABLES = ("tasks", "inbox", "reflections", "crm_notes")


def uid_for(conn: sqlite3.Connection, tbl: str, row_id: int) -> int:
    """Get (minting if needed) the user-facing id for a row."""
    assert tbl in USER_TABLES, tbl
    row = conn.execute("SELECT uid FROM uids WHERE tbl = ? AND row_id = ?",
                       (tbl, row_id)).fetchone()
    if row:
        return row["uid"]
    cur = conn.execute("INSERT INTO uids (tbl, row_id) VALUES (?, ?)", (tbl, row_id))
    conn.commit()
    return cur.lastrowid


def resolve(conn: sqlite3.Connection, uid: int) -> tuple[str, int] | None:
    row = conn.execute("SELECT tbl, row_id FROM uids WHERE uid = ?", (uid,)).fetchone()
    return (row["tbl"], row["row_id"]) if row else None


def release(conn: sqlite3.Connection, tbl: str, row_id: int) -> None:
    """Forget a deleted row's uid. The uid number is never reused
    (AUTOINCREMENT), so a stale reference can only miss, never hit new data."""
    conn.execute("DELETE FROM uids WHERE tbl = ? AND row_id = ?", (tbl, row_id))
    conn.commit()


def backfill(conn: sqlite3.Connection) -> int:
    """Mint uids for all pre-existing rows, in creation order across tables,
    so older records get lower numbers. Idempotent."""
    rows: list[tuple[int, str, int]] = []
    for tbl, ts_col in (("inbox", "ts"), ("tasks", "created_ts"),
                        ("reflections", "ts"), ("crm_notes", "ts")):
        for r in conn.execute(f"SELECT id, {ts_col} AS ts FROM {tbl}"):
            rows.append((r["ts"], tbl, r["id"]))
    rows.sort()
    n = 0
    for _, tbl, row_id in rows:
        cur = conn.execute("INSERT OR IGNORE INTO uids (tbl, row_id) VALUES (?, ?)",
                           (tbl, row_id))
        n += cur.rowcount
    conn.commit()
    return n
