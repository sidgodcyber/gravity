-- 0001: initial schema. Timestamps are unix epoch milliseconds, UTC.

CREATE TABLE life_stream (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    device      TEXT NOT NULL,
    source      TEXT NOT NULL,               -- window | clipboard | file | browser | manual
    kind        TEXT NOT NULL DEFAULT '',    -- source subtype, e.g. file event type
    content     TEXT NOT NULL,               -- window title / clipboard text / path / url
    app         TEXT NOT NULL DEFAULT '',    -- foreground process name where applicable
    sensitivity TEXT NOT NULL DEFAULT 'normal',  -- normal | sensitive
    extra       TEXT NOT NULL DEFAULT '{}'   -- JSON
);
CREATE INDEX idx_life_stream_ts ON life_stream(ts);
CREATE INDEX idx_life_stream_source_ts ON life_stream(source, ts);

CREATE TABLE reflections (
    id                INTEGER PRIMARY KEY,
    ts                INTEGER NOT NULL,
    device            TEXT NOT NULL,
    raw_text          TEXT NOT NULL,
    -- fields below are filled by LLM extraction
    domain            TEXT,
    project           TEXT,
    skills            TEXT,                  -- JSON array of skill names
    insight_type      TEXT,                  -- learned | decision | idea | observation
    portfolio_score   REAL,                  -- 0..1, how portfolio-worthy
    extraction_status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    extraction_model  TEXT
);
CREATE INDEX idx_reflections_ts ON reflections(ts);
CREATE INDEX idx_reflections_status ON reflections(extraction_status);

-- skill graph as plain tables
CREATE TABLE skill_nodes (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,                -- skill | domain | tool | project | person | client
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    meta       TEXT NOT NULL DEFAULT '{}',
    UNIQUE(name, type)
);
CREATE TABLE skill_edges (
    id         INTEGER PRIMARY KEY,
    src        INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
    dst        INTEGER NOT NULL REFERENCES skill_nodes(id) ON DELETE CASCADE,
    rel        TEXT NOT NULL,                -- uses | part_of | demonstrated_in | works_with
    weight     REAL NOT NULL DEFAULT 0.5,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '[]',   -- JSON list of {"table": ..., "id": ...}
    UNIQUE(src, dst, rel)
);
CREATE INDEX idx_skill_edges_src ON skill_edges(src);
CREATE INDEX idx_skill_edges_dst ON skill_edges(dst);

CREATE TABLE opportunities (
    id              INTEGER PRIMARY KEY,
    ts              INTEGER NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'manual',
    fit_score       REAL,
    payoff_estimate TEXT,
    deadline        TEXT,                    -- ISO date if known
    status          TEXT NOT NULL DEFAULT 'new',  -- new | surfaced | interested | passed | done
    feedback        TEXT                     -- my reaction and why
);
CREATE INDEX idx_opportunities_status ON opportunities(status);

CREATE TABLE crm (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'prospect',  -- prospect | client | past_client
    notes           TEXT NOT NULL DEFAULT '',
    next_action     TEXT,
    next_action_due TEXT,                    -- ISO date
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
);
CREATE TABLE crm_touchpoints (
    id      INTEGER PRIMARY KEY,
    crm_id  INTEGER NOT NULL REFERENCES crm(id) ON DELETE CASCADE,
    ts      INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT '',        -- email | dm | call | meeting
    summary TEXT NOT NULL
);
CREATE INDEX idx_crm_touchpoints_crm ON crm_touchpoints(crm_id, ts);

CREATE TABLE style_corpus (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    kind      TEXT NOT NULL DEFAULT '',      -- email | post | message | caption
    audience  TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL,
    embedding BLOB                           -- float32 vector; NULL until embedded
);

CREATE TABLE usage_ledger (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    tier       TEXT NOT NULL,
    task       TEXT NOT NULL,                -- e.g. extract_reflection, nightly_summary, ask
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_usage_ledger_ts ON usage_ledger(ts);

-- the system's own lessons (runtime learnings, e.g. user feedback on briefs)
CREATE TABLE memory (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    name    TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    body    TEXT NOT NULL
);

-- generated morning briefs, so /brief can serve the latest even offline
CREATE TABLE briefs (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    content   TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);

-- small key/value control plane: work_mode flag, import checkpoints, etc.
CREATE TABLE control (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);

-- manual /spent log for Claude Code usage fallback
CREATE TABLE spent_log (
    id     INTEGER PRIMARY KEY,
    ts     INTEGER NOT NULL,
    what   TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0           -- user-estimated USD or token count
);

-- full-text search (external-content FTS5, kept in sync by triggers)
CREATE VIRTUAL TABLE life_stream_fts USING fts5(
    content, content='life_stream', content_rowid='id'
);
CREATE TRIGGER life_stream_ai AFTER INSERT ON life_stream BEGIN
    INSERT INTO life_stream_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER life_stream_ad AFTER DELETE ON life_stream BEGIN
    INSERT INTO life_stream_fts(life_stream_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER life_stream_au AFTER UPDATE ON life_stream BEGIN
    INSERT INTO life_stream_fts(life_stream_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO life_stream_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE VIRTUAL TABLE reflections_fts USING fts5(
    raw_text, content='reflections', content_rowid='id'
);
CREATE TRIGGER reflections_ai AFTER INSERT ON reflections BEGIN
    INSERT INTO reflections_fts(rowid, raw_text) VALUES (new.id, new.raw_text);
END;
CREATE TRIGGER reflections_ad AFTER DELETE ON reflections BEGIN
    INSERT INTO reflections_fts(reflections_fts, rowid, raw_text) VALUES ('delete', old.id, old.raw_text);
END;
CREATE TRIGGER reflections_au AFTER UPDATE ON reflections BEGIN
    INSERT INTO reflections_fts(reflections_fts, rowid, raw_text) VALUES ('delete', old.id, old.raw_text);
    INSERT INTO reflections_fts(rowid, raw_text) VALUES (new.id, new.raw_text);
END;

CREATE VIRTUAL TABLE style_corpus_fts USING fts5(
    text, content='style_corpus', content_rowid='id'
);
CREATE TRIGGER style_corpus_ai AFTER INSERT ON style_corpus BEGIN
    INSERT INTO style_corpus_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER style_corpus_ad AFTER DELETE ON style_corpus BEGIN
    INSERT INTO style_corpus_fts(style_corpus_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER style_corpus_au AFTER UPDATE ON style_corpus BEGIN
    INSERT INTO style_corpus_fts(style_corpus_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO style_corpus_fts(rowid, text) VALUES (new.id, new.text);
END;
