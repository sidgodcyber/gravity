"""The work firewall.

Every captured event must pass ``Firewall.decide`` before anything touches
disk. There is deliberately exactly one decision function; capture sources
never write to the database directly (see pipeline.py).

Design rules, in priority order:
1. Fail closed: on any doubt or error, DROP.
2. Work mode (or a forced work-mode host) drops everything except manual
   /log reflections.
3. Blocklist matching is case-insensitive everywhere. Paths are matched on
   BOTH their literal and symlink-resolved forms, component-wise (so
   ``C:/work`` never accidentally matches ``C:/workspace2``). Apps are
   matched on process executable name, not window title, so a renamed
   window cannot hide a blocklisted app.
4. Patterns without glob characters are treated as substrings — a config
   entry like "jira" means "*jira*".
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

MANUAL = "manual"

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")),
]
PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


@dataclass(frozen=True)
class Event:
    """A capture event, before it has been judged."""

    source: str                # window | clipboard | file | browser | manual
    content: str               # window title / clipboard text / file path / url
    app: str = ""              # foreground process executable name
    window_title: str = ""     # foreground window title (clipboard/browser carry this)
    kind: str = ""             # source subtype (file event type, browser name, ...)
    ts: int = 0                # ORIGINAL event time (epoch ms); 0 = now. Imports
                               # (browser history, AI prompts) must set this or the
                               # curriculum engine's across-days signal collapses.
    # A clipboard change is only *detected* at poll time; the copy happened up
    # to one poll earlier, possibly in the previously focused window. Both
    # attributions are checked against the blocklist.
    alt_app: str = ""
    alt_window_title: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    allow: bool
    content: str = ""
    sensitivity: str = "normal"
    reason: str = ""           # for tests and --status counters; never stored


def _norm_app(name: str) -> str:
    base = os.path.basename(name.strip()).casefold()
    return base[:-4] if base.endswith(".exe") else base


def _path_parts(p: str | Path) -> tuple[str, ...]:
    return tuple(part.casefold().rstrip("\\/") for part in Path(p).parts)


def _path_variants(raw: str) -> list[tuple[str, ...]]:
    """Literal and symlink-resolved component tuples for a path."""
    expanded = Path(os.path.expanduser(str(raw)))
    variants = [_path_parts(expanded)]
    try:
        resolved = expanded.resolve(strict=False)
        parts = _path_parts(resolved)
        if parts not in variants:
            variants.append(parts)
    except OSError:
        pass  # keep the literal variant; caller still fails closed on match
    return variants


def _prefix_match(path_parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(prefix) > 0 and path_parts[: len(prefix)] == prefix


def _pattern_hit(text: str, patterns: list[str]) -> bool:
    t = text.casefold()
    for pat in patterns:
        if any(c in pat for c in "*?["):
            if fnmatchcase(t, pat):
                return True
        elif pat and pat in t:
            return True
    return False


class Firewall:
    def __init__(self, firewall_cfg: dict, hostname: str | None = None):
        cfg = firewall_cfg or {}
        bl = cfg.get("blocklist") or {}
        self._block_path_variants: list[tuple[str, ...]] = []
        for raw in bl.get("paths") or []:
            self._block_path_variants.extend(_path_variants(raw))
        self._block_apps = {_norm_app(a) for a in (bl.get("apps") or []) if a.strip()}
        self._block_titles = [str(p).casefold() for p in bl.get("window_titles") or []]
        self._block_urls = [str(p).casefold() for p in bl.get("url_patterns") or []]
        self._sensitive_titles = [str(p).casefold() for p in cfg.get("sensitive_titles") or []]
        self._redact = bool(cfg.get("redact_secrets", True))
        hosts = {str(h).casefold() for h in cfg.get("work_mode_hosts") or []}
        host = (hostname or socket.gethostname()).casefold()
        self.forced_work_mode = host in hosts

    # -- the one decision function -------------------------------------
    def decide(self, event: Event, work_mode: bool) -> Decision:
        if event.source == MANUAL:
            # Deliberate reflections are always recorded, even in work mode.
            return Decision(True, event.content)

        if self.forced_work_mode or work_mode:
            return Decision(False, reason="work_mode")

        if event.source == "clipboard" and not (event.app or event.window_title):
            # No idea which window this was copied from (e.g. Wayland, or a
            # transient attribution failure) — fail closed.
            return Decision(False, reason="no_attribution")

        for app in (event.app, event.alt_app):
            if app.strip() and _norm_app(app) in self._block_apps:
                return Decision(False, reason="blocked_app")

        titles_to_check = [event.window_title, event.alt_window_title]
        if event.source == "window":
            titles_to_check.append(event.content)
        if event.source == "browser":
            titles_to_check.append(str(event.extra.get("title", "")))
        for title in titles_to_check:
            if title and _pattern_hit(title, self._block_titles):
                return Decision(False, reason="blocked_title")

        if event.source == "file":
            hit, err = self._path_blocked(event.content)
            if err:
                return Decision(False, reason="path_error")  # fail closed
            if hit:
                return Decision(False, reason="blocked_path")

        # events that carry a provenance path (e.g. an AI prompt's project
        # dir) are dropped when that path is blocklisted — a prompt written
        # inside the company repo is company context
        src_path = str(event.extra.get("src_path") or "")
        if src_path:
            hit, err = self._path_blocked(src_path)
            if hit or err:
                return Decision(False, reason="blocked_src_path")

        # blocklist title patterns also run against searchable text content
        if event.source in ("search", "ai_prompt") and _pattern_hit(
            event.content, self._block_titles
        ):
            return Decision(False, reason="blocked_content")

        # a search event derived from a blocklisted URL is company context
        # even though its content is just the query text
        if event.source == "search" and _pattern_hit(
            str(event.extra.get("url", "")), self._block_urls
        ):
            return Decision(False, reason="blocked_url")

        if event.source == "browser":
            if _pattern_hit(event.content, self._block_urls):
                return Decision(False, reason="blocked_url")

        content = event.content
        if self._redact and event.source == "clipboard":
            content = redact_secrets(content)

        sensitivity = "normal"
        for title in titles_to_check:
            if title and _pattern_hit(title, self._sensitive_titles):
                sensitivity = "sensitive"
                break
        if event.source == "window" and _pattern_hit(content, self._sensitive_titles):
            sensitivity = "sensitive"

        return Decision(True, content, sensitivity)

    def _path_blocked(self, raw_path: str) -> tuple[bool, bool]:
        """(blocked, error). Checks literal AND resolved forms of the event
        path against literal AND resolved forms of every blocklist entry."""
        if not self._block_path_variants:
            return False, False
        try:
            raw = str(raw_path)
            # Real capture sources only ever produce clean absolute paths;
            # anything else is garbage we can't compare safely — fail closed.
            if "\x00" in raw or not Path(os.path.expanduser(raw)).is_absolute():
                return False, True
            event_variants = _path_variants(raw)
        except Exception:
            return False, True
        for ev in event_variants:
            for blocked in self._block_path_variants:
                if _prefix_match(ev, blocked):
                    return True, False
        return False, False


def redact_secrets(text: str) -> str:
    if PRIVATE_KEY_MARKER.search(text):
        return "[REDACTED:private-key]"
    for label, pattern in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text
