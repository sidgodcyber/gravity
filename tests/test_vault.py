"""Obsidian vault export: adversarial filenames must never escape the vault,
user-created files must never be touched, and incremental sync must rewrite
only what changed and remove notes for deleted nodes (healing extends into
the vault)."""

import json

import pytest

from exocortex.config import Config
from exocortex.state import db
from exocortex.state.uids import uid_for
from exocortex.vault import VaultExporter, export_vault, sanitize_name


def cfg_for(tmp_path):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["vault"] = {"path": str(tmp_path / "vault"), "enabled": True}
    return c


def add_node(conn, name, ntype="skill", confidence=0.5):
    now = db.now_ms()
    cur = conn.execute(
        "INSERT INTO skill_nodes (name, type, created_ts, updated_ts, confidence)"
        " VALUES (?, ?, ?, ?, ?)", (name, ntype, now, now, confidence))
    conn.commit()
    return cur.lastrowid


def add_edge(conn, src, dst, rel="in_domain", evidence=None):
    now = db.now_ms()
    conn.execute(
        "INSERT INTO skill_edges (src, dst, rel, created_ts, updated_ts, evidence)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (src, dst, rel, now, now, json.dumps(evidence or [])))
    conn.commit()


def vault_files(root):
    return {p.relative_to(root).as_posix()
            for p in root.rglob("*") if p.is_file()}


# ------------------------------------------------------------ sanitization

@pytest.mark.parametrize("evil", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32",
    'quote"pipe|question?star*',
    "angle<brack>ets:colon",
    "wiki[[link]]#hash^caret",
    "CON", "com1", "NUL",
    "...", "   ", "",
    "trailing dots...", "trailing space   ",
    "emoji 🚀🔥 stays fine",
    "x" * 300,
])
def test_sanitize_never_produces_path_tricks(evil):
    s = sanitize_name(evil, node_id=7)
    assert s, f"empty result for {evil!r}"
    assert not any(ch in s for ch in '<>:"/\\|?*[]#^'), s
    assert ".." not in s
    assert not s.endswith((".", " "))
    assert s.casefold() not in {"con", "com1", "nul"}
    assert len(s) <= 80


def test_adversarial_names_stay_inside_vault(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    for name in ["../../../pwn", "..\\..\\evil", "CON", 'a"b|c?d*e',
                 "sla/sh\\es", "🚀 emoji skill", "[[escape]] attempt#1"]:
        add_node(conn, name)
    conn.close()

    stats = export_vault(cfg)
    root = tmp_path / "vault"
    files = vault_files(root)
    assert stats["written"] == 8  # 7 notes + dashboard
    # every produced file is inside the vault; nothing appeared next to it
    assert all(not f.startswith("..") for f in files)
    outside = [p for p in tmp_path.rglob("*.md")
               if root not in p.parents and p.parent != root]
    assert outside == []
    assert not (tmp_path / "pwn.md").exists()
    assert not (tmp_path.parent / "evil.md").exists()


def test_same_sanitized_name_gets_disambiguated(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    a = add_node(conn, "clip/studio")
    b = add_node(conn, "clip\\studio")   # sanitizes identically
    conn.close()
    export_vault(cfg)
    files = vault_files(tmp_path / "vault")
    assert f"Skills/clip studio.md" in files
    assert f"Skills/clip studio ({b}).md" in files


# ------------------------------------------------------------ content

def test_note_has_frontmatter_wikilinks_and_uid_evidence(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    # make reflection row id != its uid by minting uids for other rows first
    conn.execute("INSERT INTO inbox (ts, raw_text, source, intent)"
                 " VALUES (?, 'x', 'test', 'reflection')", (db.now_ms(),))
    conn.commit()
    uid_for(conn, "inbox", 1)
    conn.execute("INSERT INTO reflections (ts, device, raw_text)"
                 " VALUES (?, 'test', 'built the mediapipe face mesh demo')",
                 (db.now_ms(),))
    conn.commit()
    refl_uid = uid_for(conn, "reflections", 1)
    assert refl_uid != 1  # the display number must be the uid, not the row id

    skill = add_node(conn, "mediapipe", "skill", confidence=0.35)
    dom = add_node(conn, "computer vision", "domain")
    add_edge(conn, skill, dom, "in_domain",
             [{"table": "reflections", "id": 1}])
    conn.close()

    export_vault(cfg)
    note = (tmp_path / "vault" / "Skills" / "mediapipe.md").read_text(encoding="utf-8")
    assert note.startswith("---\ntype: skill\nconfidence: 0.35\n")
    assert "gravity: exporter-owned" in note
    assert "[[computer vision]]" in note
    assert "belongs to the domain" in note
    assert f"record #{refl_uid}," in note      # uid, never the raw table id
    assert "record #1," not in note
    # provenance label, now device-tagged (multi-device build)
    assert "you said (direct log, on test)" in note
    assert "shaky" in note                      # low-confidence phrasing
    # and the wikilink target actually exists
    assert (tmp_path / "vault" / "Domains" / "computer vision.md").exists()


def test_dashboard_counts_and_client_links(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    a = add_node(conn, "reels editing", "skill")
    b = add_node(conn, "social media", "domain")
    add_edge(conn, a, b)
    now = db.now_ms()
    conn.execute("INSERT INTO tasks (text, entity, status, created_ts, updated_ts)"
                 " VALUES ('send the July calendar', 'Roven', 'open', ?, ?)",
                 (now, now))
    conn.commit()
    task_uid = uid_for(conn, "tasks", 1)
    conn.close()

    export_vault(cfg)
    dash = (tmp_path / "vault" / "_START HERE.md").read_text(encoding="utf-8")
    assert "2 nodes · 1 connections" in dash
    assert "[[Roven]]" in dash
    client = (tmp_path / "vault" / "Clients" / "Roven.md").read_text(encoding="utf-8")
    assert f"#{task_uid} send the July calendar" in client


# ------------------------------------------------------------ incremental

def test_unchanged_notes_not_rewritten(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    add_node(conn, "python")
    conn.close()
    s1 = export_vault(cfg)
    note = tmp_path / "vault" / "Skills" / "python.md"
    before = note.stat().st_mtime_ns
    s2 = export_vault(cfg)
    assert s2["written"] == 0 and s2["removed"] == 0
    assert s2["unchanged"] == s1["notes"]
    assert note.stat().st_mtime_ns == before


def test_changed_node_rewritten_deleted_node_removed(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    keep = add_node(conn, "keep me")
    gone = add_node(conn, "delete me")
    export_vault(cfg)
    assert (tmp_path / "vault" / "Skills" / "delete me.md").exists()

    conn.execute("UPDATE skill_nodes SET confidence = 0.9 WHERE id = ?", (keep,))
    conn.execute("DELETE FROM skill_nodes WHERE id = ?", (gone,))
    conn.commit()
    conn.close()
    s = export_vault(cfg)
    assert not (tmp_path / "vault" / "Skills" / "delete me.md").exists()
    assert s["removed"] == 1
    assert "0.90" in (tmp_path / "vault" / "Skills" / "keep me.md").read_text(encoding="utf-8")


# ------------------------------------------------------------ user files

def test_user_created_files_never_touched(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    add_node(conn, "obsidian")
    root = tmp_path / "vault"
    # a user note at an unrelated path, and one squatting on an exporter path
    (root / "Skills").mkdir(parents=True)
    (root / "my own thoughts.md").write_text("mine", encoding="utf-8")
    squatter = root / "Skills" / "obsidian.md"
    squatter.write_text("the user's own obsidian note", encoding="utf-8")

    export_vault(cfg)
    assert (root / "my own thoughts.md").read_text(encoding="utf-8") == "mine"
    assert squatter.read_text(encoding="utf-8") == "the user's own obsidian note"

    # even after the node is deleted, the user's file must survive: the
    # exporter never adopted it into the manifest
    conn.execute("DELETE FROM skill_nodes")
    conn.commit()
    conn.close()
    s = export_vault(cfg)
    assert s["removed"] == 0
    assert squatter.read_text(encoding="utf-8") == "the user's own obsidian note"
    assert (root / "my own thoughts.md").exists()


# ------------------------------------------------- verifier regressions

def test_garbage_manifest_entries_cannot_wedge_the_export(tmp_path):
    """'' or '.' entries resolve to the vault root DIRECTORY; unlinking it
    crashed every subsequent export until the manifest was hand-deleted."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    add_node(conn, "survivor")
    conn.close()
    export_vault(cfg)
    mpath = tmp_path / "vault" / ".gravity" / "manifest.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m[""] = "junk"
    m["."] = "junk"
    m["Skills"] = "junk"  # a directory, not a file
    mpath.write_text(json.dumps(m), encoding="utf-8")

    for _ in range(3):  # must not crash, ever again
        s = export_vault(cfg)
        assert s["removed"] == 0
    cleaned = json.loads(mpath.read_text(encoding="utf-8"))
    assert "" not in cleaned and "." not in cleaned and "Skills" not in cleaned
    assert (tmp_path / "vault" / "Skills" / "survivor.md").exists()


def test_hand_edited_exporter_note_is_restored(tmp_path):
    """Docs promise exporter-owned notes are overwritten every export —
    the unchanged check must compare against the file on disk, not just
    the manifest."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    add_node(conn, "python")
    conn.close()
    export_vault(cfg)
    note = tmp_path / "vault" / "Skills" / "python.md"
    original = note.read_text(encoding="utf-8")
    note.write_text("USER TAMPERED", encoding="utf-8")

    s = export_vault(cfg)
    assert s["written"] >= 1
    assert note.read_text(encoding="utf-8") == original


def test_entity_client_frontmatter_is_valid_yaml(tmp_path):
    """'created: ?' is a YAML syntax error that broke the whole properties
    block in Obsidian for entity-only client pages."""
    yaml = pytest.importorskip("yaml")
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    now = db.now_ms()
    conn.execute("INSERT INTO tasks (text, entity, status, created_ts, updated_ts)"
                 " VALUES ('t', 'Roven', 'open', ?, ?)", (now, now))
    conn.commit()
    conn.close()
    export_vault(cfg)
    text = (tmp_path / "vault" / "Clients" / "Roven.md").read_text(encoding="utf-8")
    fm = text.split("---")[1]
    parsed = yaml.safe_load(fm)
    assert parsed["type"] == "client"
    assert "?" not in fm


def test_three_way_name_collision_loses_no_note(tmp_path):
    """x, 'x (id)', x again: the suffix itself must be collision-checked or
    one node's note is silently dropped."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    add_node(conn, "x", "domain")
    b = add_node(conn, "x (%d)" % (2 + 3), "skill")  # forgeable suffix
    add_node(conn, "x", "skill")
    conn.close()
    s = export_vault(cfg)
    files = vault_files(tmp_path / "vault")
    notes = {f for f in files if f.endswith(".md") and f != "_START HERE.md"}
    assert len(notes) == 3, notes
    assert s["written"] == 4  # 3 notes + dashboard


def test_case_only_rename_keeps_note_present(tmp_path):
    """Windows is case-insensitive: renaming 'newname'→'NEWNAME' must not
    leave a one-export hole where the note vanishes."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    nid = add_node(conn, "newname")
    export_vault(cfg)
    conn.execute("UPDATE skill_nodes SET name = 'NEWNAME' WHERE id = ?", (nid,))
    conn.commit()
    conn.close()
    export_vault(cfg)
    note = tmp_path / "vault" / "Skills" / "NEWNAME.md"
    assert note.exists()
    assert "NEWNAME" not in json.dumps(  # no stale entry left behind
        {k: 0 for k in json.loads((tmp_path / "vault" / ".gravity" /
                                   "manifest.json").read_text(encoding="utf-8"))
         if not (tmp_path / "vault" / k).exists()})


def test_crm_rows_differing_only_in_case_both_get_pages(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    now = db.now_ms()
    for name in ("Roven", "ROVEN"):
        conn.execute("INSERT INTO crm (name, kind, created_ts, updated_ts)"
                     " VALUES (?, 'client', ?, ?)", (name, now, now))
    conn.commit()
    conn.close()
    export_vault(cfg)
    pages = [f for f in vault_files(tmp_path / "vault")
             if f.startswith("Clients/")]
    assert len(pages) == 2, pages


def test_manifest_entry_outside_root_is_not_deleted(tmp_path):
    """A tampered manifest must not let the deletion pass reach outside."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    add_node(conn, "anything")
    conn.close()
    export_vault(cfg)
    victim = tmp_path / "precious.md"
    victim.write_text("do not delete", encoding="utf-8")
    mpath = tmp_path / "vault" / ".gravity" / "manifest.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["../precious.md"] = "deadbeef"
    mpath.write_text(json.dumps(m), encoding="utf-8")

    export_vault(cfg)
    assert victim.exists() and victim.read_text(encoding="utf-8") == "do not delete"
