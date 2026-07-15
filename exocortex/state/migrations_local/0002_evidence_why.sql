-- 0002 (local): the gap rationale quotes my trail, so it lives here, not in
-- the synced concepts table.
ALTER TABLE concept_evidence ADD COLUMN why TEXT NOT NULL DEFAULT '';
