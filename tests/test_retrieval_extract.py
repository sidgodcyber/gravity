import json
import sys
import types

from exocortex.config import Config
from exocortex.extract.graph import upsert_edge, upsert_node
from exocortex.extract.reflections import add_reflection, extract_pending
from exocortex.retrieval.embeddings import cosine, fts_terms, pack, unpack
from exocortex.retrieval.style import import_samples
from exocortex.state import db


def cfg_for(tmp_path):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["embeddings"]["enabled"] = False  # unit tests never download models
    c.data["router"]["tiers"]["T1_free"] = ["fake/extractor"]
    return c


def install_fake_litellm(monkeypatch, reply_text):
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True

    def completion(model, messages, **kw):
        usage = types.SimpleNamespace(prompt_tokens=50, completion_tokens=30)
        msg = types.SimpleNamespace(content=reply_text)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    mod.completion = completion
    mod.completion_cost = lambda completion_response=None: 0.0
    monkeypatch.setitem(sys.modules, "litellm", mod)


def test_vector_pack_roundtrip_and_cosine():
    v = [0.1, -0.5, 2.0]
    assert [round(x, 5) for x in unpack(pack(v))] == v
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0


def test_fts_terms_sanitizes():
    q = 'what did I learn about the "museum exhibit" API design?'
    terms = fts_terms(q)
    assert '"museum"' in terms and '"exhibit"' in terms and " OR " in terms
    assert "what" not in terms  # stopword dropped
    assert fts_terms("a of I") == ""


def test_style_import_dedupes(tmp_path):
    cfg = cfg_for(tmp_path)
    d = tmp_path / "samples"
    d.mkdir()
    (d / "one.md").write_text("Hey! Loved your gallery post — here's an idea.", encoding="utf-8")
    (d / "two.txt").write_text("Quick update on the exhibit copy deck.", encoding="utf-8")
    (d / "skip.pdf").write_bytes(b"%PDF")
    assert import_samples(cfg, str(d), kind="message") == 2
    assert import_samples(cfg, str(d), kind="message") == 0  # dedupe on exact text
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM style_corpus").fetchone()[0] == 2
    conn.close()


def test_reflection_extraction_updates_graph(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    install_fake_litellm(monkeypatch, json.dumps({
        "skills": ["python", "sqlite"],
        "domain": "software",
        "project": "Exocortex",
        "insight_type": "learned",
        "portfolio_score": 0.7,
    }))
    conn = db.connect(cfg.db_path)
    rid = add_reflection(conn, cfg, "Learned WAL mode makes concurrent sqlite writes safe")
    row = conn.execute("SELECT * FROM reflections WHERE id = ?", (rid,)).fetchone()
    assert row["extraction_status"] == "done"
    assert json.loads(row["skills"]) == ["python", "sqlite"]
    assert row["domain"] == "software" and row["portfolio_score"] == 0.7
    nodes = {(r["name"], r["type"]) for r in conn.execute("SELECT name, type FROM skill_nodes")}
    assert {("python", "skill"), ("sqlite", "skill"), ("software", "domain"),
            ("Exocortex", "project")} <= nodes
    edge = conn.execute(
        "SELECT evidence FROM skill_edges WHERE rel = 'demonstrated_in' LIMIT 1"
    ).fetchone()
    assert json.loads(edge["evidence"]) == [{"table": "reflections", "id": rid}]
    conn.close()


def test_failed_extraction_stays_retryable(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    install_fake_litellm(monkeypatch, "sorry, I can't produce JSON today")
    conn = db.connect(cfg.db_path)
    rid = add_reflection(conn, cfg, "note to self")
    assert conn.execute(
        "SELECT extraction_status FROM reflections WHERE id = ?", (rid,)
    ).fetchone()[0] == "failed"
    install_fake_litellm(monkeypatch, '{"skills": [], "domain": null, "project": null,'
                                      ' "insight_type": "observation", "portfolio_score": 0}')
    assert extract_pending(conn, cfg) == 1
    assert conn.execute(
        "SELECT extraction_status FROM reflections WHERE id = ?", (rid,)
    ).fetchone()[0] == "done"
    conn.close()


def test_graph_upserts_strengthen_not_duplicate(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = db.connect(cfg.db_path)
    a = upsert_node(conn, "python", "skill")
    b = upsert_node(conn, "software", "domain")
    assert upsert_node(conn, "python", "skill") == a
    upsert_edge(conn, a, b, "in_domain", {"table": "reflections", "id": 1})
    upsert_edge(conn, a, b, "in_domain", {"table": "reflections", "id": 2})
    assert conn.execute("SELECT count(*) FROM skill_nodes WHERE name='python'").fetchone()[0] == 1
    edges = conn.execute("SELECT weight, evidence FROM skill_edges").fetchall()
    assert len(edges) == 1
    assert edges[0]["weight"] > 0.5
    assert len(json.loads(edges[0]["evidence"])) == 2
    conn.close()
