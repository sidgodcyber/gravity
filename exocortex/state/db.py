"""SQLite state store: one file, WAL mode, numbered-SQL-file migrations."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LOCAL_MIGRATIONS_DIR = Path(__file__).parent / "migrations_local"


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(db_path: str | Path, *, migrate: bool = True,
            migrations_dir: Path = MIGRATIONS_DIR) -> sqlite3.Connection:
    """Open (creating if needed) a state db. Each thread opens its own
    connection; a connection must not be shared across threads."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if migrate:
        apply_migrations(conn, migrations_dir)
    return conn


def local_connect(cfg) -> sqlite3.Connection:
    """Open the LOCAL-ONLY db (Plane B trail). Never enters sync bundles."""
    return connect(cfg.state_dir / "local.db", migrations_dir=LOCAL_MIGRATIONS_DIR)


def applied_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_ts INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def pending_migrations(conn: sqlite3.Connection,
                       migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    current = applied_version(conn)
    out = []
    for path in sorted(migrations_dir.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version > current:
            out.append(path)
    return out


def apply_migrations(conn: sqlite3.Connection,
                     migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending numbered migrations in order. executescript runs in
    autocommit, so a failing migration can leave partial DDL — acceptable for
    additive migrations; write destructive ones as their own guarded scripts."""
    applied = []
    for path in pending_migrations(conn, migrations_dir):
        version = int(path.name.split("_", 1)[0])
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_ts) VALUES (?, ?, ?)",
            (version, path.name, now_ms()),
        )
        conn.commit()
        applied.append(path.name)
    return applied


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the WAL into the main db file (used before sync bundling)."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def get_control(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_control(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO control (key, value, updated_ts) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
        (key, value, now_ms()),
    )
    conn.commit()


def split_plane_b(cfg) -> int:
    """One-time (idempotent) move of Plane B rows out of the synced db into
    local.db. Everything captured ambiently (window/clipboard/file/browser)
    is trail data and must never sync; only deliberate manual entries stay.
    Rows are inserted into local.db and deleted from the synced db only after
    the insert count is verified. Returns rows moved."""
    main = connect(cfg.db_path)
    try:
        n = main.execute(
            "SELECT count(*) FROM life_stream WHERE source != 'manual'"
        ).fetchone()[0]
        if not n:
            return 0
        local = local_connect(cfg)  # ensure file + schema exist
        local.close()
        local_path = str(cfg.state_dir / "local.db")
        main.execute("ATTACH DATABASE ? AS localdb", (local_path,))
        pre = main.execute("SELECT count(*) FROM localdb.life_stream").fetchone()[0]
        main.execute(
            "INSERT INTO localdb.life_stream"
            " (ts, device, source, kind, content, app, sensitivity, extra)"
            " SELECT ts, device, source, kind, content, app, sensitivity, extra"
            " FROM life_stream WHERE source != 'manual'"
        )
        post = main.execute("SELECT count(*) FROM localdb.life_stream").fetchone()[0]
        if post - pre != n:
            main.rollback()
            raise RuntimeError(f"plane split aborted: {post - pre} of {n} rows landed")
        main.execute("DELETE FROM life_stream WHERE source != 'manual'")
        main.commit()
        # capture checkpoints are machine-local — move them too
        for row in main.execute(
            "SELECT key, value, updated_ts FROM control WHERE key LIKE 'browser_ckpt_%'"
            " OR key = 'browser_import_last' OR key = 'ai_trail_ckpt_ms'"
        ).fetchall():
            main.execute(
                "INSERT INTO localdb.control (key, value, updated_ts) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_ts = excluded.updated_ts",
                (row["key"], row["value"], row["updated_ts"]),
            )
            main.execute("DELETE FROM control WHERE key = ?", (row["key"],))
        main.commit()
        main.execute("DETACH DATABASE localdb")
        return n
    finally:
        main.close()


def set_work_mode(conn: sqlite3.Connection, on: bool) -> None:
    """The one way to flip work mode. Turning it OFF also raises the
    browser-history visit-time floor: anything browsed while work mode was on
    must never be imported later, so imports resume from 'now'."""
    set_control(conn, "work_mode", "on" if on else "off")
    if not on:
        set_control(conn, "browser_floor_us", str(int(time.time() * 1_000_000)))
