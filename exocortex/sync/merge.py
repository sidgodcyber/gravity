"""Hub-side merge of spoke capture batches. Insert-only, idempotent twice
over: a consumed batch_id is never reopened, and every event carries a
deterministic device-tagged uid that inserts at most once (spoke_events
ledger). Reflections land extraction_status='pending', so the hub's own
nightly extraction and skill-graph flow treat them exactly like local logs.

Plane guard on the RECEIVING side too: only reflections and code_activity
events are accepted — a tampered or buggy batch smuggling trail rows
(life_stream) is refused event-by-event and logged, never inserted.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from ..config import Config
from ..state import db
from .main import _passphrase, _rclone
from .spoke import QUEUE_TABLES

log = logging.getLogger("exo.merge")

REFLECTION_KEYS = {"ts", "raw_text"}
CODE_KEYS = {"ts", "repo", "commit_hash", "message", "files_changed",
             "insertions", "deletions", "files"}
_TS_FLOOR = 946_684_800_000  # 2000-01-01: anything earlier is a broken clock


def _sane_ts(value) -> int:
    """Epoch-ms within a plausible window, or ValueError (refused event).
    An absurd ts would merge fine and then crash every /recent and /source
    render — unrecoverable without manual SQL, so refuse at the boundary."""
    ts = int(value)
    if not (_TS_FLOOR <= ts <= db.now_ms() + 7 * 86400_000):
        raise ValueError(f"implausible event ts {ts}")
    return ts


def inbox_dir(cfg: Config) -> Path:
    return cfg.state_dir / "sync-in" / "spokes"


def fetch_batches(cfg: Config) -> int:
    """Copy new batch files from <remote>/spokes/** into the local inbox.
    Returns how many were fetched; 0 with no remote/rclone (manual mode:
    the user drops files into state/sync-in/spokes/ themselves)."""
    remote = (cfg.get("sync.remote") or "").strip()
    rclone = _rclone()
    if not remote or not rclone:
        return 0
    spokes = f"{remote.rstrip('/')}/spokes"
    r = subprocess.run([rclone, "lsf", spokes, "--files-only", "-R"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("merge: rclone lsf failed: %s", r.stderr.strip()[:300])
        return 0
    indir = inbox_dir(cfg)
    indir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for line in r.stdout.splitlines():
        rel = line.strip()
        if not rel.endswith(".json.age") or "/merged/" in rel or "merged/" == rel[:7]:
            continue
        name = rel.rsplit("/", 1)[-1]
        if ((indir / name).exists() or (indir / "merged" / name).exists()
                or (indir / "failed" / name).exists()):
            continue  # never re-fetch what we've already consumed or failed
        c = subprocess.run([rclone, "copy", f"{spokes}/{rel}", str(indir)],
                           capture_output=True, text=True)
        if c.returncode == 0:
            fetched += 1
        else:
            log.warning("merge: fetch failed for %s: %s", rel, c.stderr.strip()[:200])
    return fetched


def _consume_remote(cfg: Config, name: str) -> None:
    """Best-effort: park the merged batch under spokes/merged/ on the remote
    so it's never re-fetched. Local ledgers already guarantee idempotency."""
    remote = (cfg.get("sync.remote") or "").strip()
    rclone = _rclone()
    if not remote or not rclone:
        return
    spokes = f"{remote.rstrip('/')}/spokes"
    r = subprocess.run([rclone, "lsf", spokes, "--files-only", "-R"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return
    for line in r.stdout.splitlines():
        rel = line.strip()
        if rel.rsplit("/", 1)[-1] == name and "merged/" not in rel:
            subprocess.run([rclone, "moveto", f"{spokes}/{rel}",
                            f"{spokes}/merged/{name}"], capture_output=True)
            return


def _insert_event(conn, device: str, ev: dict) -> bool:
    """Insert one validated event. Returns True if a row was added."""
    tbl, row = ev["table"], ev["row"]
    if tbl == "reflections":
        if not isinstance(row, dict) or not REFLECTION_KEYS <= set(row) \
                or not str(row["raw_text"]).strip():
            raise ValueError("malformed reflection event")
        conn.execute(
            "INSERT INTO reflections (ts, device, raw_text, extraction_status)"
            " VALUES (?, ?, ?, 'pending')",
            # cap: this text goes verbatim into LLM extraction prompts later
            (_sane_ts(row["ts"]), device, str(row["raw_text"])[:10000]))
        return True
    if tbl == "code_activity":
        if not isinstance(row, dict) or not CODE_KEYS <= set(row):
            raise ValueError("malformed code_activity event")
        cur = conn.execute(
            "INSERT OR IGNORE INTO code_activity (ts, repo, commit_hash,"
            " message, files_changed, insertions, deletions, files)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_sane_ts(row["ts"]), str(row["repo"]), str(row["commit_hash"]),
             str(row["message"])[:500], int(row["files_changed"]),
             int(row["insertions"]), int(row["deletions"]),
             json.dumps(json.loads(row["files"]))[:8000]
             if isinstance(row["files"], str) else json.dumps(row["files"])[:8000]))
        return cur.rowcount > 0
    raise ValueError(f"refused event table {tbl!r}")  # Plane guard


def merge_batches(cfg: Config) -> dict:
    """Fetch (when a remote exists) and merge every pending batch file.
    Safe to re-run any number of times; a bad FILE can never block the good
    files behind it (it lands in failed/ and the loop continues)."""
    import pyrage

    # a missing passphrase is a CONFIG problem, not a bad batch: abort before
    # touching any file, so good batches stay pending instead of being
    # misfiled into failed/ (e.g. env var absent in the scheduled task)
    passphrase = _passphrase(cfg)

    fetched = fetch_batches(cfg)
    indir = inbox_dir(cfg)
    indir.mkdir(parents=True, exist_ok=True)
    merged_dir = indir / "merged"
    failed_dir = indir / "failed"

    def _fail(f, why):
        log.error("merge: %s — moving %s to failed/", why, f.name)
        failed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), failed_dir / f.name)
        stats["failed"] += 1

    conn = db.connect(cfg.db_path)
    stats = {"fetched": fetched, "batches": 0, "already": 0, "events": 0,
             "new_rows": 0, "refused": 0, "failed": 0}
    try:
        for f in sorted(indir.glob("batch-*.json.age")):
            try:
                if f.stat().st_size > 64 * 1024 * 1024:
                    _fail(f, "batch exceeds 64MB")
                    continue
                payload = pyrage.passphrase.decrypt(f.read_bytes(), passphrase)
                batch = json.loads(payload)
                batch_id = str(batch["batch_id"])
                device = str(batch["device"]).strip() or "unknown-device"
                events = batch["events"]
                assert isinstance(events, list)
            except Exception as e:
                _fail(f, f"cannot read ({e})")
                continue

            if device == cfg.device_name:
                # a "spoke" claiming the hub's own name is a config mistake
                # (or uid squatting) — refuse loudly rather than fake local rows
                _fail(f, f"batch claims THIS machine's name ({device!r}) —"
                         " give the spoke a unique device_name")
                continue

            if conn.execute("SELECT 1 FROM spoke_batches WHERE batch_id = ?",
                            (batch_id,)).fetchone():
                stats["already"] += 1
                merged_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), merged_dir / f.name)
                _consume_remote(cfg, f.name)
                continue

            now = db.now_ms()
            new_rows = 0
            try:
                for ev in events:
                    if not isinstance(ev, dict) or not str(ev.get("uid") or ""):
                        log.error("merge: refused shapeless event from %s", device)
                        stats["refused"] += 1
                        continue
                    uid = str(ev["uid"])
                    if conn.execute("SELECT 1 FROM spoke_events WHERE event_uid = ?",
                                    (uid,)).fetchone():
                        continue
                    if ev.get("table") not in QUEUE_TABLES:
                        log.error("merge: REFUSED event table %r from %s"
                                  " (plane guard)", ev.get("table"), device)
                        stats["refused"] += 1
                        continue
                    try:
                        if _insert_event(conn, device, ev):
                            new_rows += 1
                    except (ValueError, KeyError, TypeError) as e:
                        log.error("merge: refused malformed event from %s: %s",
                                  device, e)
                        stats["refused"] += 1
                        continue
                    conn.execute("INSERT INTO spoke_events (event_uid, merged_ts)"
                                 " VALUES (?, ?)", (uid, now))
                try:
                    created = int(batch.get("created_ts") or now)
                except (TypeError, ValueError):
                    created = now
                conn.execute(
                    "INSERT INTO spoke_batches (batch_id, device, created_ts,"
                    " merged_ts, n_events, n_new) VALUES (?, ?, ?, ?, ?, ?)",
                    (batch_id, device, created, now, len(events), new_rows))
                conn.commit()
            except Exception:
                # contain the blast radius to THIS file: without this, one
                # malformed-but-decryptable batch wedged every merge (and
                # every nightly) forever, silently
                conn.rollback()
                log.exception("merge: batch %s blew up mid-merge", f.name)
                _fail(f, "unexpected error mid-merge (rolled back)")
                continue
            stats["batches"] += 1
            stats["events"] += len(events)
            stats["new_rows"] += new_rows
            merged_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), merged_dir / f.name)
            _consume_remote(cfg, f.name)
    finally:
        conn.close()
    return stats


def merge_summary(stats: dict) -> str:
    bits = [f"{stats['batches']} batch(es) merged, {stats['new_rows']} new rows"]
    if stats["fetched"]:
        bits.append(f"{stats['fetched']} fetched from remote")
    if stats["already"]:
        bits.append(f"{stats['already']} already merged (skipped)")
    if stats["refused"]:
        bits.append(f"{stats['refused']} events REFUSED (see logs)")
    if stats["failed"]:
        bits.append(f"{stats['failed']} unreadable file(s) moved to failed/")
    return "; ".join(bits)


def devices_report(cfg: Config) -> str:
    """Who's feeding the brain: this hub + every spoke ever heard from,
    with last-batch times and pending unmerged work."""
    import datetime as dt

    from ..state.status import daemon_alive

    conn = db.connect(cfg.db_path)
    try:
        lines = [f"🖥 {cfg.device_name} (hub, this machine) — daemon "
                 + ("running" if daemon_alive(cfg) else "NOT RUNNING")]
        rows = conn.execute(
            "SELECT device, count(*) AS batches, sum(n_new) AS rows_added,"
            " max(created_ts) AS last_built, max(merged_ts) AS last_merged"
            " FROM spoke_batches GROUP BY device ORDER BY device").fetchall()
        for r in rows:
            try:
                built = (f"{dt.datetime.fromtimestamp(r['last_built'] / 1000):%a %b %d %H:%M}")
            except (OverflowError, OSError, ValueError):
                built = "unknown time"
            lines.append(
                f"📡 {r['device']} (spoke) — last batch {built},"
                f" {r['batches']} batches / {r['rows_added'] or 0} rows total")
        if not rows:
            lines.append("no spoke batches received yet")
        pending = sorted(inbox_dir(cfg).glob("batch-*.json.age"))
        if pending:
            lines.append(f"⏳ {len(pending)} batch(es) awaiting merge"
                         " (next nightly, or `exo sync merge`)")
        remote = (cfg.get("sync.remote") or "").strip()
        if remote and _rclone():
            r = subprocess.run([_rclone(), "lsf", f"{remote.rstrip('/')}/spokes",
                                "--files-only", "-R"], capture_output=True, text=True)
            if r.returncode == 0:
                waiting = [l for l in r.stdout.splitlines()
                           if l.strip().endswith(".json.age") and "merged/" not in l]
                if waiting:
                    lines.append(f"☁ {len(waiting)} batch(es) on the remote not"
                                 " yet fetched")
        return "\n".join(lines)
    finally:
        conn.close()
