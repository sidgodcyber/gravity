# Concept-inference engine (Phase 2) — how it stays honest

- curriculum/engine.py: mechanical per-day trail aggregation → ONE structured
  LLM call (T2 free stack) → validate_gaps() cross-checks every evidence quote
  is a VERBATIM (casefold) substring of the material. Ungrounded citations are
  dropped; a gap with no surviving evidence is discarded and logged. This gate
  cannot be prompted away — it's the anti-confabulation guarantee.
- Thin trail (< MIN_TRAIL_EVENTS=8) returns "not enough trail" WITHOUT calling
  the LLM. /ask and the engine state absence rather than invent urgency/dates
  (the Phase-1 fabricated-deadline failure mode; test in test_curriculum.py).
- CRITICAL: browser/ai_trail imports MUST set Event.ts to the ORIGINAL event
  time (visit time / prompt timestamp), not now. Without it every imported row
  lands on today and the "recurring across days" signal collapses — the live
  engine returned 0 gaps until this was fixed. Verified: real 30-day trail →
  5 grounded gaps (mediapipe api, issdc access, claude-code setup, ...).
- Skill-graph writeback uses upsert_node(bump=False) + set_node_confidence:
  recurring lookups push confidence DOWN, graduation UP. A default +0.1 upsert
  bump would cancel the penalties (was a real bug caught in review).
- One-concept-per-day: surface_daily has a GLOBAL 20h gate (control key
  foundation_last_surfaced_ms) on top of the per-concept 3-day cooldown, so
  repeated /brief calls in one day don't burn multiple concepts.
- Browser first-run backfills only 30 days (backfill_floor) so years of old
  history don't flood the per-profile 2000-row cap.

- "Connection you missed" (cycles/nightly.py missed_connection): anti-confab.
  States a cross-project link ONLY when two recently-active, otherwise-unlinked
  endpoint nodes (project/domain) share a CONCRETE ARTIFACT node
  (tool/skill/concept/technique/dataset/file) via edges — never shared
  vocabulary. Shared domain/category = excluded (that's jargon). Returns ""
  → brief omits the line; the returned string names the artifact so the LLM
  cites it instead of free-associating, and the prompt says omit-if-empty +
  don't-invent. Adversarial test: two projects sharing only a domain label
  (Chandrayaan-2 / AgeWarp) → "" (test_no_connection_on_surface_jargon_only).
