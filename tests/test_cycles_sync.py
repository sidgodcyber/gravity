import json
import shutil
import sys
import types

import pytest

from exocortex.config import Config
from exocortex.cycles import nightly
from exocortex.state import db
from exocortex.sync.main import SyncError, pull, push


def cfg_for(tmp_path):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["embeddings"]["enabled"] = False
    c.data["router"]["tiers"]["T1_free"] = ["fake/t1"]
    c.data["router"]["tiers"]["T2_workhorse"] = ["fake/t2"]
    c.data["sync"]["passphrase"] = "correct horse battery staple smoke"
    return c


def install_fake_litellm(monkeypatch, replies: dict[str, str] | Exception):
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True

    def completion(model, messages, **kw):
        if isinstance(replies, Exception):
            raise replies
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=10)
        msg = types.SimpleNamespace(content=replies.get(model, "generic reply"))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    mod.completion = completion
    mod.completion_cost = lambda completion_response=None: 0.001
    monkeypatch.setitem(sys.modules, "litellm", mod)


def seed(conn):
    now = db.now_ms()
    conn.execute(
        "INSERT INTO life_stream (ts, device, source, content, app) VALUES (?, 'd', 'window',"
        " 'PubChem — spectroscopy dataset', 'chrome.exe')", (now,))
    conn.execute(
        "INSERT INTO reflections (ts, device, raw_text, domain) VALUES (?, 'd',"
        " 'sketched the exhibit interactive', 'museum tech')", (now,))
    conn.execute(
        "INSERT INTO opportunities (ts, title, fit_score, deadline) VALUES (?,"
        " 'Regional science fair', 0.9, '2026-08-01')", (now,))
    conn.execute(
        "INSERT INTO crm (name, kind, next_action, next_action_due, created_ts, updated_ts)"
        " VALUES ('Gallery X', 'prospect', 'send proposal', '2026-07-01', ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO skill_nodes (name, type, created_ts, updated_ts, confidence)"
        " VALUES ('python', 'skill', ?, ?, 0.9), ('exhibit design', 'domain', ?, ?, 0.8)",
        (now, now, now, now))
    conn.commit()


def test_compose_brief_stores_and_uses_materials(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    install_fake_litellm(monkeypatch, {
        "fake/t1": "You explored spectroscopy data and sketched the exhibit interactive.",
        "fake/t2": "🌅 Brief — test\nYesterday: three sentences here.",
    })
    conn = db.connect(cfg.db_path)
    seed(conn)
    text = nightly.compose_brief(cfg, conn)
    assert text.startswith("🌅")
    assert conn.execute("SELECT count(*) FROM briefs").fetchone()[0] == 1
    # opportunity got surfaced
    assert conn.execute("SELECT status FROM opportunities").fetchone()[0] == "surfaced"
    conn.close()


def test_brief_degrades_mechanically_when_all_models_fail(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    install_fake_litellm(monkeypatch, RuntimeError("everything is down"))
    conn = db.connect(cfg.db_path)
    seed(conn)
    text = nightly.compose_brief(cfg, conn)
    assert "offline fallback" in text
    assert "Regional science fair" in text
    assert "Gallery X" in text          # CRM alert survives without any LLM
    conn.close()


def test_missed_connection_requires_shared_artifact(tmp_path):
    """A connection is stated only when two unlinked projects share a concrete
    artifact node — and names that artifact so the brief can cite it."""
    from exocortex.extract.graph import upsert_edge, upsert_node
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    kiosk = upsert_node(conn, "kiosk exhibit", "project")
    agewarp = upsert_node(conn, "agewarp", "project")
    mp = upsert_node(conn, "mediapipe", "tool")
    upsert_edge(conn, mp, kiosk, "used_in")     # both projects genuinely
    upsert_edge(conn, mp, agewarp, "used_in")   # use the same tool
    conn.commit()
    out = nightly.missed_connection(conn)
    assert "mediapipe" in out and "kiosk exhibit" in out and "agewarp" in out
    conn.close()


def test_no_connection_on_surface_jargon_only(tmp_path):
    """Adversarial (Chandrayaan-2 / AgeWarp): two unrelated real projects that
    share only a domain label — no shared tool/technique/dataset — must
    produce NO connection line, never an invented one."""
    from exocortex.extract.graph import upsert_edge, upsert_node
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    chandra = upsert_node(conn, "chandrayaan-2 lunar analysis", "project")
    agewarp = upsert_node(conn, "agewarp de-aging", "project")
    ds = upsert_node(conn, "data science", "domain")   # shared VOCABULARY only
    upsert_edge(conn, chandra, ds, "in_domain")
    upsert_edge(conn, agewarp, ds, "in_domain")
    # each project uses its OWN distinct tool — nothing concrete is shared
    upsert_edge(conn, upsert_node(conn, "gdal", "tool"), chandra, "used_in")
    upsert_edge(conn, upsert_node(conn, "pytorch", "tool"), agewarp, "used_in")
    conn.commit()
    assert nightly.missed_connection(conn) == ""
    conn.close()


def test_crm_alerts_flags_due_and_stale(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    seed(conn)
    alerts = nightly.crm_alerts(conn, cfg)
    assert any("Gallery X" in a for a in alerts)
    conn.close()


# ------------------------------------------------------------------ sync

def test_sync_round_trip_restores_brain(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO reflections (ts, device, raw_text) VALUES (?, 'd', 'the canary row')",
        (db.now_ms(),))
    conn.commit()
    conn.close()

    msg = push(cfg)
    assert "encrypted bundle written" in msg
    outdir = cfg.state_dir / "sync-out"
    bundle = next(outdir.glob("exocortex-*.tar.gz.age"))
    assert bundle.stat().st_size > 0
    raw = bundle.read_bytes()
    assert b"the canary row" not in raw  # actually encrypted

    # simulate a new machine: fresh state dir, bundle dropped into sync-in
    cfg2 = cfg_for(tmp_path)
    cfg2.data["state_dir"] = str(tmp_path / "state2")
    indir = cfg2.state_dir / "sync-in"
    indir.mkdir(parents=True)
    shutil.copy2(bundle, indir / bundle.name)
    msg = pull(cfg2)
    assert "db restored" in msg
    conn = db.connect(cfg2.db_path)
    assert conn.execute(
        "SELECT count(*) FROM reflections WHERE raw_text = 'the canary row'"
    ).fetchone()[0] == 1
    conn.close()


def test_pull_refuses_overwrite_without_force(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.close()
    push(cfg)
    bundle = next((cfg.state_dir / "sync-out").glob("*.age"))
    indir = cfg.state_dir / "sync-in"
    indir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle, indir / bundle.name)
    with pytest.raises(SyncError, match="--force"):
        pull(cfg)
    msg = pull(cfg, force=True)
    assert "backed up" in msg


def test_wrong_passphrase_fails(tmp_path):
    cfg = cfg_for(tmp_path)
    db.connect(cfg.db_path).close()
    push(cfg)
    bundle = next((cfg.state_dir / "sync-out").glob("*.age"))
    indir = cfg.state_dir / "sync-in"
    indir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle, indir / bundle.name)
    cfg.data["sync"]["passphrase"] = "wrong phrase entirely"
    with pytest.raises(Exception):
        pull(cfg, force=True)
