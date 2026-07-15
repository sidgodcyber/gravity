"""Style corpus: import my writing samples for voice-matched drafting."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..config import Config
from ..state import db

log = logging.getLogger("exo.style")

TEXT_EXTS = {".txt", ".md", ".markdown"}


def import_samples(cfg: Config, path: str, kind: str = "") -> int:
    """Import a file (one sample) or a directory (one sample per text file),
    then embed the new rows if embeddings are enabled."""
    root = Path(os.path.expanduser(path))
    files: list[Path]
    if root.is_dir():
        files = [p for p in sorted(root.rglob("*")) if p.suffix.lower() in TEXT_EXTS]
    elif root.is_file():
        files = [root]
    else:
        raise FileNotFoundError(path)

    conn = db.connect(cfg.db_path)
    try:
        n = 0
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            exists = conn.execute(
                "SELECT 1 FROM style_corpus WHERE text = ? LIMIT 1", (text,)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO style_corpus (ts, kind, audience, text) VALUES (?, ?, ?, ?)",
                (db.now_ms(), kind, "", text),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()

    if n and cfg.get("embeddings.enabled", True):
        try:
            from .embeddings import embed_pending
            embed_pending(cfg)
        except Exception:
            log.exception("embedding failed; run `exo embed` to retry")
    return n
