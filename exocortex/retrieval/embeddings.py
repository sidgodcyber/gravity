"""Local CPU embeddings via fastembed (ONNX). Loaded only inside batch jobs
and import commands — never resident in the daemon or idle bot."""

from __future__ import annotations

import logging
import os
import sqlite3
import struct

from ..config import Config
from ..state import db

log = logging.getLogger("exo.embeddings")

_model = None


def _get_model(cfg: Config):
    global _model
    if _model is None:
        cache = cfg._path(cfg.get("embeddings.cache_dir", ".cache/models"))
        cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("FASTEMBED_CACHE_PATH", str(cache))
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=cfg.get("embeddings.model"), cache_dir=str(cache))
    return _model


def embed_texts(cfg: Config, texts: list[str]) -> list[bytes]:
    model = _get_model(cfg)
    return [pack(v) for v in model.embed(texts)]


def pack(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *(float(x) for x in vec))


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def embed_pending(cfg: Config, reembed_all: bool = False) -> int:
    """Embed style_corpus rows that don't have a vector yet."""
    if not cfg.get("embeddings.enabled", True):
        return 0
    conn = db.connect(cfg.db_path)
    try:
        where = "" if reembed_all else "WHERE embedding IS NULL"
        rows = conn.execute(f"SELECT id, text FROM style_corpus {where}").fetchall()
        if not rows:
            return 0
        vectors = embed_texts(cfg, [r["text"] for r in rows])
        conn.executemany(
            "UPDATE style_corpus SET embedding = ? WHERE id = ?",
            [(v, r["id"]) for v, r in zip(vectors, rows)],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def style_examples(cfg: Config, conn: sqlite3.Connection, query: str, k: int = 3,
                   kind: str = "") -> list[sqlite3.Row]:
    """Top-k style samples for few-shot voice matching: vector search when
    embeddings exist, FTS fallback otherwise."""
    kind_sql = " AND kind = ?" if kind else ""
    params: tuple = (kind,) if kind else ()
    rows = conn.execute(
        f"SELECT id, kind, text, embedding FROM style_corpus"
        f" WHERE embedding IS NOT NULL{kind_sql}", params,
    ).fetchall()
    if rows and cfg.get("embeddings.enabled", True):
        try:
            qvec = unpack(embed_texts(cfg, [query])[0])
            scored = sorted(
                rows, key=lambda r: -cosine(qvec, unpack(r["embedding"]))
            )
            return scored[:k]
        except Exception:
            log.exception("vector style search failed; falling back to FTS")
    terms = fts_terms(query)
    if not terms:
        return conn.execute(
            f"SELECT id, kind, text FROM style_corpus WHERE 1=1{kind_sql}"
            " ORDER BY ts DESC LIMIT ?", (*params, k),
        ).fetchall()
    return conn.execute(
        "SELECT sc.id, sc.kind, sc.text FROM style_corpus_fts f"
        " JOIN style_corpus sc ON sc.id = f.rowid"
        f" WHERE style_corpus_fts MATCH ?{kind_sql} ORDER BY rank LIMIT ?",
        (terms, *params, k),
    ).fetchall()


STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "with", "was", "were", "what",
    "when", "where", "how", "why", "who", "did", "does", "have", "has", "had",
    "this", "that", "about", "from", "into", "you", "your", "are", "not",
}


def fts_terms(question: str, limit: int = 8) -> str:
    """Turn free text into a safe FTS5 OR-query."""
    import re
    words = [w for w in re.findall(r"[A-Za-z0-9_]{3,}", question.lower())
             if w not in STOPWORDS]
    unique = list(dict.fromkeys(words))[:limit]
    return " OR ".join(f'"{w}"' for w in unique)
