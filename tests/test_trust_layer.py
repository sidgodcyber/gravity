"""Final-consolidation trust layer: the five live-reproduced bugs, each as a
regression test. Bug 2 additionally gets a live-loop test (simulated process
restart between the /delete and the 'yes')."""

import json
import sys
import types

import pytest

from exocortex.config import Config
from exocortex.intents import classify, dispatch, memory_edit, route
from exocortex.intents.provenance import describe, short_label
from exocortex.intents.tasks import create_task, set_status_by_uid
from exocortex.state import db
from exocortex.state.uids import backfill, resolve, uid_for


def cfg_for(tmp_path):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["embeddings"]["enabled"] = False
    c.data["router"]["tiers"]["T2_workhorse"] = ["fake/t2"]
    c.data["router"]["tiers"]["T1_free"] = ["fake/t1"]
    return c


def fake_llm(monkeypatch, classifier_reply: str, resolver_match: str | None = None):
    import re
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True
    mod.drop_params = True

    def completion(model, messages, **kw):
        text = messages[-1]["content"]
        if "resolve a user's command" in text:
            targets = []
            if resolver_match:
                targets = [int(m.group(1)) for m in
                           re.finditer(rf"#(\d+): [^\n]*{re.escape(resolver_match)}",
                                       text, re.I)]
            out = json.dumps({"targets": sorted(set(targets)),
                              "question": None, "note": resolver_match or ""})
        else:
            out = classifier_reply
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=10)
        msg = types.SimpleNamespace(content=out)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    mod.completion = completion
    mod.completion_cost = lambda completion_response=None: 0.0
    monkeypatch.setitem(sys.modules, "litellm", mod)


# ================================================================ Bug 1: IDs

def test_bug1_regression_task9_vs_inbox9(tmp_path):
    """The exact live failure: tasks.id 9 and inbox.id 9 both exist; the
    displayed id must delete the row it was shown for, and only it."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    # engineer colliding internal ids
    for i in range(9):
        conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, ?, 'reflection')",
                     (db.now_ms(), f"filler {i}" if i < 8 else "Yes — Delete 7"))
    for i in range(8):
        create_task(conn, f"task filler {i}")
    museum = create_task(conn, "Follow up on call with the museum director")
    assert museum == 9  # tasks.id 9
    assert conn.execute("SELECT id FROM inbox WHERE raw_text = 'Yes — Delete 7'"
                        ).fetchone()["id"] == 9  # inbox.id 9 — the collision

    task_uid = uid_for(conn, "tasks", museum)
    inbox_uid = uid_for(conn, "inbox", 9)
    assert task_uid != inbox_uid  # ONE namespace: no shared numbers

    ok, msg = memory_edit.delete_by_uid(cfg, conn, task_uid)
    assert ok and "museum director" in msg
    # the task is gone; the same-numbered inbox row is untouched
    assert conn.execute("SELECT count(*) FROM tasks WHERE id = 9").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM inbox WHERE id = 9").fetchone()[0] == 1
    conn.close()


def test_uid_never_reused_after_delete(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    t = create_task(conn, "ephemeral")
    uid = uid_for(conn, "tasks", t)
    memory_edit.delete_by_uid(cfg, conn, uid)
    t2 = create_task(conn, "successor")
    uid2 = uid_for(conn, "tasks", t2)
    assert uid2 > uid                       # AUTOINCREMENT: no reuse
    assert resolve(conn, uid) is None       # stale id misses, never hits new data
    conn.close()


def test_backfill_assigns_uids_in_creation_order(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (100, 'older', 'reflection')")
    conn.execute("INSERT INTO tasks (text, status, created_ts, updated_ts)"
                 " VALUES ('newer', 'open', 200, 200)")
    conn.commit()
    assert backfill(conn) == 2
    assert resolve(conn, 1) == ("inbox", 1)   # older record → lower uid
    assert resolve(conn, 2)[0] == "tasks"
    conn.close()


# ================================================================ Bug 2: gate

DELETE_CLASSIFY = json.dumps({
    "intent": "correction", "tags": [], "confidence": 0.9, "entity": None,
    "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
    "correction": {"action": "delete", "target": "the filler note",
                   "find": "filler", "replace_with": None},
    "command": None, "clarify": None})


def test_bug2_confirmation_survives_process_restart(tmp_path, monkeypatch):
    """/delete → (process dies) → 'Yes' in a fresh process must still execute.
    Pending lives in the db, not any instance's memory."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'filler note', 'reflection')",
                 (db.now_ms(),))
    conn.commit()
    conn.close()
    fake_llm(monkeypatch, DELETE_CLASSIFY, resolver_match="filler")

    out1 = route.handle_incoming(cfg, "delete the filler note")
    assert "confirm" in out1["reply"].lower() or "yes" in out1["reply"].lower()
    # pending persisted in DB — verify, then simulate the restart by using
    # a completely fresh call path (no shared objects)
    conn = db.connect(cfg.db_path)
    assert route.get_pending(conn) is not None
    conn.close()

    out2 = route.handle_incoming(cfg, "Yes")
    assert "Deleted" in out2["reply"]
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM inbox WHERE raw_text='filler note'"
                        ).fetchone()[0] == 0
    # and the confirmation itself was NOT stored as content
    assert conn.execute("SELECT count(*) FROM inbox WHERE raw_text='Yes'"
                        ).fetchone()[0] == 0
    assert route.get_pending(conn) is None
    conn.close()


def test_bug2_yes_variants_accepted(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    for phrase in ("yes", "Yes", "YEP", "confirm"):
        conn = db.connect(cfg.db_path)
        conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'victim "
                     + phrase + "', 'reflection')", (db.now_ms(),))
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        route.set_pending(conn, {"type": "delete", "ts": db.now_ms(),
                                 "refs": [{"table": "inbox", "id": rid}]})
        conn.close()
        out = route.handle_incoming(cfg, phrase)
        assert "Deleted" in out["reply"], phrase


def test_ok_never_confirms_a_delete(tmp_path, monkeypatch):
    """'ok' is an everyday acknowledgement (people reply ok to a brief) —
    it must cancel-and-route, not destroy data."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'precious', 'reflection')",
                 (db.now_ms(),))
    route.set_pending(conn, {"type": "delete", "ts": db.now_ms(),
                             "refs": [{"table": "inbox", "id": 1}]})
    conn.close()
    fake_llm(monkeypatch, json.dumps({
        "intent": "reflection", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": "ok", "weekly": None,
        "correction": None, "command": None, "clarify": None}))
    out = route.handle_incoming(cfg, "ok")
    assert "Deleted" not in out["reply"]
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM inbox WHERE raw_text='precious'"
                        ).fetchone()[0] == 1
    conn.close()


def test_edit_releases_uid_no_rebinding(tmp_path, monkeypatch):
    """Verifier CRITICAL: /edit deletes+recreates the inbox row; without
    release() the old uid rebinds to whatever reuses the rowid."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'original', 'reflection')",
                 (db.now_ms(),))
    conn.commit()
    old_uid = uid_for(conn, "inbox", 1)
    fake_llm(monkeypatch, json.dumps({
        "intent": "reflection", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": "corrected text",
        "weekly": None, "correction": None, "command": None, "clarify": None}))
    memory_edit.edit_record(cfg, conn, 1, "corrected text")
    # the old uid must be dead — never rebound to the recreated row
    ref = resolve(conn, old_uid)
    if ref is not None:
        tbl, rid = ref
        row = conn.execute(f"SELECT 1 FROM {tbl} WHERE id = ?", (rid,)).fetchone()
        assert row is None, "old uid rebound to a live record after edit"
    conn.close()


def test_replace_without_replacement_never_offers_delete(tmp_path, monkeypatch):
    """Verifier HIGH: 'fix X' with no replacement text must ask what to write,
    not degrade into a delete confirmation."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'urban straps note', 'client_note')",
                 (db.now_ms(),))
    conn.commit()
    conn.close()
    fake_llm(monkeypatch, json.dumps({
        "intent": "correction", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": {"action": "replace", "target": "urban straps",
                       "find": "urban straps", "replace_with": None},
        "command": None, "clarify": None}), resolver_match="urban")
    out = route.handle_incoming(cfg, "fix the urban straps note")
    assert "delete" not in out["reply"].lower()
    assert "instead" in out["reply"].lower()
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM inbox").fetchone()[0] >= 1  # untouched
    conn.close()


# ================================================================ Bug 3: NL

def test_bug3_delete_task_9_natural_language(tmp_path, monkeypatch):
    """'delete task 9' (no slash) resolves through the NL layer to the
    exact shown uid, shows the plan, and deletes on confirm."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    t = create_task(conn, "the museum director task")
    uid = uid_for(conn, "tasks", t)
    conn.close()
    fake_llm(monkeypatch, json.dumps({
        "intent": "correction", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": {"action": "delete", "target": f"task {uid}",
                       "find": "museum director", "replace_with": None},
        "command": None, "clarify": None}), resolver_match="museum")
    out = route.handle_incoming(cfg, f"delete task {uid}")
    assert f"#{uid}" in out["reply"] and "confirm" in out["reply"].lower()
    out2 = route.handle_incoming(cfg, "yes")
    assert "Deleted" in out2["reply"]
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    conn.close()


def test_bug3_everything_except_shows_exact_list(tmp_path, monkeypatch):
    """Set expressions must expand to an explicit shown list; the kept record
    must not appear in it."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    keep_row = None
    for i, txt in enumerate(["keep this one", "extra a", "extra b", "extra c"]):
        conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, ?, 'reflection')",
                     (db.now_ms(), txt))
    conn.commit()
    keep_uid = uid_for(conn, "inbox",
                       conn.execute("SELECT id FROM inbox WHERE raw_text='keep this one'"
                                    ).fetchone()["id"])
    conn.close()
    fake_llm(monkeypatch, json.dumps({
        "intent": "correction", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": {"action": "delete", "target": "everything except keep",
                       "find": "extra", "replace_with": None},
        "command": None, "clarify": None}), resolver_match="extra")
    out = route.handle_incoming(cfg, f"delete everything except #{keep_uid}")
    assert "3 record(s)" in out["reply"]
    assert f"#{keep_uid} keep this one" not in out["reply"]
    route.handle_incoming(cfg, "yes")
    conn = db.connect(cfg.db_path)
    remaining = [r["raw_text"] for r in conn.execute("SELECT raw_text FROM inbox")]
    assert remaining == ["keep this one"]
    conn.close()


def test_bug3_resolver_cannot_invent_targets(tmp_path, monkeypatch):
    """Structural guard: a resolver reply naming a uid that was never in the
    provided context is discarded."""
    from exocortex.intents.resolve import resolve_targets
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'only record', 'reflection')",
                 (db.now_ms(),))
    conn.commit()
    real_uid = uid_for(conn, "inbox", 1)
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True
    mod.drop_params = True
    msg = types.SimpleNamespace(content=json.dumps(
        {"targets": [real_uid, 999, 12345], "question": None, "note": "x"}))
    mod.completion = lambda model, messages, **kw: types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=msg)],
        usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    mod.completion_cost = lambda completion_response=None: 0.0
    monkeypatch.setitem(sys.modules, "litellm", mod)
    res = resolve_targets(cfg, conn, "delete", "delete stuff", "record")
    assert res["targets"] == [real_uid]   # invented ids stripped
    conn.close()


# ================================================================ Bug 4: provenance

def test_bug4_provenance_labels_and_source(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, source, intent) VALUES"
                 " (?, 'typed by me', 'telegram', 'reflection')", (db.now_ms(),))
    conn.execute("INSERT INTO inbox (ts, raw_text, source, intent) VALUES"
                 " (?, 'spoken by me', 'voice', 'weekly_update')", (db.now_ms(),))
    conn.execute("INSERT INTO inbox (ts, raw_text, source, intent) VALUES"
                 " (?, 'a system test', 'test', 'client_note')", (db.now_ms(),))
    conn.commit()
    assert "you said (typed" in short_label(conn, "inbox", 1)
    assert "voice" in short_label(conn, "inbox", 2)
    assert "not you" in short_label(conn, "inbox", 3)  # test traffic never reads as the user
    listing = memory_edit.recent(conn, 5)
    assert "you said" in listing            # provenance visible in /recent
    uid = uid_for(conn, "inbox", 1)
    desc = describe(cfg, conn, uid)
    assert "Origin: you said" in desc and "When:" in desc
    conn.close()


# ================================================================ Bug 5: grounding

MUSEUM_CLASSIFY = json.dumps({
    "intent": "task", "tags": [], "confidence": 0.85, "entity": None,
    "tasks": [{"text": "Follow up on call with the museum director", "due": None,
               "priority": 0, "entity": None, "subtasks": [],
               "presupposes": "a call with the museum director"}],
    "ideas": [], "note": None, "reflection": None, "weekly": None,
    "correction": None, "command": None, "clarify": None})


def test_bug5_museum_director_task_held_without_evidence(tmp_path, monkeypatch):
    """The exact live failure: no record of any call → ask, don't mint."""
    cfg = cfg_for(tmp_path)
    fake_llm(monkeypatch, MUSEUM_CLASSIFY)
    out = route.handle_incoming(cfg, "Follow up on call with the museum director?")
    assert "no record of" in out["reply"].lower()
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0  # not minted
    conn.close()
    # confirming creates it, marked as unverified-premise
    out2 = route.handle_incoming(cfg, "yes")
    assert "Task #" in out2["reply"] and "without a prior record" in out2["reply"]
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    conn.close()


def test_bug5_grounded_presupposition_creates_directly(tmp_path, monkeypatch):
    """With actual evidence of the call in the store, no question is asked."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO reflections (ts, device, raw_text, extraction_status)"
                 " VALUES (?, 'd', 'had a call with the museum director about the kiosk',"
                 " 'done')", (db.now_ms(),))
    conn.commit()
    conn.close()
    fake_llm(monkeypatch, MUSEUM_CLASSIFY)
    out = route.handle_incoming(cfg, "Follow up on call with the museum director")
    assert "Task #" in out["reply"] and "no record" not in out["reply"].lower()
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    conn.close()


# ================================================================ reassign

def test_reassign_never_files_a_task(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake_llm(monkeypatch, json.dumps({
        "intent": "command", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": None,
        "command": {"action": "reassign", "target": "clean the skill graph"},
        "clarify": None}))
    out = route.handle_incoming(cfg, "not a task for me, it's for you: clean the skill graph")
    assert "not filing it as your task" in out["reply"].lower()
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    conn.close()


# ================================================================ guide

def test_meta_questions_route_to_guide_not_storage(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake_llm(monkeypatch, json.dumps({
        "intent": "meta", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": None, "command": None, "clarify": None}))
    out = route.handle_incoming(cfg, "what can you do?")
    assert "command card" in out["reply"].lower() or "Gravity" in out["reply"]
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0  # not logged
    conn.close()


def test_guide_topics_exist_and_render():
    from exocortex.guide import CARD, TOPICS, guide_text
    assert guide_text("") == CARD
    for topic in TOPICS:
        assert len(guide_text(topic)) > 50
    assert "confirm" in guide_text("delete").lower()
    assert "you said" in guide_text("provenance")
