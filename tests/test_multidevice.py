"""Multi-device hub & spoke: role gating (the structural one-bot rule),
per-device Plane A allowlists, Plane B exclusion from outbound batches, and
idempotent hub merges. The adversarial cases here are the spec's must-pass
bar — do not weaken."""

import json
import subprocess
import time

import pytest

from exocortex.config import Config
from exocortex.daemon.allowlist import device_code_paths, resolved_roots
from exocortex.extract.reflections import add_reflection
from exocortex.state import db
from exocortex.sync.merge import inbox_dir, merge_batches
from exocortex.sync.spoke import build_batch, push_queue, queue_dir

PASSPHRASE = "test-passphrase-for-batches"


def base_cfg(tmp_path, name, role, state):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / state)
    c.data["role"] = role
    c.data["device_name"] = name
    c.data["hub_name"] = "laptop"
    c.data["sync"]["passphrase"] = PASSPHRASE
    c.data["sync"]["remote"] = ""  # manual transport in tests
    return c


def spoke_cfg(tmp_path):
    return base_cfg(tmp_path, "desktop", "spoke", "spoke-state")


def hub_cfg(tmp_path):
    return base_cfg(tmp_path, "laptop", "hub", "hub-state")


def deliver(spoke, hub):
    """The manual transport: move spoke batches into the hub's inbox."""
    indir = inbox_dir(hub)
    indir.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in sorted((queue_dir(spoke) / "pending").glob("*.age")):
        target = indir / f.name
        target.write_bytes(f.read_bytes())
        moved.append(target)
    return moved


# ------------------------------------------------------------ role gating

def test_spoke_refuses_to_start_the_bot(tmp_path):
    from exocortex.bot.main import RoleError, run_bot
    cfg = spoke_cfg(tmp_path)
    cfg.data["telegram"]["bot_token"] = "123:fake"  # even with a token
    with pytest.raises(RoleError) as e:
        run_bot(cfg)
    assert "spoke" in str(e.value) and "laptop" in str(e.value)


def test_cli_gate_blocks_hub_commands_on_spoke(tmp_path, capsys, monkeypatch):
    from exocortex import cli
    cfg_file = tmp_path / "spoke.yaml"
    cfg_file.write_text(
        "role: spoke\ndevice_name: desktop\nhub_name: laptop\n"
        f"state_dir: {(tmp_path / 'spoke-state').as_posix()}\n", encoding="utf-8")
    for argv in (["bot"], ["cycle", "nightly"], ["brief"], ["tasks"],
                 ["recent"], ["msg", "hi"], ["devices"], ["graph", "export"]):
        rc = cli.main(["--config", str(cfg_file)] + argv)
        out = capsys.readouterr().out
        assert rc == 2, argv
        assert "spoke" in out and "laptop" in out, argv


def test_spoke_sync_pull_and_brain_push_refuse(tmp_path):
    from exocortex.sync.main import SyncError, pull, push
    cfg = spoke_cfg(tmp_path)
    with pytest.raises(SyncError, match="spoke"):
        pull(cfg)
    with pytest.raises(SyncError, match="spoke"):
        push(cfg)


def test_spoke_installer_never_registers_bot_or_cycles(tmp_path):
    from exocortex.ops.installer import win_startup_for, win_tasks_for
    spoke = spoke_cfg(tmp_path)
    hub = hub_cfg(tmp_path)
    spoke_cmds = {c for _, c in win_startup_for(spoke)}
    spoke_tasks = {c for _, c, _ in win_tasks_for(spoke)}
    assert "bot.cmd" not in spoke_cmds
    assert spoke_cmds == {"daemon.cmd"}
    assert spoke_tasks == {"push.cmd"}
    assert not spoke_tasks & {"nightly.cmd", "brief.cmd", "weekly.cmd"}
    assert {c for _, c in win_startup_for(hub)} == {"daemon.cmd", "bot.cmd"}


# ------------------------------------------------- per-device allowlists

def test_allowlist_dict_is_per_device(tmp_path):
    lap = tmp_path / "laptop-code"
    desk = tmp_path / "desktop-code"
    lap.mkdir(), desk.mkdir()
    cfg = hub_cfg(tmp_path)
    cfg.data["capture_code_from"] = {
        "laptop": [str(lap)], "desktop": [str(desk)]}
    assert device_code_paths(cfg) == [str(lap)]
    roots = resolved_roots(cfg)
    from exocortex.daemon.allowlist import is_allowed
    assert is_allowed(lap / "x.py", roots)
    # the OTHER machine's paths never apply here
    assert not is_allowed(desk / "x.py", roots)


def test_allowlist_unknown_device_fails_closed(tmp_path):
    cfg = hub_cfg(tmp_path)
    cfg.data["device_name"] = "some-new-machine"
    cfg.data["capture_code_from"] = {"laptop": [str(tmp_path)]}
    assert device_code_paths(cfg) == []
    assert resolved_roots(cfg) == []
    from exocortex.daemon.allowlist import is_allowed
    assert not is_allowed(tmp_path / "x.py", [])


def test_allowlist_legacy_list_still_works(tmp_path):
    cfg = hub_cfg(tmp_path)
    cfg.data["capture_code_from"] = [str(tmp_path)]
    assert device_code_paths(cfg) == [str(tmp_path)]
    assert len(resolved_roots(cfg)) == 1


# ------------------------------------------------------- queue + Plane B

def test_batch_carries_reflections_and_never_plane_b(tmp_path):
    import pyrage
    cfg = spoke_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    add_reflection(conn, cfg, "spoke reflection one", extract_now=False)
    add_reflection(conn, cfg, "spoke reflection two", extract_now=False)

    # Plane B through the real pipeline → local.db
    from exocortex.daemon.filter import Event, Firewall
    from exocortex.daemon.pipeline import Pipeline
    local = db.local_connect(cfg)
    p = Pipeline(local, Firewall(cfg.data["firewall"]), cfg.device_name,
                 inline_flush=True)
    p.submit(Event(source="browser", kind="visit",
                   content="https://secret-research.example/page-i-visited"))
    p.flush()
    assert local.execute("SELECT count(*) FROM life_stream").fetchone()[0] >= 1

    # adversarial: a trail row smuggled into the STAGING db itself
    conn.execute("INSERT INTO life_stream (ts, device, source, content)"
                 " VALUES (?, 'desktop', 'browser', 'smuggled-trail-row')",
                 (db.now_ms(),))
    conn.commit()
    conn.close()
    local.close()

    batch_file = build_batch(cfg)
    assert batch_file is not None
    payload = pyrage.passphrase.decrypt(batch_file.read_bytes(), PASSPHRASE)
    batch = json.loads(payload)
    assert batch["device"] == "desktop"
    assert {e["table"] for e in batch["events"]} <= {"reflections", "code_activity"}
    text = payload.decode("utf-8")
    assert "secret-research.example" not in text
    assert "smuggled-trail-row" not in text
    assert "life_stream" not in text


def test_build_batch_returns_none_when_nothing_new(tmp_path):
    cfg = spoke_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    add_reflection(conn, cfg, "only one", extract_now=False)
    conn.close()
    assert build_batch(cfg) is not None
    assert build_batch(cfg) is None  # checkpoint advanced


def test_spoke_push_builds_queue_not_brain_bundle(tmp_path):
    cfg = spoke_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    add_reflection(conn, cfg, "queued thought", extract_now=False)
    conn.close()
    msg = push_queue(cfg)
    assert "batch" in msg
    pend = list((queue_dir(cfg) / "pending").glob("batch-*.json.age"))
    assert len(pend) == 1
    # and never a brain bundle
    assert not list((cfg.state_dir / "sync-out").glob("exocortex-*")) \
        if (cfg.state_dir / "sync-out").exists() else True


# ------------------------------------------------------------ hub merge

def _spoke_with_two_reflections(tmp_path):
    spoke = spoke_cfg(tmp_path)
    conn = db.connect(spoke.db_path)
    add_reflection(conn, spoke, "merged from the desktop: mediapipe idea",
                   extract_now=False)
    add_reflection(conn, spoke, "second desktop thought", extract_now=False)
    conn.close()
    build_batch(spoke)
    return spoke


def test_merge_inserts_device_tagged_pending_reflections(tmp_path):
    spoke = _spoke_with_two_reflections(tmp_path)
    hub = hub_cfg(tmp_path)
    deliver(spoke, hub)
    stats = merge_batches(hub)
    assert stats["batches"] == 1 and stats["new_rows"] == 2
    conn = db.connect(hub.db_path)
    rows = conn.execute("SELECT id, device, extraction_status, raw_text"
                        " FROM reflections").fetchall()
    assert len(rows) == 2
    assert all(r["device"] == "desktop" for r in rows)
    assert all(r["extraction_status"] == "pending" for r in rows)
    # provenance names the device, and displays use uids
    from exocortex.intents.provenance import short_label
    assert "on desktop" in short_label(conn, "reflections", rows[0]["id"])
    conn.close()
    # consumed file moved out of the inbox
    assert not list(inbox_dir(hub).glob("*.age"))
    assert len(list((inbox_dir(hub) / "merged").glob("*.age"))) == 1


def test_merge_same_batch_twice_inserts_nothing(tmp_path):
    spoke = _spoke_with_two_reflections(tmp_path)
    hub = hub_cfg(tmp_path)
    files = deliver(spoke, hub)
    assert merge_batches(hub)["new_rows"] == 2
    # deliver the SAME file again (e.g. manual copy twice)
    merged = inbox_dir(hub) / "merged" / files[0].name
    (inbox_dir(hub) / files[0].name).write_bytes(merged.read_bytes())
    stats = merge_batches(hub)
    assert stats["new_rows"] == 0 and stats["already"] == 1
    conn = db.connect(hub.db_path)
    assert conn.execute("SELECT count(*) FROM reflections").fetchone()[0] == 2
    conn.close()


def test_rebatched_rows_dedupe_by_event_uid(tmp_path):
    """A checkpoint rollback on the spoke re-batches the same rows under a
    NEW batch_id — the deterministic event uids must still dedupe."""
    spoke = _spoke_with_two_reflections(tmp_path)
    hub = hub_cfg(tmp_path)
    deliver(spoke, hub)
    assert merge_batches(hub)["new_rows"] == 2
    # roll the spoke's checkpoint back and rebuild
    conn = db.connect(spoke.db_path)
    db.set_control(conn, "queue_ckpt_reflections", "0")
    conn.close()
    time.sleep(1.1)  # distinct batch filename (second-resolution timestamp)
    assert build_batch(spoke) is not None
    deliver(spoke, hub)
    stats = merge_batches(hub)
    assert stats["batches"] == 1 and stats["new_rows"] == 0
    conn = db.connect(hub.db_path)
    assert conn.execute("SELECT count(*) FROM reflections").fetchone()[0] == 2
    conn.close()


def test_tampered_batch_with_trail_events_is_refused(tmp_path):
    import pyrage
    hub = hub_cfg(tmp_path)
    batch = {
        "batch_id": "tampered-0001", "device": "desktop",
        "created_ts": db.now_ms(),
        "events": [
            {"uid": "desktop/life_stream/1", "table": "life_stream",
             "row": {"ts": 1, "content": "trail row trying to sync"}},
            {"uid": "desktop/reflections/1", "table": "reflections",
             "row": {"ts": db.now_ms(), "raw_text": "legit reflection"}},
            {"uid": "desktop/reflections/2", "table": "reflections",
             "row": {"ts": db.now_ms()}},  # malformed: no raw_text
        ],
    }
    indir = inbox_dir(hub)
    indir.mkdir(parents=True, exist_ok=True)
    enc = pyrage.passphrase.encrypt(json.dumps(batch).encode(), PASSPHRASE)
    (indir / "batch-desktop-tampered.json.age").write_bytes(enc)
    stats = merge_batches(hub)
    assert stats["refused"] == 2
    assert stats["new_rows"] == 1
    conn = db.connect(hub.db_path)
    assert conn.execute("SELECT count(*) FROM reflections").fetchone()[0] == 1
    # nothing landed in the synced life_stream
    assert conn.execute("SELECT count(*) FROM life_stream").fetchone()[0] == 0
    conn.close()


def test_unreadable_batch_goes_to_failed_not_crash(tmp_path):
    hub = hub_cfg(tmp_path)
    indir = inbox_dir(hub)
    indir.mkdir(parents=True, exist_ok=True)
    (indir / "batch-desktop-garbage.json.age").write_bytes(b"not encrypted at all")
    stats = merge_batches(hub)
    assert stats["failed"] == 1 and stats["batches"] == 0
    assert len(list((indir / "failed").glob("*.age"))) == 1


def test_devices_report_names_spokes_and_pending(tmp_path):
    from exocortex.sync.merge import devices_report
    spoke = _spoke_with_two_reflections(tmp_path)
    hub = hub_cfg(tmp_path)
    deliver(spoke, hub)
    merge_batches(hub)
    # a second, not-yet-merged batch shows as pending
    conn = db.connect(spoke.db_path)
    add_reflection(conn, spoke, "third", extract_now=False)
    conn.close()
    time.sleep(1.1)
    build_batch(spoke)
    deliver(spoke, hub)
    report = devices_report(hub)
    assert "laptop (hub" in report
    assert "desktop (spoke)" in report
    assert "awaiting merge" in report


# ------------------------------------------- verifier-found regressions

def test_malformed_decryptable_batch_cannot_wedge_merge(tmp_path):
    """A batch that decrypts but has garbage inside crashed the merge loop
    forever, blocking every good batch behind it — the nightly swallowed the
    error, so nobody would know."""
    import pyrage
    hub = hub_cfg(tmp_path)
    indir = inbox_dir(hub)
    indir.mkdir(parents=True, exist_ok=True)
    bad = {"batch_id": "bad-1", "device": "desktop", "created_ts": "NaN",
           "events": ["boom", {"uid": "", "table": 7}, None]}
    (indir / "batch-desktop-aaa.json.age").write_bytes(
        pyrage.passphrase.encrypt(json.dumps(bad).encode(), PASSPHRASE))
    good = {"batch_id": "good-1", "device": "desktop", "created_ts": db.now_ms(),
            "events": [{"uid": "desktop/reflections/1@111", "table": "reflections",
                        "row": {"ts": db.now_ms(), "raw_text": "good one"}}]}
    (indir / "batch-desktop-zzz.json.age").write_bytes(
        pyrage.passphrase.encrypt(json.dumps(good).encode(), PASSPHRASE))

    stats = merge_batches(hub)  # must not raise
    assert stats["new_rows"] == 1  # the good batch behind the bad one merged
    stats2 = merge_batches(hub)  # and re-running stays clean
    assert stats2["failed"] == 0 and stats2["new_rows"] == 0
    conn = db.connect(hub.db_path)
    assert conn.execute("SELECT count(*) FROM reflections").fetchone()[0] == 1
    conn.close()


def test_missing_passphrase_leaves_batches_pending(tmp_path):
    """No passphrase = config problem: abort BEFORE touching files. The old
    behavior misfiled every good batch into failed/, silently dropping all
    spoke capture."""
    from exocortex.sync.main import SyncError
    spoke = _spoke_with_two_reflections(tmp_path)
    hub = hub_cfg(tmp_path)
    deliver(spoke, hub)
    hub.data["sync"]["passphrase"] = ""
    with pytest.raises(SyncError):
        merge_batches(hub)
    assert len(list(inbox_dir(hub).glob("batch-*.json.age"))) == 1  # still pending
    assert not (inbox_dir(hub) / "failed").exists()


def test_invalid_role_fails_closed(tmp_path):
    """role: 'spok' (typo) must not silently become hub — that re-enables
    the two-bots-one-token failure this feature exists to prevent."""
    from exocortex.config import ConfigError
    cfg = base_cfg(tmp_path, "desktop", "spok", "state")
    with pytest.raises(ConfigError):
        cfg.is_spoke
    from exocortex import cli
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("role: spok\n", encoding="utf-8")
    assert cli.main(["--config", str(cfg_file), "tasks"]) == 2


def test_batch_claiming_hub_name_is_refused(tmp_path):
    import pyrage
    hub = hub_cfg(tmp_path)
    indir = inbox_dir(hub)
    indir.mkdir(parents=True, exist_ok=True)
    imposter = {"batch_id": "imp-1", "device": "laptop",  # the hub's own name
                "created_ts": db.now_ms(),
                "events": [{"uid": "laptop/reflections/1@1", "table": "reflections",
                            "row": {"ts": db.now_ms(), "raw_text": "spoofed"}}]}
    (indir / "batch-laptop-imp.json.age").write_bytes(
        pyrage.passphrase.encrypt(json.dumps(imposter).encode(), PASSPHRASE))
    stats = merge_batches(hub)
    assert stats["failed"] == 1 and stats["new_rows"] == 0
    conn = db.connect(hub.db_path)
    assert conn.execute("SELECT count(*) FROM reflections").fetchone()[0] == 0
    conn.close()


def test_reset_staging_db_rows_still_merge(tmp_path):
    """Wiping a spoke's state dir restarts staging ids at 1; the timestamp in
    each event uid keeps fresh rows from colliding with the hub's ledger."""
    import shutil as _sh
    spoke = _spoke_with_two_reflections(tmp_path)
    hub = hub_cfg(tmp_path)
    deliver(spoke, hub)
    assert merge_batches(hub)["new_rows"] == 2
    _sh.rmtree(spoke.state_dir)  # the spoke is reinstalled from scratch
    time.sleep(1.1)
    conn = db.connect(spoke.db_path)
    add_reflection(conn, spoke, "after the reset", extract_now=False)
    conn.close()
    assert build_batch(spoke) is not None
    deliver(spoke, hub)
    assert merge_batches(hub)["new_rows"] == 1  # not swallowed by the ledger
    conn = db.connect(hub.db_path)
    assert conn.execute("SELECT count(*) FROM reflections").fetchone()[0] == 3
    conn.close()


def test_implausible_event_ts_is_refused(tmp_path):
    """An absurd ts merges fine and then crashes every /recent render on
    Windows (fromtimestamp OSError) — with the listing broken, the user
    can't even see the row's uid to delete it. Refuse at the boundary."""
    import pyrage
    hub = hub_cfg(tmp_path)
    indir = inbox_dir(hub)
    indir.mkdir(parents=True, exist_ok=True)
    bad_ts = {"batch_id": "ts-1", "device": "desktop", "created_ts": db.now_ms(),
              "events": [
                  {"uid": "desktop/reflections/1@1", "table": "reflections",
                   "row": {"ts": 10**18, "raw_text": "from the year 33mil"}},
                  {"uid": "desktop/reflections/2@2", "table": "reflections",
                   "row": {"ts": 5, "raw_text": "from 1970"}},
                  {"uid": "desktop/reflections/3@3", "table": "reflections",
                   "row": {"ts": db.now_ms(), "raw_text": "sane one"}}]}
    (indir / "batch-desktop-ts.json.age").write_bytes(
        pyrage.passphrase.encrypt(json.dumps(bad_ts).encode(), PASSPHRASE))
    stats = merge_batches(hub)
    assert stats["refused"] == 2 and stats["new_rows"] == 1
    conn = db.connect(hub.db_path)
    rows = conn.execute("SELECT raw_text FROM reflections").fetchall()
    assert [r["raw_text"] for r in rows] == ["sane one"]
    # and the listing that motivated the guard still renders
    from exocortex.intents.memory_edit import recent
    assert "sane one" in recent(conn, 5)
    conn.close()


# --------------------------------------------------- git capture (Plane A)

def _git_ok(repo):
    """Defender intermittently denies subprocess spawns; retry then skip."""
    for _ in range(3):
        try:
            r = subprocess.run(["git", "init", "-q", str(repo)],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return True
        except OSError:
            time.sleep(0.5)
    return False


def test_spoke_git_commits_travel_and_dedupe(tmp_path):
    repo = tmp_path / "personal-project"
    repo.mkdir()
    if not _git_ok(repo):
        pytest.skip("git unavailable (Defender/OS refusing subprocess)")
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
    (repo / "a.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
    r = subprocess.run(["git", "-C", str(repo), *env_args, "commit", "-q",
                        "-m", "desktop feature commit"], capture_output=True,
                       text=True)
    if r.returncode != 0:
        pytest.skip(f"git commit failed in test env: {r.stderr[:100]}")

    spoke = spoke_cfg(tmp_path)
    spoke.data["capture_code_from"] = {"desktop": [str(repo)]}
    hub = hub_cfg(tmp_path)
    assert build_batch(spoke) is not None
    deliver(spoke, hub)
    assert merge_batches(hub)["new_rows"] == 1
    conn = db.connect(hub.db_path)
    row = conn.execute("SELECT repo, message FROM code_activity").fetchone()
    assert row["message"] == "desktop feature commit"
    conn.close()
    # same commit re-batched → still one row
    sconn = db.connect(spoke.db_path)
    db.set_control(sconn, "queue_ckpt_code", "0")
    sconn.close()
    time.sleep(1.1)
    build_batch(spoke)
    deliver(spoke, hub)
    assert merge_batches(hub)["new_rows"] == 0
