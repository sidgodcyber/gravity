-- Local-only database (state/local.db): Plane B — the general activity trail.
-- THIS FILE IS NEVER INCLUDED IN SYNC BUNDLES. The sync layer only packages
-- state/exocortex.db; keeping Plane B in a separate file makes "never synced"
-- structural rather than a filter that could regress.

CREATE TABLE life_stream (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    device      TEXT NOT NULL,
    source      TEXT NOT NULL,               -- window | clipboard | file | browser | search | ai_prompt
    kind        TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    app         TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    extra       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_life_stream_ts ON life_stream(ts);
CREATE INDEX idx_life_stream_source_ts ON life_stream(source, ts);

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

-- machine-local control values (capture checkpoints etc.)
CREATE TABLE control (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);

-- evidence trail behind synced concepts: the concept names/scores/explainers
-- sync (they're curriculum), but the raw queries/prompts that justify them
-- stay on this machine.
CREATE TABLE concept_evidence (
    id           INTEGER PRIMARY KEY,
    concept_name TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    evidence     TEXT NOT NULL DEFAULT '[]'   -- JSON list of citation strings
);
CREATE INDEX idx_concept_evidence_name ON concept_evidence(concept_name);
