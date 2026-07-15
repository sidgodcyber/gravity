"""Concept engine + spaced repetition tests (LLM mocked; grounding validator
exercised with fabricated evidence)."""

import json
import sys
import types

import pytest

from exocortex.config import Config
from exocortex.curriculum import engine, spaced
from exocortex.state import db


def cfg_for(tmp_path):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["embeddings"]["enabled"] = False
    c.data["router"]["tiers"]["T2_workhorse"] = ["fake/t2"]
    c.data["router"]["tiers"]["T1_free"] = ["fake/t1"]
    return c


def seed_trail(local, days=4, per_day=3):
    """Recurring 'sqlite wal mode' lookups across several days + noise."""
    now = db.now_ms()
    for d in range(days):
        ts = now - d * 86400_000
        local.execute(
            "INSERT INTO life_stream (ts, device, source, content) VALUES (?, 'd', 'search', ?)",
            (ts, "sqlite wal mode checkpoint"),
        )
        local.execute(
            "INSERT INTO life_stream (ts, device, source, content) VALUES (?, 'd', 'ai_prompt', ?)",
            (ts, "why does sqlite wal mode need wal_checkpoint truncate"),
        )
        local.execute(
            "INSERT INTO life_stream (ts, device, source, content, extra) VALUES"
            " (?, 'd', 'browser', 'https://sqlite.org/wal.html', ?)",
            (ts, json.dumps({"title": "Write-Ahead Logging"})),
        )
    local.commit()


def fake_litellm(monkeypatch, analysis_reply, explainer_reply="{\"explainers\": []}",
                 fail_all=False):
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True
    mod.drop_params = True
    calls = []

    def completion(model, messages, **kw):
        if fail_all:
            raise AssertionError("LLM must not be called for this scenario")
        text = messages[-1]["content"]
        calls.append(text[:60])
        reply = analysis_reply if "curriculum engine" in text else explainer_reply
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=10)
        msg = types.SimpleNamespace(content=reply)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    mod.completion = completion
    mod.completion_cost = lambda completion_response=None: 0.0
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return calls


GOOD_ANALYSIS = json.dumps({
    "gaps": [
        {"concept": "sqlite wal mode", "domain": "databases",
         "why": "searched it on four separate days and asked AI about checkpoints",
         "evidence": [{"day": "2026-07-07", "quote": "sqlite wal mode checkpoint"}],
         "engagement": "surface", "score": 0.8},
        {"concept": "fabricated concept", "domain": "nowhere",
         "why": "the model made this up",
         "evidence": [{"day": "2026-07-07", "quote": "this quote is not in the trail"}],
         "engagement": "surface", "score": 0.9},
    ],
    "emerging_domains": [{"name": "databases", "note": "sustained", "days_active": 4}],
})

EXPLAINERS = json.dumps({"explainers": [{
    "concept": "sqlite wal mode",
    "explainer": "WAL mode writes changes to a log first...",
    "check_question": "What does wal_checkpoint(TRUNCATE) actually do?",
}]})


def test_engine_writes_grounded_gaps_and_drops_fabricated(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    local = db.local_connect(cfg)
    seed_trail(local)
    fake_litellm(monkeypatch, GOOD_ANALYSIS, EXPLAINERS)
    summary = engine.run_engine(cfg, conn, local)
    assert "1 gaps written" in summary and "1 dropped" in summary
    row = conn.execute("SELECT * FROM concepts").fetchone()
    assert row["name"] == "sqlite wal mode" and row["status"] == "gap"
    assert row["explainer"] and row["check_question"]
    # evidence stays in LOCAL db
    ev = local.execute("SELECT evidence FROM concept_evidence").fetchone()
    assert "sqlite wal mode checkpoint" in ev["evidence"]
    assert conn.execute(
        "SELECT count(*) FROM concepts WHERE name = 'fabricated concept'"
    ).fetchone()[0] == 0
    # recurring lookup pushed graph confidence DOWN from the 0.5 default
    node = conn.execute(
        "SELECT confidence FROM skill_nodes WHERE name = 'sqlite wal mode'"
    ).fetchone()
    assert node["confidence"] < 0.5
    # 'why' is trail-derived prose — it stays in LOCAL db, never syncs
    assert conn.execute(
        "SELECT why FROM concepts WHERE name = 'sqlite wal mode'"
    ).fetchone()["why"] == ""
    assert local.execute(
        "SELECT why FROM concept_evidence WHERE concept_name = 'sqlite wal mode'"
    ).fetchone()["why"] != ""
    conn.close(); local.close()


def test_recurring_lookup_lowers_confidence_across_nights(tmp_path, monkeypatch):
    """Finding 2 regression: a gap recurring on a second night must keep
    pushing confidence DOWN, not net upward via an automatic node bump."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    local = db.local_connect(cfg)
    seed_trail(local)
    fake_litellm(monkeypatch, GOOD_ANALYSIS, EXPLAINERS)
    engine.run_engine(cfg, conn, local)
    c1 = conn.execute("SELECT confidence FROM skill_nodes WHERE name='sqlite wal mode'"
                      ).fetchone()["confidence"]
    engine.run_engine(cfg, conn, local)   # second night, same recurring gap
    c2 = conn.execute("SELECT confidence FROM skill_nodes WHERE name='sqlite wal mode'"
                      ).fetchone()["confidence"]
    assert c2 < c1, f"confidence rose ({c1}->{c2}) on a repeated lookup"
    conn.close(); local.close()


def test_engine_honest_about_thin_trail(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    local = db.local_connect(cfg)  # empty trail
    fake_litellm(monkeypatch, GOOD_ANALYSIS, fail_all=True)  # LLM call = failure
    summary = engine.run_engine(cfg, conn, local)
    assert "not enough trail" in summary
    conn.close(); local.close()


def test_ask_states_absence_rather_than_confabulates(tmp_path, monkeypatch):
    """The Phase-1 failure mode: given a date but no deadline, /ask must not
    invent urgency. We assert the grounding instruction reaches the model and
    the model's honest 'I don't have that' passes through untouched."""
    from exocortex.retrieval import ask
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    local = db.local_connect(cfg)
    # shares keywords with the question so retrieval returns context and the
    # model path runs — but contains NO deadline for it to invent
    local.execute(
        "INSERT INTO life_stream (ts, device, source, content) VALUES"
        " (?, 'd', 'search', 'claude model access pricing on July 4')", (db.now_ms(),))
    local.commit()
    conn.close(); local.close()

    captured = {}
    def fake_completion(model, messages, **kw):
        captured["system"] = messages[0]["content"]
        import types
        usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        msg = types.SimpleNamespace(content="I don't have anything on that.")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True; mod.drop_params = True
    mod.completion = fake_completion
    mod.completion_cost = lambda completion_response=None: 0.0
    monkeypatch.setitem(sys.modules, "litellm", mod)

    out = ask.answer(cfg, "when is my model access deadline?")
    assert "don't have" in out.lower()
    # the grounding rule must actually be in the prompt sent to the model
    assert "never" in captured["system"].lower() and "deadline" in captured["system"].lower()


def test_validate_gaps_requires_verbatim_quotes():
    material = "## 2026-07-07\nsearches: css grid auto-fit | flexbox gap"
    gaps = [
        {"concept": "css grid", "evidence": [
            {"day": "2026-07-07", "quote": "css grid auto-fit"},
            {"day": "2026-07-07", "quote": "invented quote"},
        ]},
        {"concept": "ghost", "evidence": [{"day": "x", "quote": "not present"}]},
    ]
    kept, dropped = engine.validate_gaps(gaps, material)
    assert len(kept) == 1 and dropped == 1
    assert len(kept[0]["evidence"]) == 1


def test_graduated_concept_relapses_on_new_evidence(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    local = db.local_connect(cfg)
    seed_trail(local)
    now = db.now_ms()
    conn.execute(
        "INSERT INTO concepts (name, status, first_seen, last_seen) VALUES"
        " ('sqlite wal mode', 'graduated', ?, ?)", (now, now))
    conn.commit()
    fake_litellm(monkeypatch, GOOD_ANALYSIS, EXPLAINERS)
    engine.run_engine(cfg, conn, local)
    row = conn.execute("SELECT status, fumble_count FROM concepts"
                       " WHERE name = 'sqlite wal mode'").fetchone()
    assert row["status"] == "gap" and row["fumble_count"] == 1
    conn.close(); local.close()


# ------------------------------------------------------------ spaced repetition

def make_concept(conn, name="css grid", explainer="use fr units"):
    now = db.now_ms()
    conn.execute(
        "INSERT INTO concepts (name, status, gap_score, explainer,"
        " check_question, first_seen, last_seen) VALUES (?, 'gap', 0.9,"
        " ?, 'what is fr?', ?, ?)", (name, explainer, now, now))
    conn.commit()
    return conn.execute("SELECT id FROM concepts WHERE name = ?", (name,)).fetchone()["id"]


def test_surface_daily_once_and_schedules_review(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    make_concept(conn)
    row = spaced.surface_daily(cfg, conn)
    assert row["name"] == "css grid"
    again = spaced.surface_daily(cfg, conn)
    assert again is None                       # cooldown: one per brief
    c = conn.execute("SELECT * FROM concepts").fetchone()
    assert c["status"] == "reviewing" and c["next_review_ts"] > db.now_ms()
    conn.close()


def test_only_one_concept_surfaces_per_day(tmp_path):
    """Finding 3 regression: three /briefs in an afternoon must not burn three
    concepts — a global 20h gate caps it at one."""
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    make_concept(conn, name="css grid")
    make_concept(conn, name="flexbox")
    make_concept(conn, name="sqlite wal mode")
    first = spaced.surface_daily(cfg, conn)
    assert first is not None
    assert spaced.surface_daily(cfg, conn) is None
    assert spaced.surface_daily(cfg, conn) is None
    reviewing = conn.execute(
        "SELECT count(*) FROM concepts WHERE status = 'reviewing'"
    ).fetchone()[0]
    assert reviewing == 1
    conn.close()


def test_foundation_section_contains_explainer(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    local = db.local_connect(cfg)
    make_concept(conn)
    section = spaced.foundation_section(cfg, conn, local)
    assert "Foundation — css grid" in section and "use fr units" in section
    conn.close(); local.close()


def test_grade_ladder_to_graduation_and_fumble(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    cid = make_concept(conn)
    conn.execute("UPDATE concepts SET status='reviewing', review_stage=0 WHERE id=?", (cid,))
    conn.commit()
    for _ in range(3):
        reply = spaced.grade(cfg, conn, cid, "got_it")
    assert "graduated" not in reply
    reply = spaced.grade(cfg, conn, cid, "got_it")
    assert "graduated" in reply
    assert conn.execute("SELECT status FROM concepts WHERE id = ?", (cid,)
                        ).fetchone()["status"] == "graduated"
    node = conn.execute("SELECT confidence FROM skill_nodes WHERE name='css grid'"
                        ).fetchone()
    assert node["confidence"] > 0.5

    cid2 = make_concept(conn, name="flexbox")
    spaced.grade(cfg, conn, cid2, "nope")
    row = conn.execute("SELECT review_stage, fumble_count FROM concepts WHERE id = ?",
                       (cid2,)).fetchone()
    assert row["review_stage"] == 0 and row["fumble_count"] == 1
    conn.close()


def test_mark_learned_and_weekly_outcome(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    make_concept(conn, name="spectral calibration")
    assert "graduated" in spaced.mark_learned(cfg, conn, "spectral calibration")
    assert "recorded as understood" in spaced.mark_learned(cfg, conn, "brand new thing")
    conn.close()

    rid = spaced.store_weekly_outcome(cfg, "shipped the museum kiosk prototype")
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM reflections WHERE id = ?", (rid,)).fetchone()
    assert row["insight_type"] == "weekly_outcome"
    assert row["extraction_status"] == "done"   # marker type must survive
    conn.close()
