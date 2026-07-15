import sqlite3

from exocortex.state import db


def test_migrations_apply_and_are_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for expected in [
        "life_stream", "reflections", "skill_nodes", "skill_edges",
        "opportunities", "crm", "crm_touchpoints", "style_corpus",
        "usage_ledger", "memory", "briefs", "control", "spent_log",
        "schema_migrations",
    ]:
        assert expected in tables, expected
    assert db.apply_migrations(conn) == []  # nothing pending on second run
    conn.close()


def test_wal_mode(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_fts_stays_in_sync(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO life_stream (ts, device, source, content) VALUES (?, ?, ?, ?)",
        (db.now_ms(), "test", "window", "reading about spectroscopy methods"),
    )
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM life_stream_fts WHERE life_stream_fts MATCH 'spectroscopy'"
    ).fetchall()
    assert len(hits) == 1
    conn.execute("DELETE FROM life_stream WHERE id = ?", (hits[0]["rowid"],))
    conn.commit()
    assert (
        conn.execute(
            "SELECT count(*) FROM life_stream_fts WHERE life_stream_fts MATCH 'spectroscopy'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_control_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.get_control(conn, "work_mode", "off") == "off"
    db.set_control(conn, "work_mode", "on")
    assert db.get_control(conn, "work_mode") == "on"
    db.set_control(conn, "work_mode", "off")
    assert db.get_control(conn, "work_mode") == "off"
    conn.close()
