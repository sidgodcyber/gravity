-- 0003: Phase 2.5 — task & intent layer + editable memory.

-- every inbound message, classified. `artifacts` records exactly what this
-- message spawned (tasks, reflections, NEW graph nodes, dossier notes) so
-- edits/deletes can heal downstream instead of leaving ghost data.
CREATE TABLE inbox (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    raw_text   TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'telegram',   -- telegram | voice | cli
    intent     TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',         -- secondary intents
    extracted  TEXT NOT NULL DEFAULT '{}',         -- full classifier JSON
    confidence REAL,
    artifacts  TEXT NOT NULL DEFAULT '[]'          -- [{"table": ..., "id": ..., "created": bool}]
);
CREATE INDEX idx_inbox_ts ON inbox(ts);

CREATE TABLE tasks (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    parent_id  INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    entity     TEXT,                                -- client/project it belongs to
    status     TEXT NOT NULL DEFAULT 'open',        -- open | done | dropped
    due_ts     INTEGER,                             -- epoch ms, NULL = no deadline
    priority   INTEGER NOT NULL DEFAULT 0,          -- 0 normal .. 2 high (explicit only)
    inbox_id   INTEGER,                             -- source message
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE INDEX idx_tasks_status_due ON tasks(status, due_ts);
CREATE INDEX idx_tasks_entity ON tasks(entity);

-- client dossier entries (Phase 3 builds on these); one row per note so a
-- deleted source message can surgically remove exactly its own notes
CREATE TABLE crm_notes (
    id       INTEGER PRIMARY KEY,
    crm_id   INTEGER NOT NULL REFERENCES crm(id) ON DELETE CASCADE,
    ts       INTEGER NOT NULL,
    note     TEXT NOT NULL,
    inbox_id INTEGER
);
CREATE INDEX idx_crm_notes_crm ON crm_notes(crm_id);

-- link reflections back to the inbox message that spawned them (provenance
-- for downstream healing on edit/delete)
ALTER TABLE reflections ADD COLUMN inbox_id INTEGER;
