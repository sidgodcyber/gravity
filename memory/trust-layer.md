# Trust layer (final consolidation) — what was actually wrong, and the fixes

Five live-reproduced bugs; every fix has a regression test in
tests/test_trust_layer.py. Do not weaken any of these:

1. ID integrity: tasks.id and inbox.id were independent sequences — "Task #9"
   and inbox row 9 collided and /delete hit the wrong table. Fix: uids table
   (migration 0004, state/uids.py) — ONE user-facing namespace, AUTOINCREMENT
   (never reused), release() on delete. RULE: any # shown to the user must be
   a uid; any lookup must go through uids.resolve. Raw table ids never appear
   in user-facing text.
2. Confirmation state: was in-process (bot._pending) — lost on restart and
   split across accidental duplicate pollers (Telegram distributes updates
   between two getUpdates loops!). That's how "Yes — Delete 7" got STORED as a
   record. Fix: pending persisted in db control key ('pending_confirm', TTL
   10min), checked in route.handle_incoming BEFORE classification; slash
   commands clear it; bot singleton lock (state/bot.lock). Verified through
   the real PTB Application with a rebuild between /delete and "Yes".
3. NL commands: intents/resolve.py — LLM maps words→uids but may only return
   ids present in the provided context (last_shown + open tasks + recent +
   text-search candidates); anything else is stripped. Plan echoed (exact
   record list) before every destructive action.
4. Provenance: intents/provenance.py — you said / observed / inferred labels
   on every display; 'test' source reads as "system test message (not you)"
   (my own live probes once looked like user data — always source='test' or
   'cli' for probes!).
5. Grounding: dispatch._presupposition_evidence — AND-joined distinctive
   terms, and the triggering message is EXCLUDED from its own evidence (a
   task self-grounded off its own inbox row until that exclusion).

Also: bot handler wiring lives in build_app(cfg) — live-loop tests must use
it so the verified flow is the shipped flow. Browser backfill (sources/
backfill.py) imports full history (floor deliberately ignored, span reported);
two synced profiles produce duplicate visits — dedupe by (ts,source,content).

Post-verifier hardening (all have tests — do not weaken):
- EVERY delete of a uid-bearing row must call uids.release() — base tables
  have NO AUTOINCREMENT, rowids get reused, and an unreleased uid REBINDS to
  whatever reuses the row (reproduced live via edit_record before the fix).
- AFFIRM_STRICT (yes/yep/confirm/do it) for deletes/replaces/creates;
  AFFIRM_CASUAL (+ok/okay) ONLY for voice transcripts. "ok" never destroys.
- take_pending() consumes atomically (DELETE..RETURNING) — bot and CLI can't
  both execute one confirmation.
- A replace with no replacement text asks "what should it say instead?" —
  never falls through to a delete offer.
- Resolver disambiguation questions set a clarify pending so the answer
  merges and re-resolves instead of dead-ending as stored junk.
- /tasks and /due update last_shown too (ordinals/set-expressions reference
  the listing the user actually saw last, not a stale /recent).
- clear_pending runs AFTER the owner check (strangers can't mutate state);
  CLI delete previews and requires --yes.
- Anything that prints "#N" to the user MUST be a uid. Grep for f"#{ before
  shipping any new display: raw table ids in user-facing text are the bug
  class this whole layer exists to kill.
