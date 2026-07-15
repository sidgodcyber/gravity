-- 0002: Phase 2 — auto-curriculum. Synced-db side: concepts (the curriculum
-- state, portable) and code_activity (Plane A git capture, allowlist-gated).
-- Evidence behind concepts lives in local.db (see migrations_local/0001).

CREATE TABLE concepts (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    domain         TEXT,
    status         TEXT NOT NULL DEFAULT 'gap',   -- gap | reviewing | graduated
    gap_score      REAL NOT NULL DEFAULT 0.5,     -- how much this needs attention
    engagement     TEXT NOT NULL DEFAULT '',      -- surface | deep (from engine)
    why            TEXT NOT NULL DEFAULT '',      -- plain-language gap rationale
    explainer      TEXT,                          -- short original teaching text
    check_question TEXT,                          -- spaced-rep prompt
    first_seen     INTEGER NOT NULL,
    last_seen      INTEGER NOT NULL,
    surfaced_ts    INTEGER,                       -- last time in a brief
    next_review_ts INTEGER,
    review_stage   INTEGER NOT NULL DEFAULT 0,
    fumble_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_concepts_status ON concepts(status);
CREATE INDEX idx_concepts_review ON concepts(next_review_ts);

CREATE TABLE code_activity (
    id            INTEGER PRIMARY KEY,
    ts            INTEGER NOT NULL,              -- commit time, unix ms
    repo          TEXT NOT NULL,
    commit_hash   TEXT NOT NULL,
    message       TEXT NOT NULL,
    files_changed INTEGER NOT NULL DEFAULT 0,
    insertions    INTEGER NOT NULL DEFAULT 0,
    deletions     INTEGER NOT NULL DEFAULT 0,
    files         TEXT NOT NULL DEFAULT '[]',    -- JSON list, repo-relative
    UNIQUE(repo, commit_hash)
);
CREATE INDEX idx_code_activity_ts ON code_activity(ts);
