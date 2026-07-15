# Two capture planes (Phase 2) — the safety-critical invariant

- Plane A (code/git): read ONLY through daemon/allowlist.py from
  `capture_code_from`. Allowlist, not blocklist — absent paths are unreadable.
  Resolved-path matching so a symlink/junction inside an allowed tree pointing
  out does NOT widen it. Fails closed. Lives in exocortex.db, SYNCS.
- Plane B (browser/search/ai_prompt/window/clipboard): ALL daemon capture goes
  to state/local.db via db.local_connect(). local.db is NEVER in a sync bundle
  — structural (separate file; make_bundle tars only exocortex.db+config).
  sync.push() also runs split_plane_b then REFUSES if non-manual rows remain in
  the synced db (guards the Phase-1→2 stale-daemon window).
- split_plane_b (db.py): idempotent one-time move of non-manual rows main→local
  via ATTACH + INSERT...SELECT, verified by delta count before DELETE. Called
  by daemon start, nightly, migrate, and push.
- Concept `why` prose quotes the trail → stored in local.db concept_evidence,
  never in synced concepts table. Names/scores/explainers sync.
- Tests: tests/test_planes.py has the adversarial cases (symlink-out-of-
  allowlist, sync canary in local.db, search-from-blocklisted-url). They SKIP
  (not fail) when Defender blocks mklink/git subprocess — that's the AV quirk,
  re-run to confirm.
