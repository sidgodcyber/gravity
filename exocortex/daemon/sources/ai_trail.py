"""Plane B capture: my prompts to AI tools (Claude Code session transcripts).

Verified on this machine 2026-07-07: real prompts are `type:"user"` lines in
~/.claude/projects/<slug>/<session>.jsonl with no isMeta / toolUseResult and
message.content as a string or text blocks. (The docs' history.jsonl does not
exist on this install — the VS Code extension doesn't write it — so
transcripts are the source; importing into local.db also outlives their
30-day cleanup.) Every prompt goes through the firewall pipeline: work mode
drops it, blocklist title patterns run against the text, and the session's
project path (cwd) runs against the path blocklist via extra["src_path"].
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from sqlite3 import Connection

from ...config import Config
from ...state import db
from ..filter import Event
from ..pipeline import Pipeline

log = logging.getLogger("exo.ai_trail")

CKPT_KEY = "ai_trail_ckpt_ms"
SKIP_PREFIXES = ("<command-", "<local-command", "<ide_", "<system-reminder")
MAX_CHARS = 2000


def _prompt_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "").strip() for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def import_prompts(cfg: Config, local_conn: Connection, pipeline: Pipeline) -> int:
    """Import new user prompts since the checkpoint. Returns prompts submitted
    (pre-firewall). In work mode this is a no-op that does NOT advance the
    checkpoint — otherwise pre-work-mode prompts would be silently lost."""
    if pipeline.work_mode:
        return 0
    root = Path(os.path.expanduser(cfg.get("claude_code.transcripts_dir")))
    if not root.is_dir():
        return 0
    ckpt_ms = int(db.get_control(local_conn, CKPT_KEY, "0") or 0)
    # uuids already stored recently — dedupe across overlapping scans
    seen = {
        json.loads(r["extra"]).get("uuid")
        for r in local_conn.execute(
            "SELECT extra FROM life_stream WHERE source = 'ai_prompt' AND ts >= ?",
            (ckpt_ms - 7 * 86400_000,),
        )
    }
    submitted = 0
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime * 1000 < ckpt_ms - 3600_000:
                continue
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"user"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (obj.get("type") != "user" or obj.get("isMeta")
                            or "toolUseResult" in obj or obj.get("isSidechain")):
                        continue
                    try:
                        import datetime as dt
                        ts_ms = int(dt.datetime.fromisoformat(
                            obj.get("timestamp", "")).timestamp() * 1000)
                    except ValueError:
                        continue
                    if ts_ms <= ckpt_ms:
                        continue
                    uuid = obj.get("uuid")
                    if not uuid or uuid in seen:
                        continue
                    text = _prompt_text((obj.get("message") or {}).get("content"))
                    if not text or text.startswith(SKIP_PREFIXES):
                        continue
                    seen.add(uuid)
                    pipeline.submit(Event(
                        source="ai_prompt",
                        content=text[:MAX_CHARS],
                        kind="claude-code",
                        ts=ts_ms,
                        extra={
                            "uuid": uuid,
                            "src_path": obj.get("cwd", ""),
                            "project": obj.get("slug", ""),
                        },
                    ))
                    submitted += 1
        except OSError as e:
            log.warning("could not read %s: %s", path, e)
    db.set_control(local_conn, CKPT_KEY, str(db.now_ms()))
    return submitted
