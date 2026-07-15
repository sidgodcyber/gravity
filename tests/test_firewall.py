"""Adversarial tests for the work firewall. A failure here is a privacy leak."""

import os
import subprocess
import sys
import time

import pytest

from exocortex.daemon.filter import Event, Firewall, redact_secrets
from exocortex.daemon.pipeline import Pipeline
from exocortex.state import db

HOST = "TEST-HOST"


def fw(blocklist=None, sensitive=None, hosts=None, hostname=HOST, redact=True):
    return Firewall(
        {
            "blocklist": blocklist or {},
            "sensitive_titles": sensitive or [],
            "work_mode_hosts": hosts or [],
            "redact_secrets": redact,
        },
        hostname=hostname,
    )


def make_dir_link(link, target):
    """Symlink if permitted, else an NTFS junction (no admin needed). Returns
    False when the platform allows neither. Subprocess spawning is retried:
    Defender on this machine intermittently denies CreateProcess."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        if os.name != "nt":
            return False
        for attempt in range(4):
            try:
                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    capture_output=True,
                )
                return r.returncode == 0
            except OSError:
                time.sleep(1.0 + attempt)
        return False


# ---------------------------------------------------------------- paths

def test_blocked_path_direct(tmp_path):
    company = tmp_path / "company"
    company.mkdir()
    f = fw({"paths": [str(company)]})
    ev = Event(source="file", content=str(company / "secret" / "plan.docx"))
    assert not f.decide(ev, work_mode=False).allow


def test_blocked_path_via_symlink(tmp_path):
    """Adversarial: a link that resolves INTO a blocklisted dir must be caught."""
    company = tmp_path / "company"
    company.mkdir()
    link = tmp_path / "innocent"
    if not make_dir_link(link, company):
        pytest.skip("cannot create symlink or junction on this system")
    f = fw({"paths": [str(company)]})
    ev = Event(source="file", content=str(link / "client_list.xlsx"))
    assert not f.decide(ev, work_mode=False).allow


def test_blocklist_entry_that_is_itself_a_symlink(tmp_path):
    """Adversarial: the CONFIG entry is a link; events under its real target
    must still be blocked."""
    real = tmp_path / "real_company_dir"
    real.mkdir()
    link = tmp_path / "worklink"
    if not make_dir_link(link, real):
        pytest.skip("cannot create symlink or junction on this system")
    f = fw({"paths": [str(link)]})
    ev = Event(source="file", content=str(real / "roadmap.pptx"))
    assert not f.decide(ev, work_mode=False).allow


def test_path_case_insensitive(tmp_path):
    company = tmp_path / "Company"
    company.mkdir()
    f = fw({"paths": [str(company).lower()]})
    ev = Event(source="file", content=str(tmp_path / "COMPANY" / "x.txt"))
    assert not f.decide(ev, work_mode=False).allow


def test_path_prefix_does_not_overmatch_siblings(tmp_path):
    """C:/work must never block C:/workspace2."""
    f = fw({"paths": [str(tmp_path / "work")]})
    ev = Event(source="file", content=str(tmp_path / "workspace2" / "notes.md"))
    assert f.decide(ev, work_mode=False).allow


def test_unparseable_path_fails_closed():
    f = fw({"paths": ["C:/work"]})
    ev = Event(source="file", content="\x00bad\x00path")
    d = f.decide(ev, work_mode=False)
    # either judged blocked or dropped as error — it must not be stored
    if d.allow:
        pytest.fail("unparseable path was allowed through")


# ---------------------------------------------------------------- apps

def test_blocked_app_with_renamed_window_title():
    """Adversarial: matching is on process name; a renamed title can't hide it."""
    f = fw({"apps": ["slack"]})
    ev = Event(source="window", content="Totally Personal Chat", app="Slack.exe")
    assert not f.decide(ev, work_mode=False).allow


def test_blocked_app_catches_clipboard_from_that_app():
    f = fw({"apps": ["teams"]})
    ev = Event(source="clipboard", content="quarterly numbers...",
               app="ms-teams.exe" if False else "Teams.exe",
               window_title="random")
    assert not f.decide(ev, work_mode=False).allow


def test_app_match_exe_suffix_both_directions():
    assert not fw({"apps": ["outlook.exe"]}).decide(
        Event(source="window", content="Inbox", app="OUTLOOK"), False).allow
    assert not fw({"apps": ["outlook"]}).decide(
        Event(source="window", content="Inbox", app="outlook.EXE"), False).allow


def test_app_match_is_exact_not_substring():
    """'teams' must not block 'steamsync.exe'."""
    f = fw({"apps": ["teams"]})
    ev = Event(source="window", content="Steam", app="steamsync.exe")
    assert f.decide(ev, work_mode=False).allow


# ---------------------------------------------------------------- titles & urls

def test_blocked_title_glob_case_insensitive():
    f = fw({"window_titles": ["*AcmeCorp*"]})
    ev = Event(source="window", content="planning doc — acmecorp intranet")
    assert not f.decide(ev, work_mode=False).allow


def test_bare_word_pattern_means_substring():
    f = fw({"window_titles": ["jira"]})
    ev = Event(source="window", content="PROJ-123 - JIRA Board")
    assert not f.decide(ev, work_mode=False).allow


def test_clipboard_from_blocked_window_title():
    """Copying out of a blocklisted window is blocked even though the
    clipboard text itself is innocuous."""
    f = fw({"window_titles": ["*confluence*"]})
    ev = Event(source="clipboard", content="meeting notes",
               window_title="Company Wiki - Confluence")
    assert not f.decide(ev, work_mode=False).allow


def test_blocked_url_pattern():
    f = fw({"url_patterns": ["*acme.com*", "*mail.google.com/mail/u/1*"]})
    assert not f.decide(
        Event(source="browser", content="https://issues.acme.com/PROJ-9"), False).allow
    assert not f.decide(
        Event(source="browser", content="https://mail.google.com/mail/u/1/#inbox"), False).allow
    assert f.decide(
        Event(source="browser", content="https://mail.google.com/mail/u/0/#inbox"), False).allow


def test_browser_page_title_checked_against_title_blocklist():
    f = fw({"window_titles": ["*acme*"]})
    ev = Event(source="browser", content="https://random.site/page",
               extra={"title": "Acme Q3 Planning"})
    assert not f.decide(ev, work_mode=False).allow


# ---------------------------------------------------------------- work mode

def test_work_mode_drops_everything_except_manual():
    f = fw()
    for source in ["window", "clipboard", "file", "browser"]:
        assert not f.decide(Event(source=source, content="x"), work_mode=True).allow
    assert f.decide(Event(source="manual", content="learned a thing"), True).allow


def test_forced_work_mode_host_ignores_toggle():
    f = fw(hosts=[HOST.lower()])  # case-insensitive host match
    assert f.forced_work_mode
    assert not f.decide(Event(source="window", content="anything"), work_mode=False).allow
    assert f.decide(Event(source="manual", content="note"), work_mode=False).allow


def test_work_mode_toggle_mid_session_purges_queue(tmp_path):
    """Adversarial: clipboard captured moments before work mode turns on must
    never be written."""
    conn = db.connect(tmp_path / "t.db")
    p = Pipeline(conn, fw(), "dev")
    p.submit(Event(source="clipboard", content="pre-toggle paste", window_title="w"))
    p.submit(Event(source="manual", content="deliberate note"))
    assert p.pending() == 2
    p.set_work_mode(True)          # purge happens here
    assert p.pending() == 1        # only the manual note survives
    p.flush()
    rows = conn.execute("SELECT source, content FROM life_stream").fetchall()
    assert [(r["source"], r["content"]) for r in rows] == [("manual", "deliberate note")]
    conn.close()


def test_flush_recheck_drops_even_without_explicit_purge(tmp_path):
    """Even if the toggle path forgot to purge, flush re-checks work mode."""
    conn = db.connect(tmp_path / "t.db")
    p = Pipeline(conn, fw(), "dev")
    p.submit(Event(source="clipboard", content="sneaky", window_title="w"))
    p.work_mode = True             # simulate a raw flag flip with no purge call
    p.flush()
    assert conn.execute("SELECT count(*) FROM life_stream").fetchone()[0] == 0
    conn.close()


def test_submit_while_work_mode_on_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    p = Pipeline(conn, fw(), "dev")
    p.set_work_mode(True)
    assert not p.submit(Event(source="window", content="title"))
    assert p.submit(Event(source="manual", content="note"))
    conn.close()


# ---------------------------------------------------------------- sensitivity & secrets

def test_sensitive_title_stored_but_tagged():
    f = fw(sensitive=["*bank*"])
    d = f.decide(Event(source="window", content="Chase Bank - Login"), False)
    assert d.allow and d.sensitivity == "sensitive"


def test_clipboard_from_sensitive_window_tagged():
    f = fw(sensitive=["*password*"])
    d = f.decide(Event(source="clipboard", content="hunter2",
                       window_title="KeePass - Password Manager"), False)
    assert d.allow and d.sensitivity == "sensitive"


def test_clipboard_secret_redaction():
    f = fw()
    d = f.decide(Event(source="clipboard",
                       content="key is sk-ant-api03-abcdefghijklmnop1234 ok",
                       app="notepad.exe", window_title="notes.txt"), False)
    assert "sk-ant" not in d.content and "[REDACTED:api-key]" in d.content


def test_private_key_block_fully_redacted():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
    assert redact_secrets(text) == "[REDACTED:private-key]"


def test_redaction_covers_common_token_shapes():
    samples = {
        "aws-key": "AKIAIOSFODNN7EXAMPLE",
        "github-token": "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "slack-token": "xoxb-123456789012-abcdefghijklmnop",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    }
    for label, sample in samples.items():
        out = redact_secrets(f"before {sample} after")
        assert sample not in out, label
        assert f"[REDACTED:{label}]" in out, label


# ---------------------------------------------------------------- pipeline basics

def test_pipeline_batches_and_counts(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    p = Pipeline(conn, fw({"apps": ["slack"]}), "laptop")
    p.submit(Event(source="window", content="editor - main.py", app="code.exe"))
    p.submit(Event(source="window", content="hidden", app="slack.exe"))  # dropped
    assert p.pending() == 1
    assert p.flush() == 1
    assert p.dropped == 1 and p.written == 1
    row = conn.execute("SELECT * FROM life_stream").fetchone()
    assert row["device"] == "laptop" and row["source"] == "window"
    conn.close()
