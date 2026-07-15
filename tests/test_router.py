import datetime as dt
import json
import sys
import types

import pytest

from exocortex.config import Config
from exocortex.router import budget, claude_code, prices
from exocortex.router.llm import BudgetExceeded, complete, parse_json_loose
from exocortex.state import db


def cfg_with(tmp_path, extra=None):
    c = Config.load(tmp_path / "nonexistent.yaml")  # defaults only
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["router"]["tiers"]["T1_free"] = ["fake/model-a", "fake/model-b"]
    c.data["router"]["tiers"]["T2_workhorse"] = ["anthropic/claude-sonnet-5"]
    if extra:
        c.data.update(extra)
    return c


def fake_litellm(monkeypatch, responses):
    """Install a fake litellm module; `responses` maps model -> text or Exception."""
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True
    calls = []

    def completion(model, messages, **kw):
        calls.append(model)
        r = responses[model]
        if isinstance(r, Exception):
            raise r
        usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20)
        msg = types.SimpleNamespace(content=r)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    def completion_cost(completion_response=None):
        return 0.0123

    mod.completion = completion
    mod.completion_cost = completion_cost
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return calls


def test_fallback_chain_and_ledger(tmp_path, monkeypatch):
    cfg = cfg_with(tmp_path)
    conn = db.connect(cfg.db_path)
    calls = fake_litellm(monkeypatch, {
        "fake/model-a": RuntimeError("rate limited"),
        "fake/model-b": "hello from b",
    })
    out = complete(cfg, conn, "T1_free", "test_task", [{"role": "user", "content": "hi"}])
    assert out == "hello from b"
    assert calls == ["fake/model-a", "fake/model-b"]
    rows = conn.execute("SELECT model, tier, task, ok, cost_usd FROM usage_ledger ORDER BY id").fetchall()
    assert [(r["model"], r["ok"]) for r in rows] == [("fake/model-a", 0), ("fake/model-b", 1)]
    assert rows[1]["task"] == "test_task"
    assert rows[1]["cost_usd"] == 0.0  # T1_free is recorded at $0
    conn.close()


def test_paid_tier_records_cost(tmp_path, monkeypatch):
    cfg = cfg_with(tmp_path)
    conn = db.connect(cfg.db_path)
    fake_litellm(monkeypatch, {"anthropic/claude-sonnet-5": "answer"})
    complete(cfg, conn, "T2_workhorse", "brief", [{"role": "user", "content": "x"}])
    row = conn.execute("SELECT cost_usd, provider FROM usage_ledger WHERE ok=1").fetchone()
    assert row["cost_usd"] == pytest.approx(0.0123)
    assert row["provider"] == "anthropic"
    conn.close()


def test_governor_blocks_paid_tiers_at_cap(tmp_path, monkeypatch):
    cfg = cfg_with(tmp_path)
    cfg.data["router"]["monthly_budget_usd"] = 1.0
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO usage_ledger (ts, provider, model, tier, task, tokens_in, tokens_out, cost_usd)"
        " VALUES (?, 'anthropic', 'm', 'T2_workhorse', 't', 1, 1, 1.5)",
        (db.now_ms(),),
    )
    conn.commit()
    fake_litellm(monkeypatch, {"anthropic/claude-sonnet-5": "should not run"})
    with pytest.raises(BudgetExceeded):
        complete(cfg, conn, "T2_workhorse", "brief", [{"role": "user", "content": "x"}])
    # free tier still works
    fake_litellm(monkeypatch, {"fake/model-a": "ok", "fake/model-b": "ok"})
    assert complete(cfg, conn, "T1_free", "t", [{"role": "user", "content": "x"}]) == "ok"
    conn.close()


def test_prices_lookup_and_cache_math():
    assert prices.lookup("anthropic/claude-sonnet-5") == (3.0, 15.0)
    assert prices.lookup("claude-opus-4-8") == (5.0, 25.0)
    assert prices.lookup("gemini/gemini-2.5-flash") is None
    # 1M in + 1M out on sonnet-5 = 3 + 15; cache: 1M read = 0.3, 1M write = 3.75
    assert prices.estimate("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(18.0)
    assert prices.estimate("claude-sonnet-5", 0, 0, 1_000_000, 1_000_000) == pytest.approx(0.3 + 3.75)


def test_parse_json_loose():
    assert parse_json_loose('{"a": 1}') == {"a": 1}
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Sure! Here you go: {"a": 1} hope that helps') == {"a": 1}


def test_claude_code_parser_dedupes_request_ids(tmp_path):
    tdir = tmp_path / "projects" / "proj"
    tdir.mkdir(parents=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    line = {
        "type": "assistant", "timestamp": ts, "requestId": "req_1",
        "message": {"model": "claude-opus-4-8",
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_read_input_tokens": 100,
                              "cache_creation_input_tokens": 50}},
    }
    other = dict(line, requestId="req_2")
    noise = {"type": "user", "timestamp": ts, "message": {"content": "hi"}}
    with open(tdir / "session.jsonl", "w", encoding="utf-8") as f:
        # same requestId written twice (one line per content block) + a distinct one
        f.write(json.dumps(line) + "\n")
        f.write(json.dumps(line) + "\n")
        f.write(json.dumps(other) + "\n")
        f.write(json.dumps(noise) + "\n")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    events = claude_code.scan_events(tmp_path / "projects", since)
    assert len(events) == 2
    s = claude_code.summarize(events, since)
    assert s.tokens_in == 20 and s.cache_read == 200 and s.cache_write == 100
    assert s.total_tokens == 20 + 10 + 100  # in + out + cache_write
    assert s.equiv_usd > 0


def test_budget_report_runs_without_keys(tmp_path):
    cfg = cfg_with(tmp_path)
    cfg.data["claude_code"]["transcripts_dir"] = str(tmp_path / "empty")
    report = budget.budget_report(cfg)
    assert "Budget" in report and "Advice" in report
