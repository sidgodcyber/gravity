"""Regression tests for the fresh-context review findings — every fixed leak
path gets a test so it can't come back."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import types

import pytest

from exocortex.config import Config
from exocortex.daemon.filter import Event, Firewall
from exocortex.daemon.pipeline import Pipeline
from exocortex.daemon.sources import browser
from exocortex.state import db
from exocortex.sync.main import SyncError, pull, push


def fw(blocklist=None, sensitive=None):
    return Firewall({"blocklist": blocklist or {}, "sensitive_titles": sensitive or []},
                    hostname="TEST-HOST")


def cfg_for(tmp_path):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["embeddings"]["enabled"] = False
    c.data["sync"]["passphrase"] = "regression test passphrase okay"
    return c


# ---- finding 2: clipboard attribution lag (alt-tab after copying) ----------

def test_clipboard_blocked_via_previous_window_app():
    """Copy in Slack, alt-tab to the editor before the next poll: the event
    arrives attributed to the editor but must still be dropped."""
    f = fw({"apps": ["slack"]})
    ev = Event(source="clipboard", content="the quarterly numbers",
               app="Code.exe", window_title="main.py — VS Code",
               alt_app="Slack.exe", alt_window_title="#general — Slack")
    assert not f.decide(ev, work_mode=False).allow


def test_clipboard_blocked_via_previous_window_title():
    f = fw({"window_titles": ["*jira*"]})
    ev = Event(source="clipboard", content="ticket text",
               app="chrome.exe", window_title="New Tab",
               alt_app="chrome.exe", alt_window_title="PROJ-1 - Jira")
    assert not f.decide(ev, work_mode=False).allow


# ---- finding 4: clipboard must fail closed without attribution -------------

def test_unattributed_clipboard_dropped():
    f = fw({"apps": ["slack"]})
    ev = Event(source="clipboard", content="mystery text")  # no app, no title
    d = f.decide(ev, work_mode=False)
    assert not d.allow and d.reason == "no_attribution"


# ---- findings 1 & 5: browser import floor + app attribution ----------------

def _make_history(path, visits):
    """Fake Chromium History db: visits = [(unix_us, url, title)]."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE urls (url TEXT, title TEXT, last_visit_time INTEGER)")
    conn.executemany(
        "INSERT INTO urls VALUES (?, ?, ?)",
        [(u, t, us + browser.WEBKIT_EPOCH_OFFSET_US) for us, u, t in visits],
    )
    conn.commit()
    conn.close()


def test_browser_import_carries_app_so_blocklist_applies(tmp_path, monkeypatch):
    hist = tmp_path / "History"
    now_us = int(time.time() * 1_000_000)
    _make_history(hist, [(now_us, "https://example.com/a", "A page")])
    monkeypatch.setattr(browser, "_chromium_profiles", lambda: [("edge", hist)])
    monkeypatch.setattr(browser, "_firefox_profiles", lambda: [])

    conn = db.connect(tmp_path / "t.db")
    p = Pipeline(conn, fw({"apps": ["msedge"]}), "dev")
    assert browser.import_history(conn, p) == 1  # submitted...
    p.flush()
    assert conn.execute("SELECT count(*) FROM life_stream").fetchone()[0] == 0  # ...but dropped
    conn.close()


def test_browser_import_skips_work_mode_history_forever(tmp_path, monkeypatch):
    hist = tmp_path / "History"
    now_us = int(time.time() * 1_000_000)
    work_visit = (now_us - 10_000_000, "https://internal.corp/secret", "Corp Wiki")
    later_visit = (now_us + 60_000_000, "https://arxiv.org/abs/1", "A paper")
    _make_history(hist, [work_visit, later_visit])
    monkeypatch.setattr(browser, "_chromium_profiles", lambda: [("chrome", hist)])
    monkeypatch.setattr(browser, "_firefox_profiles", lambda: [])

    conn = db.connect(tmp_path / "t.db")
    p = Pipeline(conn, fw(), "dev")
    # work mode was on while corp browsing happened, then turned off:
    db.set_work_mode(conn, True)
    db.set_work_mode(conn, False)   # raises the floor to now
    floor_us = int(db.get_control(conn, "browser_floor_us", "0") or 0)
    n = browser.import_history(conn, p, floor_us)
    p.flush()
    rows = [r["content"] for r in conn.execute("SELECT content FROM life_stream")]
    assert "https://internal.corp/secret" not in rows  # pre-floor visit is gone forever
    assert rows == ["https://arxiv.org/abs/1"] and n == 1
    conn.close()


def test_work_off_raises_browser_floor(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.get_control(conn, "browser_floor_us", "0") == "0"
    db.set_work_mode(conn, True)
    assert db.get_control(conn, "browser_floor_us", "0") == "0"  # only OFF raises it
    db.set_work_mode(conn, False)
    assert int(db.get_control(conn, "browser_floor_us")) > 0
    conn.close()


# ---- spec 5: sensitive rows never reach LLM contexts ------------------------

def test_sensitive_rows_excluded_from_ask_and_nightly(tmp_path):
    from exocortex.cycles.nightly import gather_day_activity
    from exocortex.retrieval.ask import gather_context
    conn = db.connect(tmp_path / "t.db")
    now = db.now_ms()
    conn.execute(
        "INSERT INTO life_stream (ts, device, source, content, sensitivity)"
        " VALUES (?, 'd', 'window', 'zebrafish research notes', 'normal')", (now,))
    conn.execute(
        "INSERT INTO life_stream (ts, device, source, content, sensitivity)"
        " VALUES (?, 'd', 'window', 'zebrafish bank statement login', 'sensitive')", (now,))
    conn.commit()
    chunks = gather_context(conn, "zebrafish")
    joined = "\n".join(chunks)
    assert "research notes" in joined
    assert "bank statement" not in joined
    activity = gather_day_activity(conn, 24)
    titles = " ".join(t for t, _ in activity["windows"])
    assert "bank statement" not in titles and "research notes" in titles
    conn.close()


# ---- spec 3: the daemon never imports litellm -------------------------------

def test_daemon_modules_are_litellm_free():
    code = (
        "import exocortex.daemon.main, exocortex.daemon.filter,"
        " exocortex.daemon.pipeline, exocortex.daemon.sources.window,"
        " exocortex.daemon.sources.clipboard, exocortex.daemon.sources.files,"
        " exocortex.daemon.sources.browser, sys;"
        " sys.exit(1 if 'litellm' in sys.modules else 0)"
    )
    r = None
    for attempt in range(4):  # Defender intermittently denies CreateProcess
        try:
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            break
        except OSError:
            time.sleep(1.0 + attempt)
    if r is None:
        pytest.skip("OS refused to spawn subprocesses (AV interference)")
    assert r.returncode == 0, f"daemon import graph pulls in litellm\n{r.stderr}"


# ---- finding 8: pull refuses while the daemon runs --------------------------

def test_pull_refuses_while_daemon_alive(tmp_path):
    cfg = cfg_for(tmp_path)
    db.connect(cfg.db_path).close()
    push(cfg)
    bundle = next((cfg.state_dir / "sync-out").glob("*.age"))
    indir = cfg.state_dir / "sync-in"
    indir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle, indir / bundle.name)
    (cfg.state_dir / "daemon_status.json").write_text(json.dumps(
        {"running": True, "pid": os.getpid(), "updated": time.time()}), encoding="utf-8")
    with pytest.raises(SyncError, match="daemon"):
        pull(cfg, force=True)
    (cfg.state_dir / "daemon_status.json").unlink()
    assert "db restored" in pull(cfg, force=True)


# ---- finding 10: paid usage is never recorded as $0 --------------------------

def test_unknown_paid_model_gets_conservative_cost(tmp_path, monkeypatch):
    from exocortex.router.llm import complete
    cfg = cfg_for(tmp_path)
    cfg.data["router"]["tiers"]["T2_workhorse"] = ["someprovider/mystery-model"]
    mod = types.ModuleType("litellm")
    mod.suppress_debug_info = True

    def completion(model, messages, **kw):
        usage = types.SimpleNamespace(prompt_tokens=1000, completion_tokens=1000)
        msg = types.SimpleNamespace(content="hi")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

    def completion_cost(completion_response=None):
        raise ValueError("unknown model")

    mod.completion = completion
    mod.completion_cost = completion_cost
    monkeypatch.setitem(sys.modules, "litellm", mod)

    conn = db.connect(cfg.db_path)
    complete(cfg, conn, "T2_workhorse", "t", [{"role": "user", "content": "x"}])
    cost = conn.execute("SELECT cost_usd FROM usage_ledger WHERE ok=1").fetchone()[0]
    assert cost > 0  # opus-level assumption, not $0
    conn.close()
