"""One-shot Telegram delivery via plain HTTP — works even when the bot
process is down (used by the nightly cycle and `exo brief --send`)."""

from __future__ import annotations

import logging

import requests

from ..config import Config

log = logging.getLogger("exo.telegram")


def send_message(cfg: Config, text: str) -> bool:
    token = (cfg.get("telegram.bot_token") or "").strip()
    chat_id = str(cfg.get("telegram.chat_id") or "").strip()
    if not token or not chat_id:
        log.warning("telegram not configured (bot_token/chat_id) — not sending")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=30,
        )
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            log.warning("telegram send failed: %s %s", r.status_code, r.text[:200])
        return bool(ok)
    except requests.RequestException as e:
        log.warning("telegram send failed: %s", e)
        return False
