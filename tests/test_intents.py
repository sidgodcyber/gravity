"""Phase 2.5: intent classification, task store, and editable memory with
downstream healing. Classifier LLM is mocked; the delete/heal path is
exercised adversarially (the spec's must-test-hard cases)."""

import json
import sys
import types

import pytest

from exocortex.config import Config
from exocortex.intents import classify, dispatch, memory_edit, route, tasks
from exocortex.state import db


def cfg_for(tmp_path):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["embeddings"]["enabled"] = False
    c.data["router"]["tiers"]["T2_workhorse"] = ["fake/t2"]
    c.data["router"]["tiers"]["T1_free"] = ["fake/t1"]
    return c


def fake_classifier(monkeypatch, reply: str, resolver_match: str | None = None):
    """Classifier calls get `reply`. Resolver calls (the second LLM stage) get
    targets built from the resolver's OWN prompt: every listed '#N: ...' line
    containing `resolver_match` — mimicking a model that picks the matching
    context ids, which is exactly what the structural validation permits."""
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
            out = json.dumps({"targets": sorted(set(targets)), "question": None,
                              "note": resolver_match or ""})
        else:
            out = reply
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=10)
        msg = types.SimpleNamespace(content=out)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    mod.completion = completion
    mod.completion_cost = lambda completion_response=None: 0.0
    monkeypatch.setitem(sys.modules, "litellm", mod)


# ---------------------------------------------------------------- classifier

def test_multi_intent_task_with_subtasks_and_entity(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    # seed a known client so entity resolution matches
    conn.execute("INSERT INTO crm (name, kind, created_ts, updated_ts) VALUES"
                 " ('Roven', 'client', 0, 0)")
    conn.commit()
    # due must be a real near-future date — _valid_due drops past dates, so a
    # hardcoded one makes the test rot as the calendar advances
    import datetime as _dt
    friday = (_dt.date.today() + _dt.timedelta(days=3)).isoformat()
    fake_classifier(monkeypatch, json.dumps({
        "intent": "task", "tags": ["weekly_update", "plan"], "confidence": 0.9,
        "entity": {"name": "roven", "known": True},
        "tasks": [{"text": "research Roven salon display", "due": friday,
                   "priority": 0, "entity": "roven",
                   "subtasks": ["salon belt display", "corporate gifting", "wholesale"]}],
        "ideas": ["pitch Roven wholesale"], "note": None, "reflection": None,
        "weekly": "finished the Roven scripts", "correction": None, "clarify": None,
    }))
    c = classify.classify(cfg, conn, "finished roven scripts, need salon research by friday")
    assert c["intent"] == "task"
    assert c["entity"]["name"] == "Roven" and c["entity"]["known"]  # canonical spelling
    assert c["tasks"][0]["due_ts"] is not None
    assert len(c["tasks"][0]["subtasks"]) == 3

    result = dispatch.dispatch(cfg, conn, c)
    # parent + 3 subtasks created
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 4
    parent = conn.execute("SELECT * FROM tasks WHERE parent_id IS NULL").fetchone()
    assert parent["entity"] == "Roven" and parent["due_ts"] is not None
    subs = conn.execute("SELECT count(*) FROM tasks WHERE parent_id = ?", (parent["id"],)).fetchone()[0]
    assert subs == 3
    # weekly tag also produced a weekly_outcome reflection
    assert conn.execute("SELECT count(*) FROM reflections WHERE insight_type='weekly_outcome'"
                        ).fetchone()[0] == 1
    assert "Roven" in result["reply"]
    conn.close()


def test_new_entity_flagged_not_invented(tmp_path, monkeypatch):
    """Anti-confabulation: an entity NOT in the known list comes back known=False
    and the echo warns, rather than being silently trusted."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    fake_classifier(monkeypatch, json.dumps({
        "intent": "client_note", "tags": [], "confidence": 0.8,
        "entity": {"name": "Urban Straps", "known": True},  # model CLAIMS known
        "tasks": [], "ideas": [], "note": "prefers minimalist branding",
        "reflection": None, "weekly": None, "correction": None, "clarify": None,
    }))
    c = classify.classify(cfg, conn, "Urban Straps prefers minimalist branding")
    # no such entity is known → classifier must override the model's claim
    assert c["entity"]["known"] is False
    result = dispatch.dispatch(cfg, conn, c)
    assert "new to me" in result["reply"].lower()
    conn.close()


def test_bad_due_date_dropped_not_guessed(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    fake_classifier(monkeypatch, json.dumps({
        "intent": "task", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [{"text": "do the thing", "due": "someday next quarter",
                   "priority": 0, "entity": None, "subtasks": []}],
        "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": None, "clarify": None,
    }))
    c = classify.classify(cfg, conn, "do the thing someday")
    assert c["tasks"][0]["due_ts"] is None  # unparseable date dropped, not invented
    conn.close()


def test_low_confidence_asks_before_storing(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake_classifier(monkeypatch, json.dumps({
        "intent": "task", "tags": [], "confidence": 0.3, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": None, "clarify": "Is that a task or just a note?",
    }))
    out = route.route(cfg, "roven stuff")
    assert out["pending"]["type"] == "clarify"
    assert "task or just a note" in out["reply"]
    conn = db.connect(cfg.db_path)
    # no task yet (waiting on clarification) BUT the raw text is persisted so
    # nothing is lost if the user wanders off
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM inbox WHERE raw_text = 'roven stuff'"
                        ).fetchone()[0] == 1
    conn.close()


def test_classifier_failure_still_persists_raw_text(tmp_path, monkeypatch):
    """Finding 2 regression: a model/parse failure must not lose the message."""
    cfg = cfg_for(tmp_path)
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True
    mod.drop_params = True

    def boom(model, messages, **kw):
        raise RuntimeError("T2 outage")
    mod.completion = boom
    mod.completion_cost = lambda completion_response=None: 0.0
    monkeypatch.setitem(sys.modules, "litellm", mod)

    out = route.route(cfg, "something important I typed")
    assert "saved" in out["reply"].lower() and out["pending"] is None
    conn = db.connect(cfg.db_path)
    assert conn.execute(
        "SELECT count(*) FROM inbox WHERE raw_text = 'something important I typed'"
    ).fetchone()[0] == 1
    conn.close()


def test_stale_pending_does_not_fire_delete(tmp_path, monkeypatch):
    """Finding 1 regression: a pending delete past its TTL must NOT execute on
    a later casual 'ok' — it routes as a fresh message instead."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'keep me', 'reflection')",
                 (db.now_ms(),))
    conn.commit()
    conn.close()
    stale = {"type": "delete", "refs": [{"table": "inbox", "id": 1}],
             "ts": db.now_ms() - 999999999}  # far past TTL
    fake_classifier(monkeypatch, json.dumps({
        "intent": "reflection", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": "ok noted", "weekly": None,
        "correction": None, "clarify": None}))
    route.resolve_pending(cfg, stale, "ok")
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM inbox WHERE raw_text='keep me'"
                        ).fetchone()[0] == 1  # NOT deleted
    conn.close()


def test_non_answer_during_pending_cancels_and_routes(tmp_path, monkeypatch):
    """Finding 4 regression: a real message sent mid-confirmation is routed,
    not swallowed as a 'no'."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'delete target', 'reflection')",
                 (db.now_ms(),))
    conn.commit()
    conn.close()
    pending = {"type": "delete", "refs": [{"table": "inbox", "id": 1}], "ts": db.now_ms()}
    fake_classifier(monkeypatch, json.dumps({
        "intent": "task", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [{"text": "buy milk", "due": None, "priority": 0, "entity": None,
                   "subtasks": []}],
        "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": None, "clarify": None}))
    out = route.resolve_pending(cfg, pending, "actually add task buy milk")
    conn = db.connect(cfg.db_path)
    # the delete did NOT happen, and the new task WAS created
    assert conn.execute("SELECT count(*) FROM inbox WHERE raw_text='delete target'"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM tasks WHERE text='buy milk'").fetchone()[0] == 1
    assert "cancelled" in out["reply"].lower()
    conn.close()


# ---------------------------------------------------------------- tasks

def test_task_lifecycle_and_views(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    pid = tasks.create_task(conn, "parent task", entity="Roven")
    tasks.create_task(conn, "child a", parent_id=pid, entity="Roven")
    tasks.create_task(conn, "solo", entity="Shunyethra")
    assert "Roven" in tasks.open_tasks(conn)
    assert "parent task" in tasks.open_tasks(conn)
    # closing the parent closes the child
    tasks.set_status(conn, pid, "done")
    remaining = conn.execute("SELECT count(*) FROM tasks WHERE status='open'").fetchone()[0]
    assert remaining == 1  # only 'solo' left
    conn.close()


# ---------------------------------------------------------------- healing

def _urban_straps_message(monkeypatch):
    return json.dumps({
        "intent": "client_note", "tags": [], "confidence": 0.8,
        "entity": {"name": "Urban Straps", "known": False},
        "tasks": [], "ideas": [], "note": "manages their salon social media",
        "reflection": None, "weekly": None, "correction": None, "clarify": None,
    })


def test_delete_heals_downstream_crm_and_client(tmp_path, monkeypatch):
    """Deleting the source message must remove the client note AND the
    auto-created client it spawned — no ghost 'Urban Straps' left behind."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    fake_classifier(monkeypatch, _urban_straps_message(monkeypatch))
    c = classify.classify(cfg, conn, "Urban Straps manages their salon social media")
    result = dispatch.dispatch(cfg, conn, c)
    inbox_id = result["inbox_id"]
    assert conn.execute("SELECT count(*) FROM crm WHERE name='Urban Straps'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM crm_notes").fetchone()[0] == 1

    ok, msg = memory_edit.delete_record(cfg, conn, inbox_id)
    assert ok
    # ghost fully purged: note, client, and inbox row all gone
    assert conn.execute("SELECT count(*) FROM crm_notes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM crm WHERE name='Urban Straps'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM inbox WHERE id=?", (inbox_id,)).fetchone()[0] == 0
    conn.close()


def test_delete_heals_skill_graph_node(tmp_path, monkeypatch):
    """Deleting a reflection removes the skill-graph node whose ONLY evidence
    was that reflection."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    fake_classifier(monkeypatch, json.dumps({
        "intent": "reflection", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None,
        "reflection": "learned about mediapipe pose landmarker", "weekly": None,
        "correction": None, "clarify": None,
    }))
    c = classify.classify(cfg, conn, "learned about mediapipe pose landmarker")
    result = dispatch.dispatch(cfg, conn, c)
    rid = [a["id"] for a in json.loads(
        conn.execute("SELECT artifacts FROM inbox WHERE id=?", (result["inbox_id"],)
                     ).fetchone()["artifacts"]) if a["table"] == "reflections"][0]
    # simulate extraction having created a graph node from this reflection
    from exocortex.extract.graph import upsert_edge, upsert_node
    n = upsert_node(conn, "mediapipe", "skill")
    d = upsert_node(conn, "computer vision", "domain")
    upsert_edge(conn, n, d, "in_domain", {"table": "reflections", "id": rid})
    conn.commit()
    assert conn.execute("SELECT count(*) FROM skill_nodes WHERE name='mediapipe'").fetchone()[0] == 1

    memory_edit.delete_record(cfg, conn, result["inbox_id"])
    # the skill node (sole evidence gone) is pruned; the domain survives
    assert conn.execute("SELECT count(*) FROM skill_nodes WHERE name='mediapipe'").fetchone()[0] == 0
    conn.close()


def test_node_with_other_evidence_survives_partial_delete(tmp_path, monkeypatch):
    """A node cited by TWO reflections keeps living when only one is deleted."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    fake_classifier(monkeypatch, json.dumps({
        "intent": "reflection", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": "used sqlite wal",
        "weekly": None, "correction": None, "clarify": None,
    }))
    c = classify.classify(cfg, conn, "used sqlite wal")
    r1 = dispatch.dispatch(cfg, conn, c)
    rid1 = [a["id"] for a in json.loads(
        conn.execute("SELECT artifacts FROM inbox WHERE id=?", (r1["inbox_id"],)
                     ).fetchone()["artifacts"]) if a["table"] == "reflections"][0]
    from exocortex.extract.graph import upsert_edge, upsert_node
    n = upsert_node(conn, "sqlite", "skill")
    d = upsert_node(conn, "databases", "domain")
    # two evidence pointers, only one is the record we'll delete
    upsert_edge(conn, n, d, "in_domain", {"table": "reflections", "id": rid1})
    upsert_edge(conn, n, d, "in_domain", {"table": "reflections", "id": 9999})
    conn.commit()
    memory_edit.delete_record(cfg, conn, r1["inbox_id"])
    assert conn.execute("SELECT count(*) FROM skill_nodes WHERE name='sqlite'").fetchone()[0] == 1
    edge = conn.execute("SELECT evidence FROM skill_edges").fetchone()
    ev = json.loads(edge["evidence"])
    assert {"table": "reflections", "id": 9999} in ev and len(ev) == 1
    conn.close()


# ---------------------------------------------------------------- NL correction

def test_nl_correction_shows_before_deleting_never_bulk_nukes(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    # two urban-straps records, one unrelated
    for txt in ["Urban Straps salon note", "note about Urban Straps gifting",
                "Roven wholesale idea"]:
        conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, ?, 'client_note')",
                     (db.now_ms(), txt))
    conn.commit()
    conn.close()
    fake_classifier(monkeypatch, json.dumps({
        "intent": "correction", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": {"action": "delete", "target": "urban straps notes",
                       "find": "urban straps", "replace_with": None}, "clarify": None,
    }), resolver_match="urban")
    out = route.route(cfg, "delete anything about urban straps")
    # must SHOW matches and wait, not delete
    assert out["pending"]["type"] == "delete"
    del_ids = {r["id"] for r in out["pending"]["refs"] if r["table"] == "inbox"}
    assert del_ids  # non-empty
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM inbox").fetchone()[0] == 3  # nothing deleted yet
    # the unrelated Roven record must NOT be in the delete set
    roven_id = conn.execute("SELECT id FROM inbox WHERE raw_text LIKE '%Roven%'").fetchone()["id"]
    assert roven_id not in del_ids
    conn.close()

    # confirm → deletes only the shown records
    res = route.resolve_pending(cfg, out["pending"], "yes")
    conn = db.connect(cfg.db_path)
    remaining = [r["raw_text"] for r in conn.execute("SELECT raw_text FROM inbox")]
    assert remaining == ["Roven wholesale idea"]
    conn.close()


def test_nl_correction_reaches_legacy_reflection(tmp_path, monkeypatch):
    """The real Urban Straps case: a pre-Phase-2.5 reflection (no inbox row)
    must still be findable and deletable by natural-language correction."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO reflections (ts, device, raw_text, insight_type,"
                 " extraction_status, inbox_id) VALUES (?, 'd',"
                 " 'research for urban straps salon business', 'weekly_outcome', 'done', NULL)",
                 (db.now_ms(),))
    conn.commit()
    conn.close()
    fake_classifier(monkeypatch, json.dumps({
        "intent": "correction", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": {"action": "delete", "target": "urban straps",
                       "find": "urban straps", "replace_with": None}, "clarify": None,
    }), resolver_match="urban")
    out = route.route(cfg, "delete anything about urban straps")
    assert out["pending"]["type"] == "delete"
    assert any(r["table"] == "reflections" for r in out["pending"]["refs"])
    route.resolve_pending(cfg, out["pending"], "yes")
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM reflections WHERE raw_text LIKE '%urban%'"
                        ).fetchone()[0] == 0
    conn.close()


def test_correction_matching_nothing_deletes_nothing(tmp_path, monkeypatch):
    """A 'correction' that resolves to no records must not delete anything."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO inbox (ts, raw_text, intent) VALUES (?, 'keep me', 'reflection')",
                 (db.now_ms(),))
    conn.commit()
    conn.close()
    fake_classifier(monkeypatch, json.dumps({
        "intent": "correction", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [], "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": {"action": "delete", "target": "nonexistent thing",
                       "find": "zzznotherematchzzz", "replace_with": None}, "clarify": None,
    }))
    out = route.route(cfg, "delete the nonexistent thing")
    assert out["pending"] is None
    assert "couldn't match" in out["reply"].lower()
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM inbox").fetchone()[0] == 1  # untouched
    conn.close()


def test_edit_reclassifies_and_heals_old(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    fake_classifier(monkeypatch, json.dumps({
        "intent": "task", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [{"text": "wrong task", "due": None, "priority": 0,
                   "entity": None, "subtasks": []}],
        "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": None, "clarify": None,
    }))
    c = classify.classify(cfg, conn, "wrong task")
    r = dispatch.dispatch(cfg, conn, c)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    # edit to a different task text — old task healed, new one created
    fake_classifier(monkeypatch, json.dumps({
        "intent": "task", "tags": [], "confidence": 0.9, "entity": None,
        "tasks": [{"text": "corrected task", "due": None, "priority": 0,
                   "entity": None, "subtasks": []}],
        "ideas": [], "note": None, "reflection": None, "weekly": None,
        "correction": None, "clarify": None,
    }))
    memory_edit.edit_record(cfg, conn, r["inbox_id"], "corrected task")
    rows = [t["text"] for t in conn.execute("SELECT text FROM tasks")]
    assert rows == ["corrected task"]  # old gone, new present
    conn.close()
