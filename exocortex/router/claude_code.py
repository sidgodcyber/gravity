"""Estimate Claude Code plan consumption from its local session transcripts.

Verified 2026-07-05 two ways, per the build spec:
- Docs (code.claude.com/docs/en/claude-directory): transcripts live at
  ``~/.claude/projects/<project>/<session>.jsonl`` ("full conversation
  transcript: every message, tool call, and tool result"), and are deleted
  after ``cleanupPeriodDays`` (default 30) — so estimates only cover that
  retention window, which is plenty for daily/weekly budgeting.
- Actual files on this machine: assistant lines carry ``message.model`` and
  ``message.usage`` (input/output/cache_read/cache_creation token counts),
  an ISO ``timestamp`` and a ``requestId``. Claude Code writes one line per
  content block and REPEATS the same usage — we dedupe on requestId or a
  single API response would be counted several times.

If Anthropic changes this format, the parser degrades to zero counts —
/budget says so, and the manual ``/spent`` command is the fallback.

Costs reported here are API-equivalent value (you pay a plan, not per
token); they exist to show pace, not to bill you.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import prices

log = logging.getLogger("exo.claude_code")


@dataclass
class UsageEvent:
    ts: dt.datetime
    model: str
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int

    @property
    def equiv_usd(self) -> float:
        return prices.estimate(
            self.model, self.tokens_in, self.tokens_out, self.cache_read, self.cache_write
        )


def scan_events(transcripts_dir: str | Path, since: dt.datetime) -> list[UsageEvent]:
    root = Path(os.path.expanduser(str(transcripts_dir)))
    if not root.is_dir():
        return []
    events: list[UsageEvent] = []
    seen: set[str] = set()
    since_stamp = since.timestamp()
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < since_stamp - 3600:
                continue  # untouched since before the window (1h slack)
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"assistant"' not in line or '"usage"' not in line:
                        continue  # cheap pre-filter before json parse
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    model = msg.get("model") or ""
                    if not usage or not model or model.startswith("<"):
                        continue
                    try:
                        ts = dt.datetime.fromisoformat(obj.get("timestamp", ""))
                    except ValueError:
                        continue
                    if ts.astimezone() < since.astimezone():
                        continue
                    rid = obj.get("requestId") or msg.get("id") or obj.get("uuid")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    events.append(
                        UsageEvent(
                            ts=ts,
                            model=model,
                            tokens_in=int(usage.get("input_tokens", 0) or 0),
                            tokens_out=int(usage.get("output_tokens", 0) or 0),
                            cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
                            cache_write=int(usage.get("cache_creation_input_tokens", 0) or 0),
                        )
                    )
        except OSError as e:
            log.warning("could not read %s: %s", path, e)
    return events


@dataclass
class UsageSummary:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    equiv_usd: float = 0.0
    by_model: dict = field(default_factory=lambda: defaultdict(int))

    def add(self, e: UsageEvent) -> None:
        self.calls += 1
        self.tokens_in += e.tokens_in
        self.tokens_out += e.tokens_out
        self.cache_read += e.cache_read
        self.cache_write += e.cache_write
        self.equiv_usd += e.equiv_usd
        self.by_model[e.model] += e.tokens_in + e.tokens_out

    @property
    def total_tokens(self) -> int:
        # cache reads are the bulk of agentic traffic; count real new tokens
        return self.tokens_in + self.tokens_out + self.cache_write


def summarize(events: list[UsageEvent], since: dt.datetime) -> UsageSummary:
    s = UsageSummary()
    for e in events:
        if e.ts.astimezone() >= since.astimezone():
            s.add(e)
    return s
