"""Plane A capture: git activity from allowlisted personal repos only.

Runs from the nightly cycle (not the daemon loop — keeps the daemon lean).
Reads commit metadata (hash, time, message, per-file stats) via `git log`;
no file contents are read in Phase 2. Every repo root is validated through
the allowlist guard before git is invoked, so paths outside
capture_code_from — including via symlinks — are never touched.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
from sqlite3 import Connection

from ...config import Config
from ...state import db
from ..allowlist import (AllowlistViolation, assert_allowed,
                         device_code_paths, resolved_roots)

log = logging.getLogger("exo.git")

FIELD_SEP = "\x1f"
COMMIT_SEP = "\x1e"


def scan_repos(cfg: Config, conn: Connection) -> int:
    """Import new commits from every allowlisted repo. Returns commits added."""
    roots = resolved_roots(cfg)
    added = 0
    for raw in device_code_paths(cfg):
        try:
            repo = assert_allowed(raw, roots)
        except AllowlistViolation:
            log.warning("skipping unresolvable capture_code_from entry: %s", raw)
            continue
        if not (repo / ".git").exists():
            continue  # allowlisted but not a git repo — nothing to scan
        key = f"git_ckpt_{repo.as_posix().casefold()}"
        since_ms = int(db.get_control(conn, key, "0") or 0)
        try:
            added += _import_repo(conn, repo, since_ms)
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.warning("git scan failed for %s: %s", repo, e)
            continue
        db.set_control(conn, key, str(db.now_ms()))
    return added


def _import_repo(conn: Connection, repo, since_ms: int) -> int:
    args = [
        "git", "-C", str(repo), "--no-pager", "log",
        f"--pretty=format:{COMMIT_SEP}%H{FIELD_SEP}%at{FIELD_SEP}%s",
        "--numstat",
    ]
    if since_ms:
        since = dt.datetime.fromtimestamp(since_ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")
        args.append(f"--since={since}")
    r = subprocess.run(args, capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log.warning("git log failed for %s: %s", repo, r.stderr.strip()[:200])
        return 0
    added = 0
    for chunk in r.stdout.split(COMMIT_SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, *stat_lines = chunk.splitlines()
        try:
            commit_hash, unix_s, message = head.split(FIELD_SEP, 2)
        except ValueError:
            continue
        files, ins, dels = [], 0, 0
        for line in stat_lines:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, d, path = parts
            files.append(path)
            ins += int(a) if a.isdigit() else 0
            dels += int(d) if d.isdigit() else 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO code_activity"
            " (ts, repo, commit_hash, message, files_changed, insertions, deletions, files)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(unix_s) * 1000, repo.as_posix(), commit_hash, message[:500],
             len(files), ins, dels, json.dumps(files[:50])),
        )
        added += cur.rowcount
    conn.commit()
    return added
