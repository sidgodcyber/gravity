-- 0005: multi-device hub & spoke. Hub-side ledgers that make spoke-batch
-- merging idempotent: a batch (by id) and an event (by deterministic
-- device-tagged uid) can each be imported at most once, ever.

CREATE TABLE spoke_batches (
    batch_id   TEXT PRIMARY KEY,             -- uuid4 minted by the spoke
    device     TEXT NOT NULL,                -- spoke's device_name
    created_ts INTEGER NOT NULL,             -- when the spoke built it (epoch ms)
    merged_ts  INTEGER NOT NULL,             -- when the hub merged it
    n_events   INTEGER NOT NULL DEFAULT 0,   -- events in the batch
    n_new      INTEGER NOT NULL DEFAULT 0    -- rows actually inserted
);
CREATE INDEX idx_spoke_batches_device ON spoke_batches(device, merged_ts);

CREATE TABLE spoke_events (
    event_uid TEXT PRIMARY KEY,              -- e.g. "desktop/reflections/12"
    merged_ts INTEGER NOT NULL
);
