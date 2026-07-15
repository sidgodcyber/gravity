"""Load config.yaml — the single place everything configurable lives.

Secrets may be inlined or written as "env:VARNAME" to pull from the
environment at load time. Paths accept ~ and are resolved against the
repo root when relative.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# Minimal safety-net defaults. config.example.yaml is the documented,
# complete reference; these only keep the system sane if a key is deleted.
DEFAULTS: dict = {
    "device_id": "",
    "role": "hub",          # hub: bot+cycles+canonical db | spoke: capture only
    "device_name": "",      # friendly name in provenance labels; "" = hostname
    "hub_name": "",         # named in spoke refusal messages
    "state_dir": "state",
    "capture": {
        "window_poll_s": 3,
        "clipboard_poll_s": 2,
        "clipboard_max_chars": 10000,
        "watched_folders": [],
        "flush_interval_s": 5,
        "flush_max_events": 100,
        "browser_import": {"enabled": True, "interval_hours": 6},
    },
    "firewall": {
        "work_mode_hosts": [],
        "blocklist": {"paths": [], "apps": [], "window_titles": [], "url_patterns": []},
        "sensitive_titles": [],
        "redact_secrets": True,
    },
    "telegram": {"bot_token": "", "chat_id": ""},
    "router": {
        "monthly_budget_usd": 10.0,
        "tiers": {"T0_local": [], "T1_free": [], "T2_workhorse": [], "T3_reasoning": []},
    },
    "embeddings": {"enabled": True, "model": "BAAI/bge-small-en-v1.5", "cache_dir": ".cache/models"},
    "claude_code": {"transcripts_dir": "~/.claude/projects"},
    "capture_code_from": [],          # Plane A allowlist; empty = no code capture
    "curriculum": {
        "trail_window_days": 14,
        "review_intervals_days": [2, 4, 8, 16],
        "weekly_question_day": "sun",
        "weekly_question_time": "19:00",
    },
    "voice": {"whisper_model": "base"},
    "cycle": {"brief_time": "07:00", "run_time": "03:30", "lookback_hours": 36},
    "crm": {"stale_after_days": 14},
    "sync": {"remote": "", "passphrase": "", "keep_versions": 14},
    # Obsidian export — derived data, regenerates from the db, never synced
    "vault": {"path": "d:/gravity-vault", "enabled": True},
}


class Config:
    def __init__(self, data: dict, path: Path | None):
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        if path is None:
            path = REPO_ROOT / "config.yaml"
        path = Path(path)
        user_cfg: dict = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if loaded is not None and not isinstance(loaded, dict):
                raise ConfigError(f"{path} must be a YAML mapping, got {type(loaded).__name__}")
            user_cfg = loaded or {}
        data = _resolve_env(_merge(DEFAULTS, user_cfg))
        return cls(data, path if path.exists() else None)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _path(self, raw: str) -> Path:
        p = Path(os.path.expanduser(raw))
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def state_dir(self) -> Path:
        return self._path(self.data["state_dir"])

    @property
    def db_path(self) -> Path:
        return self.state_dir / "exocortex.db"

    @property
    def local_db_path(self) -> Path:
        """Plane B trail db — local-only, structurally excluded from sync."""
        return self.state_dir / "local.db"

    @property
    def device_id(self) -> str:
        """The string records are stamped with. Prefers the friendly
        device_name so provenance reads "on desktop", not a hostname."""
        return (self.data.get("device_name") or self.data.get("device_id")
                or socket.gethostname())

    @property
    def device_name(self) -> str:
        return self.device_id

    @property
    def role(self) -> str:
        r = str(self.data.get("role") or "hub").strip().lower()
        if r not in ("hub", "spoke"):
            # fail CLOSED: a typo like "spok" must not silently unlock the
            # bot/cycles on a machine meant to be capture-only
            raise ConfigError(
                f"role must be 'hub' or 'spoke', got {r!r} — fix config.yaml")
        return r

    @property
    def is_spoke(self) -> bool:
        return self.role == "spoke"
