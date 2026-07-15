"""Shared daemon-liveness check (status file + pid)."""

from __future__ import annotations

import json
import time


def daemon_alive(cfg) -> bool:
    status_file = cfg.state_dir / "daemon_status.json"
    if not status_file.exists():
        return False
    try:
        import psutil
        s = json.loads(status_file.read_text(encoding="utf-8"))
        return (bool(s.get("running")) and psutil.pid_exists(int(s["pid"]))
                and time.time() - float(s["updated"]) < 60)
    except Exception:
        return False
