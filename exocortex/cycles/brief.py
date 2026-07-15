"""/brief — serve today's stored brief, or compose one on demand."""

from __future__ import annotations

import datetime as dt

from ..config import Config
from ..state import db
from .nightly import compose_brief
from .telegram_send import send_message


def brief_command(cfg: Config, send: bool = False) -> str:
    """--send (the 07:00 scheduler) delivers today's stored brief from the
    03:30 cycle; on-demand /brief always composes fresh — 'brief me now'
    must reflect what I logged five minutes ago, not a stale cache."""
    conn = db.connect(cfg.db_path)
    local_conn = db.local_connect(cfg)  # the trail lives in local.db
    try:
        if send:
            midnight = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            row = conn.execute(
                "SELECT id, content FROM briefs WHERE ts >= ? ORDER BY ts DESC LIMIT 1",
                (int(midnight.timestamp() * 1000),),
            ).fetchone()
            text = row["content"] if row else compose_brief(cfg, conn, local_conn)
            if send_message(cfg, text):
                conn.execute(
                    "UPDATE briefs SET delivered = 1 WHERE id = (SELECT MAX(id) FROM briefs)"
                )
                conn.commit()
            return text
        return compose_brief(cfg, conn, local_conn)
    finally:
        local_conn.close()
        conn.close()
