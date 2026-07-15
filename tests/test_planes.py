"""Adversarial tests for the two-plane model. A failure here means either
company code became readable (Plane A) or trail data could leave the machine
(Plane B) — the two failures with real consequences."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import time

import pytest

from exocortex.config import Config
from exocortex.daemon.allowlist import is_allowed, read_text_allowed, resolved_roots
from exocortex.daemon.filter import Event, Firewall
from exocortex.daemon.pipeline import Pipeline
from exocortex.daemon.sources import ai_trail, git_activity
from exocortex.daemon.sources.browser import extract_search_query
from exocortex.state import db
from exocortex.sync.main import push


def cfg_for(tmp_path, allow=None):
    c = Config.load(tmp_path / "none.yaml")
    c.data["state_dir"] = str(tmp_path / "state")
    c.data["embeddings"]["enabled"] = False
    c.data["sync"]["passphrase"] = "plane separation test phrase"
    c.data["capture_code_from"] = [str(p) for p in (allow or [])]
    return c


def fw(blocklist=None):
    return Firewall({"blocklist": blocklist or {}}, hostname="T")


def make_dir_link(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        if os.name != "nt":
            return False
        for attempt in range(4):
            try:
                r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                   capture_output=True)
                return r.returncode == 0
            except OSError:
                time.sleep(1.0 + attempt)
        return False


def run_git(repo, *args):
    for attempt in range(4):
        try:
            return subprocess.run(
                ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                 *args], capture_output=True, text=True)
        except OSError:
            time.sleep(1.0 + attempt)
    pytest.skip("OS refused to spawn git (AV interference)")


# ---------------------------------------------------------- Plane B: local-only

CANARY = "PLANE-B-CANARY-must-never-leave-this-machine-9f2c"


def test_sync_bundle_structurally_excludes_local_db(tmp_path):
    import pyrage
    cfg = cfg_for(tmp_path)
    db.connect(cfg.db_path).close()                     # synced brain exists
    local = db.local_connect(cfg)
    local.execute(
        "INSERT INTO life_stream (ts, device, source, content) VALUES (?, 'd', 'search', ?)",
        (db.now_ms(), CANARY),
    )
    local.commit()
    local.close()

    push(cfg)
    bundle = next((cfg.state_dir / "sync-out").glob("*.age"))
    raw = bundle.read_bytes()
    assert CANARY.encode() not in raw                   # encrypted, and...
    decrypted = pyrage.passphrase.decrypt(raw, "plane separation test phrase")
    assert CANARY.encode() not in decrypted             # ...not inside either
    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "state/local.db" not in names
    assert names <= {"state/exocortex.db", "config.yaml"}


def test_split_plane_b_moves_trail_rows_idempotently(tmp_path):
    cfg = cfg_for(tmp_path)
    main = db.connect(cfg.db_path)
    now = db.now_ms()
    main.execute("INSERT INTO life_stream (ts, device, source, content) VALUES"
                 f" ({now}, 'd', 'window', 'trail row')")
    main.execute("INSERT INTO life_stream (ts, device, source, content) VALUES"
                 f" ({now}, 'd', 'manual', 'deliberate note')")
    main.commit()
    main.close()

    assert db.split_plane_b(cfg) == 1
    assert db.split_plane_b(cfg) == 0                   # idempotent
    main = db.connect(cfg.db_path)
    rows = [r["source"] for r in main.execute("SELECT source FROM life_stream")]
    assert rows == ["manual"]                           # only deliberate entries stay
    main.close()
    local = db.local_connect(cfg)
    assert local.execute(
        "SELECT count(*) FROM life_stream WHERE content = 'trail row'"
    ).fetchone()[0] == 1
    local.close()


# ---------------------------------------------------------- Plane A: allowlist

def test_allowlist_denies_everything_by_default(tmp_path):
    cfg = cfg_for(tmp_path, allow=[])
    assert resolved_roots(cfg) == []
    assert not is_allowed(str(tmp_path / "anything"), [])


def test_allowlist_allows_inside_denies_outside(tmp_path):
    allowed = tmp_path / "mine"
    allowed.mkdir()
    other = tmp_path / "employer"
    other.mkdir()
    cfg = cfg_for(tmp_path, allow=[allowed])
    roots = resolved_roots(cfg)
    assert is_allowed(allowed / "src" / "x.py", roots)
    assert not is_allowed(other / "secret.py", roots)
    assert not is_allowed(str(tmp_path / "mine2" / "x.py"), roots)  # sibling prefix
    assert not is_allowed("relative/path.py", roots)
    assert not is_allowed("\x00junk", roots)


def test_allowlist_symlink_out_does_not_widen(tmp_path):
    """Adversarial: a link INSIDE the allowlisted tree pointing outside it
    must not make outside content readable."""
    allowed = tmp_path / "mine"
    allowed.mkdir()
    outside = tmp_path / "employer"
    outside.mkdir()
    (outside / "secret.txt").write_text("company secret", encoding="utf-8")
    link = allowed / "sneaky"
    if not make_dir_link(link, outside):
        pytest.skip("no symlink/junction support")
    cfg = cfg_for(tmp_path, allow=[allowed])
    roots = resolved_roots(cfg)
    assert not is_allowed(link / "secret.txt", roots)
    with pytest.raises(Exception):
        read_text_allowed(link / "secret.txt", cfg)


def test_read_text_allowed_reads_only_allowlisted(tmp_path):
    allowed = tmp_path / "mine"
    allowed.mkdir()
    (allowed / "notes.txt").write_text("my own text", encoding="utf-8")
    cfg = cfg_for(tmp_path, allow=[allowed])
    assert read_text_allowed(allowed / "notes.txt", cfg) == "my own text"
    with pytest.raises(Exception):
        read_text_allowed(tmp_path / "none.yaml", cfg)


def test_git_scanner_never_touches_unlisted_repo(tmp_path):
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    for repo, fname in [(mine, "a.py"), (theirs, "b.py")]:
        repo.mkdir()
        r = run_git(repo, "init", "-b", "main")
        if r.returncode != 0:
            pytest.skip("git init failed")
        (repo / fname).write_text("x = 1", encoding="utf-8")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", f"work in {repo.name}")
    cfg = cfg_for(tmp_path, allow=[mine])
    conn = db.connect(cfg.db_path)
    added = git_activity.scan_repos(cfg, conn)
    repos = {r["repo"] for r in conn.execute("SELECT repo FROM code_activity")}
    assert added == 1
    assert repos == {mine.resolve().as_posix()}
    assert not any("theirs" in r for r in repos)
    conn.close()


# ---------------------------------------------------------- Plane B: firewall

def test_ai_prompt_from_blocklisted_project_dropped(tmp_path):
    work = tmp_path / "companyrepo"
    work.mkdir()
    f = fw({"paths": [str(work)]})
    ev = Event(source="ai_prompt", content="fix the auth bug in our service",
               extra={"src_path": str(work / "subdir")})
    assert not f.decide(ev, work_mode=False).allow
    ok = Event(source="ai_prompt", content="explain sqlite wal mode",
               extra={"src_path": str(tmp_path / "personal")})
    assert f.decide(ok, work_mode=False).allow


def test_ai_prompt_content_hits_title_blocklist():
    f = fw({"window_titles": ["*acmecorp*"]})
    ev = Event(source="ai_prompt", content="refactor the AcmeCorp billing module")
    assert not f.decide(ev, work_mode=False).allow


def test_search_from_blocklisted_url_dropped():
    """Finding 4 regression: a search derived from a blocklisted URL must be
    dropped even though its query text alone looks innocent."""
    f = fw({"url_patterns": ["*jira*"]})
    ev = Event(source="search", content="acme credentials rotation",
               extra={"url": "https://www.google.com/search?q=acme+jira+creds"})
    assert not f.decide(ev, work_mode=False).allow
    ok = Event(source="search", content="css grid auto-fit",
               extra={"url": "https://www.google.com/search?q=css+grid"})
    assert f.decide(ok, work_mode=False).allow


def test_search_query_extraction():
    assert extract_search_query(
        "https://www.google.com/search?q=sqlite+wal+mode&hl=en") == "sqlite wal mode"
    assert extract_search_query(
        "https://duckduckgo.com/?q=css%20grid%20auto-fit") == "css grid auto-fit"
    assert extract_search_query(
        "https://www.youtube.com/results?search_query=spectroscopy+basics"
    ) == "spectroscopy basics"
    assert extract_search_query("https://example.com/page?q=nope") == ""


def test_ai_trail_import_and_dedupe(tmp_path):
    tdir = tmp_path / "transcripts" / "proj"
    tdir.mkdir(parents=True)
    import datetime as dt
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": ts, "cwd": str(tmp_path),
         "message": {"content": "how does sqlite wal mode work?"}},
        {"type": "user", "uuid": "u2", "timestamp": ts, "cwd": str(tmp_path),
         "message": {"content": [{"type": "text", "text": "explain fts5 triggers"}]}},
        {"type": "user", "uuid": "u3", "timestamp": ts, "isMeta": True,
         "message": {"content": "meta noise"}},
        {"type": "user", "uuid": "u4", "timestamp": ts, "toolUseResult": {},
         "message": {"content": [{"type": "tool_result"}]}},
        {"type": "user", "uuid": "u5", "timestamp": ts,
         "message": {"content": "<command-name>/model</command-name>"}},
    ]
    with open(tdir / "s.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(l) + "\n" for l in lines)
    cfg = cfg_for(tmp_path)
    cfg.data["claude_code"]["transcripts_dir"] = str(tmp_path / "transcripts")
    local = db.local_connect(cfg)
    p = Pipeline(local, fw(), "dev")
    assert ai_trail.import_prompts(cfg, local, p) == 2   # u1 + u2 only
    p.flush()
    rows = [r["content"] for r in local.execute(
        "SELECT content FROM life_stream WHERE source = 'ai_prompt' ORDER BY id")]
    assert rows == ["how does sqlite wal mode work?", "explain fts5 triggers"]
    assert ai_trail.import_prompts(cfg, local, p) == 0   # checkpoint + uuid dedupe
    local.close()
