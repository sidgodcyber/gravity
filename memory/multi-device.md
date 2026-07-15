# Multi-device (hub & spoke) — invariants and sharp edges

- ONE hub (role: hub) runs bot/cycles/brief and owns exocortex.db. Spokes
  run ONLY the capture daemon + `exo sync push` (queue batches). The role
  gate is layered on purpose: cli.SPOKE_COMMANDS allowlist, run_bot
  RoleError, sync.push/pull SyncError — do not collapse them into one check.
- role validation FAILS CLOSED: anything but exactly hub/spoke raises
  ConfigError (a "spok" typo once silently unlocked the bot → the two-
  pollers-one-token bug class this exists to kill).
- Spoke staging: the spoke's state/exocortex.db is staging, never a brain.
  `exo log` there uses extract_now=False (spokes make ZERO LLM calls) and
  prints NO #N (uids are the hub's namespace).
- Batch/merge idempotency is two ledgers (migration 0005): spoke_batches by
  batch_id, spoke_events by deterministic uid. Reflection uids embed the row
  ts ("desktop/reflections/12@<ts>") because staging rowids restart after a
  db reset — id-only uids silently swallowed post-reset rows.
- Checkpoints advance to the max id actually BATCHED, never SELECT MAX(id)
  of the table (a concurrently staged row would be skipped forever).
- merge_batches containment rules (verifier-reproduced failures, all have
  regression tests in tests/test_multidevice.py):
  * passphrase checked ONCE before the loop — missing passphrase must abort,
    not misfile good batches into failed/;
  * everything per-file is contained: malformed-but-decryptable batch →
    rollback → failed/ → NEXT file (one bad file used to wedge every merge
    and every nightly, silently);
  * hub-named batches refused; unknown tables refused (Plane B guard on the
    receiving side); refused events are NOT ledgered so a fixed re-batch
    can still land; raw_text capped (goes into LLM prompts later).
- capture_code_from is per-device (dict keyed by device_name; flat list =
  legacy this-machine form). device_code_paths() in daemon/allowlist.py is
  the ONLY reader. Missing device key → [] → captures nothing.
- device_name must be UNIQUE per machine — uids are namespaced by it; two
  spokes sharing a name silently swallow each other's rows. setup and merge
  both guard the hub-name collision; duplicate spoke names are docs-only.
- cfg.device_id now prefers device_name ("laptop"/"desktop") — all stamping
  flows through it. `exo migrate` relabels old hostname rows once.
- Plane B still never leaves any machine: batch builder SELECTs only
  reflections/code_activity; each device keeps its own trail, so the
  concept engine sees only the hub's trail (deliberate, documented).
