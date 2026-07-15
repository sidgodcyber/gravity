-- 0004: one user-facing ID namespace. Every record the bot ever shows gets a
-- row here; the uid is the ONLY number displayed to the user, and resolving a
-- uid always returns the exact row it was minted for. Fixes the Task#9 /
-- inbox#9 collision where two tables' independent sequences shared numbers.
CREATE TABLE uids (
    uid    INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl    TEXT NOT NULL,          -- tasks | inbox | reflections | crm_notes
    row_id INTEGER NOT NULL,
    UNIQUE(tbl, row_id)
);
