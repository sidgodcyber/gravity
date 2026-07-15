# Intent layer + editable memory (Phase 2.5)

- Every inbound message → intents/classify.py (T2, suppress_reasoning=True,
  max_tokens 1500). Returns one primary intent + secondary tags + extracted
  fields + confidence + raw_text. INTENTS: task, weekly_update, plan,
  client_note, reflection, question, correction.
- Anti-confabulation is STRUCTURAL: known-entity list is passed in; classifier
  OVERRIDES the model's "known" claim against the real list (normalize()), so
  a hallucinated brand comes back known=False and the echo warns. Dates are
  post-validated (_valid_due): unparseable/absurd → dropped, never guessed.
  Test: test_new_entity_flagged_not_invented, test_bad_due_date_dropped.
- intents/dispatch.py records `artifacts` (every row a message spawned) on the
  inbox row. This is the provenance for downstream healing.
- Editable memory (intents/memory_edit.py): delete_record heals via artifacts;
  delete_any((table,id)) also handles LEGACY records (reflections/crm_notes
  with inbox_id IS NULL, e.g. the pre-2.5 "Urban Straps" reflection). NL
  correction (resolve_correction) searches inbox + reflections + crm_notes,
  SHOWS matches, deletes only on confirm. _prune_graph_evidence removes graph
  nodes whose SOLE evidence was a deleted reflection; nodes with other
  evidence survive. _invalidate_today_brief clears the undelivered cached brief.
- CRITICAL ordering bug fixed: in _delete_artifacts, process 'crm' entries LAST
  (sorted key) — else the "does this client still have notes?" check runs
  before the note is deleted and the orphan client survives.
- intents/route.py holds the confirm/clarify state machine; the bot stores one
  `_pending` dict and feeds the next message to resolve_pending. Voice notes:
  transcribe → show → next reply is either "ok" (file as heard) or the
  correction text.
- Windows ONLOGON scheduled tasks need admin on this machine (Access denied) —
  daemon+bot launch via the Startup folder instead; only time-based tasks
  (nightly/brief/weekly) go through schtasks. exo install --status shows both.

CONFIRM STATE MACHINE (hardened after Phase 2.5 review — do not weaken):
- pending carries a "ts"; resolve_pending discards it past PENDING_TTL_MS
  (10 min) and routes the reply as a fresh message. A stale confirmation must
  never fire a delete later.
- Destructive confirms require an explicit word in AFFIRM; a reply that is
  neither AFFIRM nor NEGATE CANCELS the pending and routes the message (so
  "actually add task X" both cancels and creates). "ok" only confirms voice
  transcripts, never deletes.
- A slash command clears self._pending (owner_only wrapper) — the user moved
  on, so a later casual "ok" can't confirm a forgotten delete.
- route() ALWAYS persists raw text before fragile steps: classify failure →
  record_raw(intent='unclassified'); low-confidence clarify → record_raw then
  ask. Nothing is lost even when the model errors or the user never answers.
- replace_in() works on inbox (re-classify) AND legacy reflections/crm_notes
  (in-place, case-insensitive, re-extract) — a "replace" correction must never
  degrade into a delete offer.
- Echo lists EVERY created task with its own due date (a fabricated date on a
  2nd+ task must be visible); client_note with no resolved entity echoes
  honestly, never a false "saved".
