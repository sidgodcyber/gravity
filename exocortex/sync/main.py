"""Encrypted sync: the portable brain leaves the machine only encrypted.

Bundle = tar.gz of state/exocortex.db (WAL checkpointed first) + config.yaml,
encrypted to age format with a passphrase (pyrage — restorable anywhere with
standard age tooling: `age -d bundle.tar.gz.age`). Transport is rclone to any
remote you configure; with no remote, bundles land in state/sync-out/ for you
to move by hand.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from ..config import REPO_ROOT, Config
from ..state import db

log = logging.getLogger("exo.sync")

BUNDLE_PREFIX = "exocortex-"
BUNDLE_SUFFIX = ".tar.gz.age"


class SyncError(Exception):
    pass


def _passphrase(cfg: Config) -> str:
    p = (cfg.get("sync.passphrase") or "").strip()
    if not p:
        raise SyncError(
            "sync.passphrase is empty — set it (or its env var) in config.yaml."
        )
    return p


def _rclone() -> str | None:
    return shutil.which("rclone")


def _daemon_alive(cfg: Config) -> bool:
    from ..state.status import daemon_alive
    return daemon_alive(cfg)


def make_bundle(cfg: Config) -> bytes:
    """tar.gz(db snapshot + config.yaml) in memory. The db is snapshotted via
    the SQLite backup API so a running daemon can't tear the copy. Phase-1
    state stays well under available RAM; revisit past a few hundred MB."""
    src = db.connect(cfg.db_path)
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "exocortex.db"
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snap, arcname="state/exocortex.db")
            cfg_file = REPO_ROOT / "config.yaml"
            if cfg_file.exists():
                tar.add(cfg_file, arcname="config.yaml")
    return buf.getvalue()


def push(cfg: Config) -> str:
    import pyrage

    if cfg.is_spoke:
        # a spoke's exocortex.db is capture staging, not the brain — pushing
        # it as a bundle could later be restored over the canonical db
        raise SyncError(
            "this machine is a spoke — it holds no brain to push."
            " Spoke capture goes out via `exo sync push` batches only"
            " (this guard means the role routing broke; report it)."
        )
    # Plane guard at the bundling boundary: trail rows must not be in the
    # synced db (possible transiently during a Phase-1->2 upgrade with a
    # stale daemon). Split moves them; if any survive, refuse to bundle.
    db.split_plane_b(cfg)
    check = db.connect(cfg.db_path)
    leftover = check.execute(
        "SELECT count(*) FROM life_stream WHERE source != 'manual'"
    ).fetchone()[0]
    check.close()
    if leftover:
        raise SyncError(
            f"refusing to push: {leftover} trail rows still in the synced db"
            " (is an old pre-Phase-2 daemon still running? restart it first)"
        )

    encrypted = pyrage.passphrase.encrypt(make_bundle(cfg), _passphrase(cfg))
    name = BUNDLE_PREFIX + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + BUNDLE_SUFFIX
    outdir = cfg.state_dir / "sync-out"
    outdir.mkdir(parents=True, exist_ok=True)
    local = outdir / name
    local.write_bytes(encrypted)

    remote = (cfg.get("sync.remote") or "").strip()
    if not remote:
        return f"encrypted bundle written to {local} (no sync.remote configured)"
    rclone = _rclone()
    if not rclone:
        return (f"encrypted bundle at {local} — rclone not installed, could not"
                " upload. Install: https://rclone.org/install/")
    r = subprocess.run([rclone, "copy", str(local), remote], capture_output=True, text=True)
    if r.returncode != 0:
        raise SyncError(f"rclone copy failed: {r.stderr.strip()[:400]}")
    pruned = _prune_remote(rclone, remote, int(cfg.get("sync.keep_versions", 14)))
    return f"pushed {name} to {remote}" + (f" (pruned {pruned} old)" if pruned else "")


def _prune_remote(rclone: str, remote: str, keep: int) -> int:
    r = subprocess.run([rclone, "lsf", remote, "--files-only"], capture_output=True, text=True)
    if r.returncode != 0:
        return 0
    bundles = sorted(
        l.strip() for l in r.stdout.splitlines()
        if l.strip().startswith(BUNDLE_PREFIX) and l.strip().endswith(BUNDLE_SUFFIX)
    )
    n = 0
    for old in bundles[:-keep] if keep > 0 else []:
        if subprocess.run([rclone, "deletefile", f"{remote.rstrip('/')}/{old}"],
                          capture_output=True).returncode == 0:
            n += 1
    return n


def _newest_local(indir: Path) -> Path | None:
    bundles = sorted(indir.glob(f"{BUNDLE_PREFIX}*{BUNDLE_SUFFIX}"))
    return bundles[-1] if bundles else None


def pull(cfg: Config, force: bool = False) -> str:
    import pyrage

    if cfg.is_spoke:
        hub = cfg.get("hub_name") or "the hub"
        raise SyncError(
            f"this machine is a spoke — it never pulls state; the canonical"
            f" brain lives on {hub}. (To turn this machine into the hub,"
            f" set role: hub in config.yaml first.)"
        )
    indir = cfg.state_dir / "sync-in"
    indir.mkdir(parents=True, exist_ok=True)
    remote = (cfg.get("sync.remote") or "").strip()
    if remote:
        rclone = _rclone()
        if not rclone:
            raise SyncError("rclone not installed — https://rclone.org/install/")
        r = subprocess.run([rclone, "lsf", remote, "--files-only"], capture_output=True, text=True)
        if r.returncode != 0:
            raise SyncError(f"rclone lsf failed: {r.stderr.strip()[:400]}")
        bundles = sorted(
            l.strip() for l in r.stdout.splitlines()
            if l.strip().startswith(BUNDLE_PREFIX) and l.strip().endswith(BUNDLE_SUFFIX)
        )
        if not bundles:
            raise SyncError(f"no bundles found at {remote}")
        newest = bundles[-1]
        r = subprocess.run([rclone, "copy", f"{remote.rstrip('/')}/{newest}", str(indir)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SyncError(f"rclone copy failed: {r.stderr.strip()[:400]}")
        bundle_path = indir / newest
    else:
        bundle_path = _newest_local(indir)
        if not bundle_path:
            raise SyncError(f"no sync.remote configured and no bundle in {indir}")

    decrypted = pyrage.passphrase.decrypt(bundle_path.read_bytes(), _passphrase(cfg))

    restored = []
    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        if "state/exocortex.db" not in members:
            raise SyncError("bundle has no state/exocortex.db — refusing")
        if _daemon_alive(cfg):
            raise SyncError(
                "the capture daemon is running on this state dir — stop it"
                " before restoring, or the fresh db gets corrupted"
            )
        if cfg.db_path.exists():
            if not force:
                raise SyncError(
                    f"{cfg.db_path} already exists — re-run with --force to replace"
                    " (a timestamped .bak is kept)"
                )
            bak = cfg.db_path.with_suffix(f".db.bak-{int(time.time())}")
            shutil.copy2(cfg.db_path, bak)
            restored.append(f"existing db backed up to {bak.name}")
            for side in (".db-wal", ".db-shm"):
                p = cfg.db_path.with_suffix(side)
                if p.exists():
                    p.unlink()
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(members["state/exocortex.db"]) as src:
            cfg.db_path.write_bytes(src.read())
        restored.append(f"db restored from {bundle_path.name}")
        if "config.yaml" in members and not (REPO_ROOT / "config.yaml").exists():
            with tar.extractfile(members["config.yaml"]) as src:
                (REPO_ROOT / "config.yaml").write_bytes(src.read())
            restored.append("config.yaml restored (none existed here)")
    return "; ".join(restored)
