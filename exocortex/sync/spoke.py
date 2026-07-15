"""Spoke-side outbound queue: capture leaves a spoke ONLY as encrypted,
append-only batch files.

The spoke's state/exocortex.db is a STAGING db, never pushed as a brain:
`exo log` reflections and allowlisted git commits accumulate there, and
`build_batch` packages rows past a checkpoint into a JSON batch encrypted
with the shared sync passphrase. Event uids are deterministic and
device-tagged ("desktop/reflections/12"), so the same staged row can never
merge twice on the hub even if it lands in two batches.

Plane B is structurally excluded: this module only ever SELECTs from
`reflections` and `code_activity`. The trail (life_stream, local.db) has no
code path into a batch — see tests/test_multidevice.py's adversarial cases.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from ..config import Config
from ..state import db
from .main import SyncError, _passphrase, _rclone

log = logging.getLogger("exo.spoke")

# the ONLY tables allowed into an outbound batch (Plane A + user reflections)
QUEUE_TABLES = ("reflections", "code_activity")


def queue_dir(cfg: Config) -> Path:
    return cfg.state_dir / "queue"


def _remote_spoke_dir(cfg: Config) -> str | None:
    remote = (cfg.get("sync.remote") or "").strip()
    if not remote:
        return None
    return f"{remote.rstrip('/')}/spokes/{cfg.device_name}"


def build_batch(cfg: Config) -> Path | None:
    """Stage new capture into an encrypted batch file. Returns None when
    there is nothing new. Checkpoints advance only after the file is safely
    on disk."""
    import pyrage

    conn = db.connect(cfg.db_path)
    try:
        try:
            from ..daemon.sources.git_activity import scan_repos
            scan_repos(cfg, conn)
        except Exception:
            log.exception("spoke: git scan failed; batching what we have")

        refl_ckpt = int(db.get_control(conn, "queue_ckpt_reflections", "0") or 0)
        code_ckpt = int(db.get_control(conn, "queue_ckpt_code", "0") or 0)
        device = cfg.device_name
        events = []
        max_refl, max_code = refl_ckpt, code_ckpt
        for r in conn.execute(
                "SELECT id, ts, raw_text FROM reflections WHERE id > ?"
                " ORDER BY id", (refl_ckpt,)):
            events.append({
                # ts in the uid: a reset staging db restarts ids at 1, but
                # fresh rows get fresh timestamps, so their uids can't
                # collide with already-ledgered ones on the hub
                "uid": f"{device}/reflections/{r['id']}@{r['ts']}",
                "table": "reflections",
                "row": {"ts": r["ts"], "raw_text": r["raw_text"]},
            })
            max_refl = max(max_refl, r["id"])
        for r in conn.execute(
                "SELECT id, ts, repo, commit_hash, message, files_changed,"
                " insertions, deletions, files FROM code_activity WHERE id > ?"
                " ORDER BY id", (code_ckpt,)):
            events.append({
                "uid": f"{device}/code/{r['repo']}/{r['commit_hash']}",
                "table": "code_activity",
                "row": {k: r[k] for k in ("ts", "repo", "commit_hash", "message",
                                          "files_changed", "insertions",
                                          "deletions", "files")},
            })
            max_code = max(max_code, r["id"])
        if not events:
            return None
        assert all(e["table"] in QUEUE_TABLES for e in events)

        batch = {
            "batch_id": str(uuid.uuid4()),
            "device": device,
            "created_ts": db.now_ms(),
            "events": events,
        }
        payload = json.dumps(batch).encode("utf-8")
        encrypted = pyrage.passphrase.encrypt(payload, _passphrase(cfg))
        pending = queue_dir(cfg) / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        safe_dev = "".join(c if c.isalnum() or c in "-_" else "-" for c in device)
        name = (f"batch-{safe_dev}-"
                f"{dt.datetime.now():%Y%m%d-%H%M%S}-{batch['batch_id'][:8]}.json.age")
        out = pending / name
        out.write_bytes(encrypted)

        # checkpoints advance to the max id actually BATCHED (a row committed
        # by a concurrent writer after our SELECT must not be skipped)
        db.set_control(conn, "queue_ckpt_reflections", str(max_refl))
        db.set_control(conn, "queue_ckpt_code", str(max_code))
        log.info("spoke: batched %d events into %s", len(events), name)
        return out
    finally:
        conn.close()


def push_queue(cfg: Config) -> str:
    """Build a batch from anything new, then upload every pending batch to
    <sync.remote>/spokes/<device>/. Without a remote, files stay in
    state/queue/pending/ for a manual move (merges are idempotent, so a
    stale copy left behind is harmless)."""
    built = build_batch(cfg)
    pending = queue_dir(cfg) / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    files = sorted(pending.glob("batch-*.json.age"))
    if not files:
        return "queue empty — nothing new to push"

    remote = _remote_spoke_dir(cfg)
    if not remote:
        return (f"{len(files)} batch(es) waiting in {pending}\n"
                "no sync.remote configured — move them to the hub's"
                " state/sync-in/spokes/ and run `exo sync merge` there")
    rclone = _rclone()
    if not rclone:
        return (f"{len(files)} batch(es) waiting in {pending} — rclone not"
                " installed, could not upload. https://rclone.org/install/")
    sent_dir = queue_dir(cfg) / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True)
    sent = 0
    for f in files:
        r = subprocess.run([rclone, "copy", str(f), remote],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SyncError(f"rclone copy failed for {f.name}:"
                            f" {r.stderr.strip()[:400]}")
        shutil.move(str(f), sent_dir / f.name)
        sent += 1
    note = f" (+1 new batch built)" if built else ""
    return f"pushed {sent} batch(es) to {remote}{note}"
